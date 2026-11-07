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
        assert "说“证据不足/无法确定”之前，先对照下方数据地图看还有哪些相关来源没查" in prompt


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
    assert "first call `conversation_search` to check the team's prior views" in cos_prompt_on
    assert "回答某家公司或主题时，先用 `conversation_search`" in cos_prompt_on
    assert "归档统计：" in cos_prompt_on
