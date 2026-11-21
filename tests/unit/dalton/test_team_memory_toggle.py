"""Unit tests for Team Memory (OpenClaw conversation archive) per-workspace & Home toggles (d028)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import create_autospec

import alembic.op as alembic_op
from fastapi import HTTPException
import psycopg
import pytest

from ptc_agent.agent.agent import PTCAgent
from ptc_agent.agent.graph import build_ptc_graph_with_session
from ptc_agent.agent.middleware.runtime_context.epoch import Workspace
from ptc_agent.agent.prompts.loader import PromptLoader
from ptc_agent.config.agent import AgentConfig
from ptc_agent.core.project_context import ProjectContext
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from ptc_agent.core.session import Session
from src.config import settings as app_settings
from src.dalton import access_tiers as dalton_tiers
from src.dalton import sharing as dalton_sharing
from src.dalton.app import router as dalton_router
from src.dalton.data_map import builder as map_builder
from src.dalton.database import data_map as db_data_map
from src.dalton.migrations.versions import d028_workspace_settings
from src.dalton.tools import build_dalton_tools
from src.server.database import user as user_db
from src.server.database import workspace as ws_db

TEAM_EMAILS = (
    "richard.lu@dalton.test,chao.yu@dalton.test,"
    "rex.yuan@dalton.test,ext.user@partner.test"
)

USERS_BY_ID: dict[str, dict[str, Any]] = {
    "u-richard": {
        "user_id": "u-richard",
        "email": "richard.lu@dalton.test",
        "name": "Richard Lu",
    },
    "u-chao": {
        "user_id": "u-chao",
        "email": "chao.yu@dalton.test",
        "name": "Chao Yu",
    },
    "u-pm": {
        "user_id": "u-pm",
        "email": "rex.yuan@dalton.test",
        "name": "Rex Yuan (PM)",
    },
    "u-external": {
        "user_id": "u-external",
        "email": "ext.user@partner.test",
        "name": "External Analyst",
    },
}


def _enable_dalton_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setattr(app_settings, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("DALTON_ALLOWED_EMAILS", TEAM_EMAILS)
    monkeypatch.setenv("DALTON_PM_USER_IDS", "u-pm")

    async def _fake_get_user(uid: str) -> dict[str, Any] | None:
        return USERS_BY_ID.get(uid)

    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(user_db.get_user, side_effect=_fake_get_user),
    )

    async def _fake_get_tier(uid: str) -> str:
        if uid == "u-external":
            return "external"
        if uid == "u-pm":
            return "admin"
        return "internal"

    async def _fake_classify_owner_tier(uid: str) -> str:
        if uid == "u-external":
            return "external"
        if uid in USERS_BY_ID:
            return "internal"
        return "unknown"

    monkeypatch.setattr(
        dalton_tiers,
        "get_user_tier",
        create_autospec(dalton_tiers.get_user_tier, side_effect=_fake_get_tier),
    )
    monkeypatch.setattr(
        dalton_tiers,
        "classify_owner_tier",
        create_autospec(
            dalton_tiers.classify_owner_tier, side_effect=_fake_classify_owner_tier
        ),
    )


def _sample_workspace(
    *,
    workspace_id: str = "11111111-1111-4111-8111-111111111111",
    user_id: str = "u-richard",
    name: str = "NVDA Deep Dive",
    status: str = "running",
    is_home: bool = False,
) -> dict[str, Any]:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    return {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "name": name,
        "description": "Semiconductor research",
        "sandbox_id": "sbx-1",
        "computer_id": "22222222-2222-4222-8222-222222222222",
        "dir_name": "NVDA_Deep_Dive",
        "previous_dir_names": [],
        "status": status,
        "is_home": is_home,
        "created_at": now,
        "updated_at": now,
        "last_activity_at": now,
        "stopped_at": None,
        "config": {},
    }


def _sample_cached_data_map_markdown() -> str:
    return map_builder.render_data_map_markdown(
        catalog_routes=[
            {"id": "all", "category": "research", "title": "跨库初筛", "rules": ["四库+AE+GP"]},
        ],
        handoff_routes=frozenset(),
        health_map={
            "conversation_search": {"status": "available", "latency_ms": 12, "latest_date": "2026-11-05"},
            "web_search": {"status": "available", "latency_ms": 80, "latest_date": "2026-11-05"},
        },
        conversation_stats={
            "total_cards": 1542,
            "min_date": "2026-06-01",
            "max_date": "2026-11-05",
            "by_type": {"fact": 412, "opinion": 364},
            "top_subjects": [{"label": "NVDA", "count": 218}],
        },
        search_api="openclaw",
    )


def test_d028_migration_upgrade_and_downgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """d028 creates dalton_workspace_settings and dalton_user_settings (default false) and drops both on downgrade."""
    assert d028_workspace_settings.revision == "d028"
    assert d028_workspace_settings.down_revision == "d027"

    exec_mock = create_autospec(alembic_op.execute)
    monkeypatch.setattr(d028_workspace_settings.op, "execute", exec_mock)

    d028_workspace_settings.upgrade()
    assert exec_mock.call_count == 2
    sql_ws = " ".join(str(exec_mock.call_args_list[0][0][0]).split())
    sql_user = " ".join(str(exec_mock.call_args_list[1][0][0]).split())

    assert "CREATE TABLE IF NOT EXISTS dalton_workspace_settings" in sql_ws
    assert (
        "workspace_id UUID PRIMARY KEY REFERENCES workspaces(workspace_id) ON DELETE CASCADE"
        in sql_ws
    )
    assert "team_memory_enabled BOOLEAN NOT NULL DEFAULT FALSE" in sql_ws
    assert "updated_by TEXT" in sql_ws
    assert "updated_at TIMESTAMPTZ" in sql_ws

    assert "CREATE TABLE IF NOT EXISTS dalton_user_settings" in sql_user
    assert "user_id VARCHAR(255) PRIMARY KEY" in sql_user
    assert "home_team_memory_enabled BOOLEAN NOT NULL DEFAULT FALSE" in sql_user
    assert "updated_at TIMESTAMPTZ" in sql_user

    exec_mock.reset_mock()
    d028_workspace_settings.downgrade()
    assert exec_mock.call_count == 2
    down_sql = "\n".join(
        " ".join(str(call[0][0]).split()) for call in exec_mock.call_args_list
    )
    assert "DROP TABLE IF EXISTS dalton_user_settings" in down_sql
    assert "DROP TABLE IF EXISTS dalton_workspace_settings" in down_sql


@pytest.mark.asyncio
async def test_default_off_omits_tool_prompt_and_data_map_card_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no row exists in dalton_workspace_settings, Team Memory defaults to off across tool, prompt, and data map."""
    fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    fake_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    fake_cur.fetchone.return_value = None

    @asynccontextmanager
    async def _cur_ctx(*args: Any, **kwargs: Any):
        yield fake_cur

    fake_conn.cursor.side_effect = _cur_ctx

    ws_id = "11111111-1111-4111-8111-111111111111"
    row = await dalton_sharing.get_workspace_settings(ws_id, conn=fake_conn)
    assert row == {
        "workspace_id": ws_id,
        "team_memory_enabled": False,
        "updated_by": None,
        "updated_at": None,
    }

    enabled = await dalton_sharing.resolve_turn_team_memory_enabled(
        role="analyst",
        workspace_id=ws_id,
        user_id="u-richard",
        user_tier="internal",
        conn=fake_conn,
    )
    assert enabled is False

    # 1) Tool not mounted by default
    analyst_tool_names = {t.name for t in build_dalton_tools("analyst", user_tier="internal")}
    cos_tool_names = {t.name for t in build_dalton_tools("chief_of_staff", user_tier="internal")}
    assert "conversation_search" not in analyst_tool_names
    assert "conversation_search" not in cos_tool_names

    # 2) Shared cached data map is filtered at turn render time (no card stats line)
    cached_md = _sample_cached_data_map_markdown()
    assert "**1542**" in cached_md
    assert "- `conversation_search`" in cached_md
    monkeypatch.setattr(
        db_data_map,
        "get_cached_data_map",
        create_autospec(
            db_data_map.get_cached_data_map,
            return_value={
                "cache_date": "2026-11-05",
                "rendered_markdown": cached_md,
                "payload": {},
            },
        ),
    )

    turn_md = await map_builder.warm_turn_data_map(
        search_api="openclaw",
        cache_date=date(2026, 11, 5),
        team_memory_enabled=False,
    )
    assert "- `conversation_search`" not in turn_md
    assert "归档统计：" not in turn_md
    assert "**1542**" not in turn_md
    assert "先用 `conversation_search`" not in turn_md
    assert "- `web_search`" in turn_md

    # 3) System prompt for both roles omits the conversation_search sentence and card stats line
    loader = PromptLoader()
    for role in ("analyst", "chief_of_staff"):
        prompt = loader.get_system_prompt(
            role=role,
            conversation_search_enabled=False,
        )
        assert "conversation_search" not in prompt
        assert "回答某家公司或主题时，先用" not in prompt
        assert "归档统计：" not in prompt
        assert "说“证据不足/无法确定”之前，对照下方数据地图，看是否还有会改变结论的直接相关来源没查" in prompt


@pytest.mark.asyncio
async def test_owner_enables_workspace_team_memory_mounts_all_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After workspace owner enables Team Memory, tool, prompt sentence, and data map card line are all active."""
    _enable_dalton_auth(monkeypatch)
    ws = _sample_workspace(user_id="u-richard")
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, return_value=ws),
    )

    stored_state = {"team_memory_enabled": False}

    async def _fake_upsert(
        workspace_id: str,
        *,
        team_memory_enabled: bool,
        updated_by: str,
        conn: Any = None,
    ) -> dict[str, Any]:
        stored_state["team_memory_enabled"] = bool(team_memory_enabled)
        return {
            "workspace_id": workspace_id,
            "team_memory_enabled": stored_state["team_memory_enabled"],
            "updated_by": updated_by,
            "updated_at": "2026-11-05T12:00:00+00:00",
        }

    async def _fake_get(
        workspace_id: str, *, conn: Any = None
    ) -> dict[str, Any]:
        return {
            "workspace_id": workspace_id,
            "team_memory_enabled": stored_state["team_memory_enabled"],
            "updated_by": "u-richard",
            "updated_at": "2026-11-05T12:00:00+00:00",
        }

    monkeypatch.setattr(
        dalton_sharing,
        "upsert_workspace_settings",
        create_autospec(dalton_sharing.upsert_workspace_settings, side_effect=_fake_upsert),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "get_workspace_settings",
        create_autospec(dalton_sharing.get_workspace_settings, side_effect=_fake_get),
    )

    # Owner enables Team Memory via PUT /workspaces/{id}/settings
    put_resp = await dalton_router.update_workspace_settings_endpoint(
        workspace_id=ws["workspace_id"],
        body=dalton_router.WorkspaceSettingsUpdateRequest(team_memory_enabled=True),
        user_id="u-richard",
    )
    assert put_resp["team_memory_enabled"] is True
    assert put_resp["can_edit"] is True

    get_resp = await dalton_router.get_workspace_settings_endpoint(
        workspace_id=ws["workspace_id"],
        user_id="u-richard",
    )
    assert get_resp["team_memory_enabled"] is True
    assert get_resp["can_edit"] is True

    # Turn resolution reads True
    turn_enabled = await dalton_sharing.resolve_turn_team_memory_enabled(
        role="analyst",
        workspace_id=ws["workspace_id"],
        user_id="u-richard",
        user_tier="internal",
    )
    assert turn_enabled is True

    # 1) Tool is mounted
    tools = {
        t.name
        for t in build_dalton_tools(
            "analyst", user_tier="internal", team_memory_enabled=turn_enabled
        )
    }
    assert "conversation_search" in tools

    # 2) Data map keeps conversation_search and card stats line
    cached_md = _sample_cached_data_map_markdown()
    monkeypatch.setattr(
        db_data_map,
        "get_cached_data_map",
        create_autospec(
            db_data_map.get_cached_data_map,
            return_value={
                "cache_date": "2026-11-05",
                "rendered_markdown": cached_md,
                "payload": {},
            },
        ),
    )
    turn_md = await map_builder.warm_turn_data_map(
        search_api="openclaw",
        cache_date=date(2026, 11, 5),
        team_memory_enabled=turn_enabled,
    )
    assert "- `conversation_search`" in turn_md
    assert "归档统计：" in turn_md
    assert "**1542**" in turn_md

    # 3) System prompt includes the tool_guide sentence and the data map card row
    prompt = PromptLoader().get_system_prompt(
        role="analyst",
        conversation_search_enabled=turn_enabled,
    )
    assert (
        "回答某家公司或主题时，先用 `conversation_search` 查团队以前的观点、假设和决定，并在答案里说明与之前看法相比有什么变化"
        in prompt
    )
    assert "归档统计：" in prompt
    assert "**1542**" in prompt


@pytest.mark.asyncio
async def test_colleague_cannot_modify_workspace_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Colleagues (even with rw web/cos access or PM status) cannot modify another owner's workspace settings."""
    _enable_dalton_auth(monkeypatch)
    ws = _sample_workspace(user_id="u-richard")
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, return_value=ws),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "get_workspace_sharing",
        create_autospec(
            dalton_sharing.get_workspace_sharing,
            return_value={
                "workspace_id": ws["workspace_id"],
                "cos_access": "rw",
                "web_access": "rw",
                "updated_by": "u-richard",
                "updated_at": None,
            },
        ),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "get_workspace_settings",
        create_autospec(
            dalton_sharing.get_workspace_settings,
            return_value={
                "workspace_id": ws["workspace_id"],
                "team_memory_enabled": True,
                "updated_by": "u-richard",
                "updated_at": None,
            },
        ),
    )
    upsert_spy = create_autospec(dalton_sharing.upsert_workspace_settings)
    monkeypatch.setattr(dalton_sharing, "upsert_workspace_settings", upsert_spy)

    # Colleague with web_access='rw' can GET (with can_edit=False), but PUT raises 403
    colleague_get = await dalton_router.get_workspace_settings_endpoint(
        workspace_id=ws["workspace_id"],
        user_id="u-chao",
    )
    assert colleague_get["team_memory_enabled"] is True
    assert colleague_get["can_edit"] is False

    for non_owner in ("u-chao", "u-pm"):
        with pytest.raises(HTTPException) as exc_info:
            await dalton_router.update_workspace_settings_endpoint(
                workspace_id=ws["workspace_id"],
                body=dalton_router.WorkspaceSettingsUpdateRequest(
                    team_memory_enabled=False
                ),
                user_id=non_owner,
            )
        assert exc_info.value.status_code == 403

    assert upsert_spy.await_count == 0


@pytest.mark.asyncio
async def test_colleague_turn_in_enabled_workspace_uses_workspace_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A colleague-dispatch turn in a shared workspace uses that workspace's Team Memory switch."""
    _enable_dalton_auth(monkeypatch)
    ws_id = "11111111-1111-4111-8111-111111111111"

    monkeypatch.setattr(
        dalton_sharing,
        "get_workspace_settings",
        create_autospec(
            dalton_sharing.get_workspace_settings,
            return_value={
                "workspace_id": ws_id,
                "team_memory_enabled": True,
                "updated_by": "u-richard",
                "updated_at": None,
            },
        ),
    )
    # Even if colleague's own Home user setting is False, the workspace turn follows the workspace setting
    monkeypatch.setattr(
        dalton_sharing,
        "get_user_team_memory_settings",
        create_autospec(
            dalton_sharing.get_user_team_memory_settings,
            return_value={
                "user_id": "u-chao",
                "home_team_memory_enabled": False,
                "updated_at": None,
            },
        ),
    )

    cached_md = _sample_cached_data_map_markdown()
    monkeypatch.setattr(
        db_data_map,
        "get_cached_data_map",
        create_autospec(
            db_data_map.get_cached_data_map,
            return_value={
                "cache_date": "2026-11-05",
                "rendered_markdown": cached_md,
                "payload": {},
            },
        ),
    )

    import ptc_agent.agent.graph as graph_mod
    import src.server.database.user_vault_secrets as vault_db

    fake_agent = create_autospec(PTCAgent, instance=True)
    fake_agent.create_agent.return_value = "BUILT_GRAPH"
    monkeypatch.setattr(
        graph_mod,
        "PTCAgent",
        create_autospec(PTCAgent, return_value=fake_agent),
    )
    monkeypatch.setattr(
        graph_mod,
        "fetch_user_data_counts",
        create_autospec(graph_mod.fetch_user_data_counts, return_value=None),
    )
    monkeypatch.setattr(
        graph_mod,
        "_read_workspace_naming",
        create_autospec(graph_mod._read_workspace_naming, return_value=Workspace("NVDA")),
    )
    monkeypatch.setattr(
        graph_mod,
        "_read_harness_blocks",
        create_autospec(graph_mod._read_harness_blocks, return_value=({}, None)),
    )
    monkeypatch.setattr(
        vault_db,
        "get_user_secrets_decrypted",
        create_autospec(vault_db.get_user_secrets_decrypted, return_value={}),
    )

    fake_session = create_autospec(Session, instance=True)
    fake_session.sandbox = create_autospec(PTCSandbox, instance=True)
    fake_session.sandbox.vault_secrets = {}
    fake_session.mcp_registry = object()
    fake_session.mcp_tool_summary = "SUMMARY"
    fake_config = create_autospec(AgentConfig, instance=True)
    fake_config.search_api = "openclaw"

    await build_ptc_graph_with_session(
        session=fake_session,
        config=fake_config,
        user_id="u-chao",
        workspace_owner_id="u-richard",
        project=ProjectContext(ws_id, "NVDA_Deep_Dive"),
        role="analyst",
    )

    call_kwargs = fake_agent.create_agent.call_args.kwargs
    assert call_kwargs.get("team_memory_enabled") is True
    assert call_kwargs.get("user_tier") == "internal"
    assert "**1542**" in map_builder.resolve_prompt_data_map()


@pytest.mark.asyncio
async def test_external_member_never_gets_team_memory_even_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External members never receive conversation_search, the prompt sentence, or the data map card row."""
    _enable_dalton_auth(monkeypatch)
    ws = _sample_workspace(user_id="u-external")
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, return_value=ws),
    )

    # Even if a database row somehow had team_memory_enabled=True, external tier resolves to False
    monkeypatch.setattr(
        dalton_sharing,
        "get_workspace_settings",
        create_autospec(
            dalton_sharing.get_workspace_settings,
            return_value={
                "workspace_id": ws["workspace_id"],
                "team_memory_enabled": True,
                "updated_by": "u-external",
                "updated_at": None,
            },
        ),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "get_user_team_memory_settings",
        create_autospec(
            dalton_sharing.get_user_team_memory_settings,
            return_value={
                "user_id": "u-external",
                "home_team_memory_enabled": True,
                "updated_at": None,
            },
        ),
    )

    for role in ("analyst", "chief_of_staff"):
        resolved = await dalton_sharing.resolve_turn_team_memory_enabled(
            role=role,
            workspace_id=ws["workspace_id"],
            user_id="u-external",
            user_tier="external",
        )
        assert resolved is False
        tool_names = {
            t.name
            for t in build_dalton_tools(
                role, user_tier="external", team_memory_enabled=True
            )
        }
        assert "conversation_search" not in tool_names

    # External owner GET /workspaces/{id}/settings -> team_memory_enabled=False, can_edit=False
    ext_ws_get = await dalton_router.get_workspace_settings_endpoint(
        workspace_id=ws["workspace_id"],
        user_id="u-external",
    )
    assert ext_ws_get["team_memory_enabled"] is False
    assert ext_ws_get["can_edit"] is False

    # External owner PUT /workspaces/{id}/settings -> 403
    with pytest.raises(HTTPException) as exc_ws_put:
        await dalton_router.update_workspace_settings_endpoint(
            workspace_id=ws["workspace_id"],
            body=dalton_router.WorkspaceSettingsUpdateRequest(team_memory_enabled=True),
            user_id="u-external",
        )
    assert exc_ws_put.value.status_code == 403

    # External user GET /user-settings -> home_team_memory_enabled=False, can_edit=False
    ext_user_get = await dalton_router.get_user_settings_endpoint(user_id="u-external")
    assert ext_user_get["home_team_memory_enabled"] is False
    assert ext_user_get["can_edit"] is False

    # External user PUT /user-settings -> 403
    with pytest.raises(HTTPException) as exc_user_put:
        await dalton_router.update_user_settings_endpoint(
            body=dalton_router.UserSettingsUpdateRequest(home_team_memory_enabled=True),
            user_id="u-external",
        )
    assert exc_user_put.value.status_code == 403


@pytest.mark.asyncio
async def test_home_turn_follows_user_level_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Home (chief_of_staff) turns follow dalton_user_settings.home_team_memory_enabled (default False)."""
    _enable_dalton_auth(monkeypatch)
    home_state = {"home_team_memory_enabled": False}

    async def _fake_get_user_settings(
        user_id: str, *, conn: Any = None
    ) -> dict[str, Any]:
        return {
            "user_id": user_id,
            "home_team_memory_enabled": home_state["home_team_memory_enabled"],
            "updated_at": None,
        }

    async def _fake_upsert_user_settings(
        user_id: str,
        *,
        home_team_memory_enabled: bool,
        conn: Any = None,
    ) -> dict[str, Any]:
        home_state["home_team_memory_enabled"] = bool(home_team_memory_enabled)
        return {
            "user_id": user_id,
            "home_team_memory_enabled": home_state["home_team_memory_enabled"],
            "updated_at": "2026-11-05T12:00:00+00:00",
        }

    monkeypatch.setattr(
        dalton_sharing,
        "get_user_team_memory_settings",
        create_autospec(
            dalton_sharing.get_user_team_memory_settings,
            side_effect=_fake_get_user_settings,
        ),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "upsert_user_team_memory_settings",
        create_autospec(
            dalton_sharing.upsert_user_team_memory_settings,
            side_effect=_fake_upsert_user_settings,
        ),
    )

    # 1) Default off in Home
    initial = await dalton_router.get_user_settings_endpoint(user_id="u-richard")
    assert initial["home_team_memory_enabled"] is False
    assert initial["can_edit"] is True

    resolved_off = await dalton_sharing.resolve_turn_team_memory_enabled(
        role="chief_of_staff",
        workspace_id="home-ws-id",
        user_id="u-richard",
        user_tier="internal",
    )
    assert resolved_off is False
    assert "conversation_search" not in {
        t.name
        for t in build_dalton_tools(
            "chief_of_staff", user_tier="internal", team_memory_enabled=resolved_off
        )
    }
    cos_prompt_off = PromptLoader().get_system_prompt(
        role="chief_of_staff",
        conversation_search_enabled=resolved_off,
    )
    assert "conversation_search" not in cos_prompt_off
    assert "归档统计：" not in cos_prompt_off

    # 2) User enables Home Team Memory via PUT /user-settings
    updated = await dalton_router.update_user_settings_endpoint(
        body=dalton_router.UserSettingsUpdateRequest(home_team_memory_enabled=True),
        user_id="u-richard",
    )
    assert updated["home_team_memory_enabled"] is True

    resolved_on = await dalton_sharing.resolve_turn_team_memory_enabled(
        role="chief_of_staff",
        workspace_id="home-ws-id",
        user_id="u-richard",
        user_tier="internal",
    )
    assert resolved_on is True
    assert "conversation_search" in {
        t.name
        for t in build_dalton_tools(
            "chief_of_staff", user_tier="internal", team_memory_enabled=resolved_on
        )
    }
    cos_prompt_on = PromptLoader().get_system_prompt(
        role="chief_of_staff",
        conversation_search_enabled=resolved_on,
    )
    assert "first call `conversation_search` to check the team's prior views, assumptions and decisions" in cos_prompt_on
    assert "回答某家公司或主题时，先用 `conversation_search`" in cos_prompt_on
    assert "归档统计：" in cos_prompt_on


@pytest.mark.asyncio
async def test_codex_round_trip_preserves_prefix_and_appends_runtime_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On GPT-6.2/Codex, toggling Team Memory (ON -> OFF -> ON) keeps the
    pinned system prompt and top-level tools array byte-identical across turns
    while appending one durable Chinese runtime message on each toggle (and none
    on unchanged turns)."""
    from langchain.agents import create_agent
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool
    from langchain_openai.chat_models.base import BaseChatOpenAI
    from langgraph.checkpoint.memory import InMemorySaver

    from ptc_agent.agent.main_state import MainAgentState
    from ptc_agent.agent.middleware.reasoning_compat import ReasoningCompatibilityMiddleware
    from ptc_agent.agent.middleware.runtime_context import (
        TEAM_MEMORY_OFF_TEXT,
        TEAM_MEMORY_ON_TEXT,
        TEAM_MEMORY_ROW_KIND,
        RowCarrierMiddleware,
        TurnContextMiddleware,
        runtime_update_from_message,
    )
    from ptc_agent.agent.middleware.runtime_context.frozen_prompt import (
        STATE_SYSTEM_PIN,
        SystemPinMiddleware,
    )
    from ptc_agent.agent.middleware.tool_surface import ToolSurfaceMiddleware
    from src.llms.llm import create_llm

    @tool
    def search_data(query: str) -> str:
        """Look up market data."""
        return query

    @tool("conversation_search")
    def conversation_search_stub(query: str) -> str:
        """Search Dalton's team conversation archive for prior views and decisions."""
        return query

    cached_md = _sample_cached_data_map_markdown()
    monkeypatch.setattr(
        db_data_map,
        "get_cached_data_map",
        create_autospec(
            db_data_map.get_cached_data_map,
            return_value={
                "cache_date": "2026-11-05",
                "rendered_markdown": cached_md,
                "payload": {},
            },
        ),
    )

    codex_client = create_llm("gpt-6.2-sol-oauth", api_key="sk-codex-test")
    captured_payloads: list[dict[str, Any]] = []
    turn_counter = 0

    async def _fake_agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        nonlocal turn_counter
        turn_counter += 1
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        captured_payloads.append(payload)
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=[
                            {
                                "type": "reasoning",
                                "id": f"rs_turn{turn_counter}",
                                "summary": [],
                                "encrypted_content": f"enc_turn{turn_counter}",
                            },
                            {"type": "text", "text": f"Answer on turn {turn_counter}."},
                        ],
                        id=f"msg_codex_{turn_counter}",
                        response_metadata={
                            "model_name": "gpt-6.2-sol",
                            "reasoning_origin": "codex-oauth",
                        },
                    )
                )
            ]
        )

    monkeypatch.setattr(BaseChatOpenAI, "_agenerate", _fake_agenerate)
    monkeypatch.setattr(BaseChatOpenAI, "_should_stream", lambda self, **_: False)

    saver = InMemorySaver()
    cfg = {"configurable": {"thread_id": "t-codex-roundtrip", "user_id": "u-richard"}}

    async def _run_turn(user_text: str, *, enabled: bool, step_minutes: int) -> None:
        await map_builder.warm_turn_data_map(
            search_api="openclaw",
            cache_date=date(2026, 11, 5),
            team_memory_enabled=enabled,
        )
        prompt = PromptLoader().get_system_prompt(
            role="chief_of_staff",
            conversation_search_enabled=enabled,
        )
        tools_list = [search_data, conversation_search_stub] if enabled else [search_data]
        agent = create_agent(
            model=codex_client,
            tools=tools_list,
            system_prompt=prompt,
            middleware=[
                ToolSurfaceMiddleware(),
                TurnContextMiddleware(
                    now=datetime(2026, 11, 21, 10, step_minutes, tzinfo=UTC),
                    timezone="UTC",
                    conversation_search_enabled=enabled,
                ),
                RowCarrierMiddleware(),
                SystemPinMiddleware(),
                ReasoningCompatibilityMiddleware(),
            ],
            checkpointer=saver,
            state_schema=MainAgentState,
        )
        await agent.ainvoke({"messages": [HumanMessage(user_text)]}, cfg)

    def _developer_texts(payload: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for item in payload["input"]:
            if isinstance(item, dict) and item.get("role") == "developer":
                content = item.get("content")
                if isinstance(content, list):
                    out.append("\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict)))
                else:
                    out.append(str(content or ""))
        return out

    def _toggle_rows(messages: list[Any]) -> list[tuple[bool, str]]:
        rows: list[tuple[bool, str]] = []
        for m in messages:
            upd = runtime_update_from_message(m)
            if upd is not None and upd.kind == TEAM_MEMORY_ROW_KIND:
                rows.append((bool(upd.provenance.get("enabled")), upd.text))
        return rows

    # Turn 1 (ON): initial prompt has conversation_search; no extra toggle row
    await _run_turn("NVDA 团队怎么看？", enabled=True, step_minutes=0)
    # Turn 2 (ON, unchanged): no toggle row appended
    await _run_turn("毛利率呢？", enabled=True, step_minutes=1)
    # Turn 3 (OFF): prefix (system prompt + top-level tools) identical to Turn 1; OFF row appended
    await _run_turn("关闭团队记忆后继续看下估值", enabled=False, step_minutes=2)
    # Turn 4 (OFF, unchanged): no duplicate OFF row
    await _run_turn("再看下现金流", enabled=False, step_minutes=3)
    # Turn 5 (ON): prefix still identical to Turn 1; ON row appended
    await _run_turn("重新开启团队记忆查下历史结论", enabled=True, step_minutes=4)
    # Turn 6 (ON, unchanged): no duplicate ON row
    await _run_turn("总结一下", enabled=True, step_minutes=5)

    assert len(captured_payloads) == 6
    baseline_tools = captured_payloads[0]["tools"]
    assert [t["name"] for t in baseline_tools] == ["search_data", "conversation_search"]
    baseline_system = _developer_texts(captured_payloads[0])[0]
    assert "conversation_search" in baseline_system

    for idx, payload in enumerate(captured_payloads):
        assert payload["tools"] == baseline_tools, f"turn {idx + 1} top-level tools mutated"
        dev_msgs = _developer_texts(payload)
        assert dev_msgs[0] == baseline_system, f"turn {idx + 1} system prompt prefix mutated"

    state_after = await create_agent(
        model=codex_client,
        tools=[search_data],
        system_prompt="noop",
        checkpointer=saver,
        state_schema=MainAgentState,
    ).aget_state(cfg)
    assert _toggle_rows(state_after.values["messages"]) == [
        (False, TEAM_MEMORY_OFF_TEXT),
        (True, TEAM_MEMORY_ON_TEXT),
    ]
    assert TEAM_MEMORY_OFF_TEXT in _developer_texts(captured_payloads[2])[-1]
    assert TEAM_MEMORY_ON_TEXT in _developer_texts(captured_payloads[4])[-1]
    # Unchanged turns did not append a new toggle row in the trailing developer block
    assert TEAM_MEMORY_OFF_TEXT not in _developer_texts(captured_payloads[1])[-1]
    assert TEAM_MEMORY_ON_TEXT not in _developer_texts(captured_payloads[3])[-1]
    assert not state_after.values[STATE_SYSTEM_PIN].get("stale")


@pytest.mark.asyncio
async def test_codex_off_to_on_uses_additional_tools_and_off_refuses_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Starting OFF -> ON uses OpenAI's `additional_tools` mechanism without mutating
    the top-level `tools` prefix; turning OFF refuses subsequent `conversation_search`
    tool calls via `ToolSurfaceMiddleware._refusal`."""
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.tools import tool
    from langchain.agents.middleware.types import ModelRequest, ModelResponse

    from ptc_agent.agent.middleware.runtime_context import (
        TEAM_MEMORY_OFF_TEXT,
        TEAM_MEMORY_ON_TEXT,
        TurnContextMiddleware,
    )
    from ptc_agent.agent.middleware.tool_surface import (
        TOOL_SURFACE_KEY,
        ToolSurfaceMiddleware,
    )
    from src.llms.llm import create_llm

    @tool
    def search_data(query: str) -> str:
        """Look up market data."""
        return query

    @tool("conversation_search")
    def conversation_search_stub(query: str) -> str:
        """Search Dalton's team conversation archive."""
        return query

    codex_client = create_llm("gpt-6.2-sol-oauth", api_key="sk-codex-test")
    surf_mw = ToolSurfaceMiddleware()
    seen: list[ModelRequest] = []

    async def _capture(req: ModelRequest) -> ModelResponse:
        seen.append(req)
        return ModelResponse(result=[AIMessage("ok")])

    # Turn 1 (OFF): top-level tools has only search_data
    req1 = ModelRequest(
        model=codex_client,
        messages=[HumanMessage("q1")],
        system_prompt="prompt off",
        state={TOOL_SURFACE_KEY: {}},
        tools=[search_data],
        model_settings={},
    )
    res1 = await surf_mw.awrap_model_call(req1, _capture)
    surface1 = res1.command.update[TOOL_SURFACE_KEY]
    assert [t["function"]["name"] for t in seen[0].tools] == ["search_data"]
    assert surface1["offered"] == ["search_data"]

    # Turn 2 (ON): top-level tools keeps search_data; conversation_search arrives via additional_tools
    bound = AIMessage(
        content=[{"type": "reasoning", "id": "rs1", "summary": [], "encrypted_content": "e1"}],
        response_metadata={"model_name": "gpt-6.2-sol", "reasoning_origin": "codex-oauth"},
    )
    turn2_rows = await TurnContextMiddleware(
        now=datetime(2026, 11, 21, 10, 5, tzinfo=UTC),
        conversation_search_enabled=True,
    ).abefore_agent({"messages": [HumanMessage("q1"), bound, HumanMessage("q2")], "runtime_system_pin": {"content": "prompt off"}})
    assert turn2_rows is not None
    assert turn2_rows["messages"][-1].content == TEAM_MEMORY_ON_TEXT

    req2 = ModelRequest(
        model=codex_client,
        messages=[HumanMessage("q1"), bound, HumanMessage("q2"), *turn2_rows["messages"]],
        system_prompt="prompt on",
        state={TOOL_SURFACE_KEY: surface1},
        tools=[search_data, conversation_search_stub],
        model_settings={},
    )
    res2 = await surf_mw.awrap_model_call(req2, _capture)
    surface2 = res2.command.update[TOOL_SURFACE_KEY]
    assert [t["function"]["name"] for t in seen[1].tools] == ["search_data"]
    assert surface2["offered"] == ["search_data", "conversation_search"]
    add_msg = seen[1].messages[-1]
    assert add_msg.content[0]["type"] == "additional_tools"
    assert [t["name"] for t in add_msg.content[0]["tools"]] == ["conversation_search"]

    # Turn 3 (OFF): offered drops conversation_search; top-level tools untouched; _refusal blocks call
    turn3_rows = await TurnContextMiddleware(
        now=datetime(2026, 11, 21, 10, 6, tzinfo=UTC),
        conversation_search_enabled=False,
    ).abefore_agent({"messages": [HumanMessage("q1"), bound, HumanMessage("q2"), *turn2_rows["messages"], AIMessage("ok"), HumanMessage("q3")]})
    assert turn3_rows is not None
    assert turn3_rows["messages"][-1].content == TEAM_MEMORY_OFF_TEXT

    req3 = ModelRequest(
        model=codex_client,
        messages=[HumanMessage("q1"), bound, HumanMessage("q2"), *turn2_rows["messages"], AIMessage("ok"), HumanMessage("q3"), *turn3_rows["messages"]],
        system_prompt="prompt off",
        state={TOOL_SURFACE_KEY: surface2},
        tools=[search_data],
        model_settings={},
    )
    res3 = await surf_mw.awrap_model_call(req3, _capture)
    surface3 = res3.command.update[TOOL_SURFACE_KEY]
    assert [t["function"]["name"] for t in seen[2].tools] == ["search_data"]
    assert surface3["offered"] == ["search_data"]

    called = False

    async def _should_not_run(_req: Any) -> Any:
        nonlocal called
        called = True
        return None

    refused = await surf_mw.awrap_tool_call(
        SimpleNamespace(
            state={TOOL_SURFACE_KEY: surface3},
            tool_call={"name": "conversation_search", "id": "call_cs_off", "args": {"query": "NVDA"}},
        ),
        _should_not_run,
    )
    assert called is False
    assert refused.status == "error"
    assert "conversation_search" in refused.content


@pytest.mark.asyncio
async def test_team_memory_notice_reappended_after_compaction_and_legacy_pin() -> None:
    """A compacted-out toggle notice is re-appended on the next turn; a legacy
    pinned thread without prior toggle rows emits the notice on flip; and the
    row stays out of summarization and baseline drift counts."""
    from langchain_core.messages import AIMessage, HumanMessage

    from ptc_agent.agent.middleware.compaction.summary_request import _summarizable
    from ptc_agent.agent.middleware.runtime_context import (
        NON_CHANGE_ROW_KINDS,
        TEAM_MEMORY_OFF_TEXT,
        TEAM_MEMORY_ON_TEXT,
        TEAM_MEMORY_ROW_KIND,
        TurnContextMiddleware,
        render_update_row,
        runtime_update_from_message,
    )
    from ptc_agent.agent.middleware.runtime_context.baseline import _retained_change_rows
    from ptc_agent.agent.middleware.runtime_context.durable import (
        DurableUpdate,
        build_update_message,
    )
    from ptc_agent.agent.middleware.runtime_context.frozen_prompt import STATE_SYSTEM_PIN
    from ptc_agent.agent.prompts import guidance_template_vars, init_loader

    assert TEAM_MEMORY_ROW_KIND in NON_CHANGE_ROW_KINDS
    now = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)

    # 1) Compaction cutoff moves past the last team_memory_switched row -> re-appended once
    t1 = await TurnContextMiddleware(
        now=now, timezone="UTC", conversation_search_enabled=True
    ).abefore_agent({"messages": [HumanMessage("q1")]})
    assert [runtime_update_from_message(m).kind for m in t1["messages"]] == ["turn_opened"]

    hist = [HumanMessage("q1"), *t1["messages"], AIMessage("a1"), HumanMessage("q2")]
    t2 = await TurnContextMiddleware(
        now=now, timezone="UTC", conversation_search_enabled=False
    ).abefore_agent({"messages": hist})
    assert [runtime_update_from_message(m).kind for m in t2["messages"]] == [
        "turn_opened",
        TEAM_MEMORY_ROW_KIND,
    ]
    assert t2["messages"][1].content == TEAM_MEMORY_OFF_TEXT

    hist.extend([*t2["messages"], AIMessage("a2"), HumanMessage("q3")])
    # Compaction cuts off everything up to q3 (index len(hist))
    t3 = await TurnContextMiddleware(
        now=now, timezone="UTC", conversation_search_enabled=False
    ).abefore_agent({"messages": hist, "_summarization_event": {"cutoff_index": len(hist)}})
    assert [runtime_update_from_message(m).kind for m in t3["messages"]] == [
        "turn_opened",
        TEAM_MEMORY_ROW_KIND,
    ]
    assert t3["messages"][1].content == TEAM_MEMORY_OFF_TEXT

    # Next turn still OFF with the re-appended notice in view -> NOT repeated
    hist_after = [*hist, *t3["messages"], AIMessage("a3"), HumanMessage("q4")]
    t4 = await TurnContextMiddleware(
        now=now, timezone="UTC", conversation_search_enabled=False
    ).abefore_agent({"messages": hist_after, "_summarization_event": {"cutoff_index": len(hist)}})
    assert [runtime_update_from_message(m).kind for m in t4["messages"]] == ["turn_opened"]

    # 2) Legacy pinned thread (no turn_opened provenance) detects mismatch via STATE_SYSTEM_PIN
    legacy_state_on = {
        "messages": [HumanMessage("old q")],
        STATE_SYSTEM_PIN: {"model": "gpt-6.2-sol", "content": "Call conversation_search first."},
    }
    unchanged_legacy = await TurnContextMiddleware(
        now=now, timezone="UTC", conversation_search_enabled=True
    ).abefore_agent(legacy_state_on)
    assert [runtime_update_from_message(m).kind for m in unchanged_legacy["messages"]] == [
        "turn_opened"
    ]
    flipped_legacy = await TurnContextMiddleware(
        now=now, timezone="UTC", conversation_search_enabled=False
    ).abefore_agent(legacy_state_on)
    assert [runtime_update_from_message(m).kind for m in flipped_legacy["messages"]] == [
        "turn_opened",
        TEAM_MEMORY_ROW_KIND,
    ]
    assert flipped_legacy["messages"][1].content == TEAM_MEMORY_OFF_TEXT

    # 3) Summarizer & baseline rebuild ignore team_memory_switched; render is level-agnostic
    switch_msg = build_update_message(
        DurableUpdate(
            kind=TEAM_MEMORY_ROW_KIND,
            schema_version=1,
            text=TEAM_MEMORY_ON_TEXT,
            provenance={"source": "user", "enabled": True},
            created_at=now,
        )
    )
    assert [m.content for m in _summarizable([HumanMessage("hi"), switch_msg])] == ["hi"]
    assert _retained_change_rows({"messages": [switch_msg]}) == ()
    assert render_update_row(runtime_update_from_message(switch_msg)) == TEAM_MEMORY_ON_TEXT
    loader = init_loader()
    renders = {
        loader.render(
            "envelope/update_row.md.j2",
            **guidance_template_vars(level),
            update=runtime_update_from_message(switch_msg).to_dict(),
        ).strip()
        for level in ("lean", "detailed")
    }
    assert renders == {TEAM_MEMORY_ON_TEXT}


@pytest.mark.asyncio
async def test_conversation_search_tool_rechecks_team_memory_toggle_at_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Executing `conversation_search` directly (e.g. Claude Code or mid-turn toggle off)
    reads `resolve_turn_team_memory_enabled` and refuses without calling `hybrid_search_conversations`."""
    import json

    from src.dalton.tools import conversation_tools as conv_tools_mod

    monkeypatch.setattr(
        conv_tools_mod,
        "get_user_tier",
        create_autospec(conv_tools_mod.get_user_tier, return_value="internal"),
    )
    search_mock = create_autospec(
        conv_tools_mod.hybrid_search_conversations,
        return_value={"ok": True, "count": 1, "cards": []},
    )
    monkeypatch.setattr(conv_tools_mod, "hybrid_search_conversations", search_mock)

    real_resolve = conv_tools_mod.resolve_turn_team_memory_enabled

    # 1) Disabled on analyst workspace -> refused without querying archive
    resolve_off = create_autospec(real_resolve, return_value=False)
    monkeypatch.setattr(conv_tools_mod, "resolve_turn_team_memory_enabled", resolve_off)
    analyst_cs = conv_tools_mod.create_conversation_tools(role="analyst")[0]
    raw_off = await analyst_cs.ainvoke(
        {"query": "NVDA Blackwell"},
        config={"configurable": {"user_id": "u-richard", "workspace_id": "ws-nvda", "thread_id": "th-1"}},
    )
    parsed_off = json.loads(raw_off)
    assert parsed_off["ok"] is False
    assert parsed_off["error"] == "team_memory_disabled"
    assert parsed_off["message"] == conv_tools_mod.TEAM_MEMORY_DISABLED_MESSAGE
    search_mock.assert_not_awaited()
    resolve_off.assert_awaited_once_with(
        role="analyst",
        workspace_id="ws-nvda",
        user_id="u-richard",
        user_tier="internal",
    )

    # 2) Disabled on Home / Chief of Staff (inferred when built without explicit role) -> refused
    from src.server.database.home_workspace import get_home_workspace_id

    home_ws = get_home_workspace_id("u-richard")
    resolve_home_off = create_autospec(real_resolve, return_value=False)
    monkeypatch.setattr(conv_tools_mod, "resolve_turn_team_memory_enabled", resolve_home_off)
    raw_home = await conv_tools_mod.conversation_search.ainvoke(
        {"query": "TSMC CoWoS"},
        config={"configurable": {"user_id": "u-richard", "workspace_id": home_ws, "thread_id": "th-2"}},
    )
    parsed_home = json.loads(raw_home)
    assert parsed_home["ok"] is False
    assert parsed_home["error"] == "team_memory_disabled"
    search_mock.assert_not_awaited()
    resolve_home_off.assert_awaited_once_with(
        role="chief_of_staff",
        workspace_id=home_ws,
        user_id="u-richard",
        user_tier="internal",
    )

    # 3) Enabled -> executes hybrid_search_conversations as normal
    resolve_on = create_autospec(real_resolve, return_value=True)
    monkeypatch.setattr(conv_tools_mod, "resolve_turn_team_memory_enabled", resolve_on)
    raw_on = await analyst_cs.ainvoke(
        {"query": "NVDA Blackwell"},
        config={"configurable": {"user_id": "u-richard", "workspace_id": "ws-nvda", "thread_id": "th-1"}},
    )
    assert json.loads(raw_on)["ok"] is True
    search_mock.assert_awaited_once()


def test_ptc_agent_passes_conversation_search_enabled_to_turn_context_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PTCAgent.create_agent passes the effective conversation_search_enabled
    into TurnContextMiddleware while keeping ToolSurfaceMiddleware and
    SystemPinMiddleware unmodified."""
    from unittest.mock import patch

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from ptc_agent.agent.middleware.runtime_context import TurnContextMiddleware
    from ptc_agent.core.paths import SandboxLayout, WorkspaceLayout
    from ptc_agent.core.mcp_registry import MCPRegistry
    from ptc_agent.config.agent import AgentConfig, LLMConfig
    from ptc_agent.config.core import (
        DaytonaConfig,
        FilesystemConfig,
        LoggingConfig,
        MCPConfig,
        SandboxConfig,
        SecurityConfig,
    )

    def _build_with(team_memory_enabled: bool, user_tier: str = "internal") -> list[Any]:
        cfg = AgentConfig(
            llm=LLMConfig(name="test-model"),
            security=SecurityConfig(),
            logging=LoggingConfig(),
            sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test-key")),
            mcp=MCPConfig(),
            filesystem=FilesystemConfig(),
        )
        llm = GenericFakeChatModel(messages=iter([]))
        captured: list[dict[str, Any]] = []

        def _fake_create_agent(_model: Any, **kwargs: Any) -> Any:
            captured.append(kwargs)
            g = create_autospec(PTCAgent, instance=True)
            g.with_config = lambda *_a, **_kw: g
            return g

        fake_sandbox = create_autospec(PTCSandbox, instance=True)
        fake_sandbox.config = cfg
        fake_sandbox.working_dir = "/home/workspace"
        fake_sandbox.livefs = None
        fake_sandbox.mounts_livefs = False
        fake_sandbox.history = None
        fake_sandbox.skills_manifest = None
        fake_sandbox.vault_secrets = {}
        fake_sandbox.layout = SandboxLayout("/home/workspace")
        fake_sandbox.workspace.return_value = WorkspaceLayout("/home/workspace")
        fake_registry = create_autospec(MCPRegistry, instance=True)
        fake_registry.connectors = {}
        fake_registry.tools_by_server = {}

        with (
            patch.object(AgentConfig, "get_llm_client", return_value=llm),
            patch(
                "ptc_agent.agent.middleware.compaction.compact.create_llm",
                return_value=llm,
            ),
            patch("ptc_agent.agent.agent.create_agent", side_effect=_fake_create_agent),
        ):
            PTCAgent(cfg).create_agent(
                llm=llm,
                thread_id="t-wiring",
                sandbox=fake_sandbox,
                mcp_registry=fake_registry,
                tool_summary="",
                user_id="u-richard",
                user_tier=user_tier,
                team_memory_enabled=team_memory_enabled,
            )
        return captured[0]["middleware"]

    mw_on = _build_with(True, user_tier="internal")
    turn_on = next(m for m in mw_on if isinstance(m, TurnContextMiddleware))
    assert turn_on._conversation_search_enabled is True

    mw_off = _build_with(False, user_tier="internal")
    turn_off = next(m for m in mw_off if isinstance(m, TurnContextMiddleware))
    assert turn_off._conversation_search_enabled is False

    # External member with team_memory_enabled=True still resolves conversation_search_enabled=False
    mw_ext = _build_with(True, user_tier="external")
    turn_ext = next(m for m in mw_ext if isinstance(m, TurnContextMiddleware))
    assert turn_ext._conversation_search_enabled is False


