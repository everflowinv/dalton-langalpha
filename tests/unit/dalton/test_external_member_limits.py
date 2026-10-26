"""Unit tests for external-member isolation (batch 2, round 2: full isolation).

Round-2 rules, decided by roster tiers inside `sharing.py` before any sharing
row is read: external callers see no colleague workspace at all (404 on both
channels), and external-owned workspaces are visible to admins (read-only) and
nobody else. Also covers the tool-layer refusals (`ingest_company_wiki`,
`conversation_search`, `publish_report`) and fail-closed tier resolution. All
doubles use `unittest.mock.create_autospec`.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import create_autospec

import psycopg
import pytest
from fastapi import HTTPException

import src.dalton.settings as settings_mod
from src.dalton import sharing as dalton_sharing
from src.dalton.access_tiers import get_user_tier
from src.dalton.app import router as dalton_router
from src.dalton.tools import conversation_tools as conv_tools_mod
from src.dalton.tools import publish_tools as publish_tools_mod
from src.dalton.tools import wiki_tools as wiki_tools_mod
from src.server.database import user as user_db

TEAM_EMAILS = "richard.lu@dalton.test,chao.yu@dalton.test"

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
    # rex.yuan is not in TEAM_EMAILS; u-pm resolves to admin via the PM list.
    "u-pm": {
        "user_id": "u-pm",
        "email": "rex.yuan@dalton.test",
        "name": "Rex Yuan (PM)",
    },
    # Invite-signup members: in `dalton_members`, not in DALTON_ALLOWED_EMAILS.
    "u-ext": {
        "user_id": "u-ext",
        "email": "alice01@team.dalton.local",
        "name": "Alice (external)",
    },
    "u-ext2": {
        "user_id": "u-ext2",
        "email": "bob02@team.dalton.local",
        "name": "Bob (external)",
    },
}


def _enable_dalton_auth(
    monkeypatch: pytest.MonkeyPatch, *, patch_users: bool = True
) -> None:
    from src.config import settings as app_settings

    settings_mod.get_dalton_config.cache_clear()
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setattr(app_settings, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(
        app_settings,
        "background_dispatch_requires_token",
        create_autospec(
            app_settings.background_dispatch_requires_token, return_value=False
        ),
    )
    monkeypatch.setenv("DALTON_ALLOWED_EMAILS", TEAM_EMAILS)
    monkeypatch.setenv("DALTON_PM_USER_IDS", "u-pm")
    if not patch_users:
        return

    async def _fake_get_user(uid: str) -> dict[str, Any] | None:
        return USERS_BY_ID.get(uid)

    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(user_db.get_user, side_effect=_fake_get_user),
    )


def _set_sharing(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cos_access: str = "ro",
    web_access: str = "hidden",
) -> None:
    async def _fake_get_sharing(
        workspace_id: str, *, conn: Any = None
    ) -> dict[str, Any]:
        return {
            "workspace_id": workspace_id,
            "cos_access": cos_access,
            "web_access": web_access,
            "updated_by": "u-richard",
            "updated_at": datetime(2026, 10, 26, 12, 0, tzinfo=UTC),
        }

    monkeypatch.setattr(
        dalton_sharing,
        "get_workspace_sharing",
        create_autospec(
            dalton_sharing.get_workspace_sharing, side_effect=_fake_get_sharing
        ),
    )


def _admit_team_members(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `is_team_member` admit invite members (the `dalton_members` arm)."""

    async def _always_member(email: str | None, **kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(
        dalton_sharing,
        "is_team_member",
        create_autospec(dalton_sharing.is_team_member, side_effect=_always_member),
    )


def _sample_workspace(
    *,
    workspace_id: str = "11111111-1111-4111-8111-111111111111",
    user_id: str = "u-richard",
    is_home: bool = False,
    status: str = "running",
) -> dict[str, Any]:
    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    return {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "name": "NVDA Deep Dive",
        "description": None,
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


class TestIsolationMatrix:
    """Caller tier x owner tier x sharing level x read/write x channel.

    Tiers come from the real `get_user_tier` roster (patched users table):
    u-chao internal, u-pm admin, u-ext/u-ext2 external.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", ["web", "cos"])
    @pytest.mark.parametrize("owner", ["u-richard", "u-ext"])
    @pytest.mark.parametrize("level", ["hidden", "ro", "rw"])
    @pytest.mark.parametrize("need", ["read", "write"])
    async def test_external_caller_always_404(
        self,
        monkeypatch: pytest.MonkeyPatch,
        channel: str,
        owner: str,
        level: str,
        need: str,
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _set_sharing(monkeypatch, cos_access=level, web_access=level)
        ws = _sample_workspace(user_id=owner)

        with pytest.raises(HTTPException) as exc:
            await dalton_sharing.require_workspace_access(
                ws, "u-ext2", channel=channel, need=need
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", ["web", "cos"])
    @pytest.mark.parametrize("caller", ["u-chao", "u-ext2"])
    @pytest.mark.parametrize("level", ["hidden", "ro", "rw"])
    @pytest.mark.parametrize("need", ["read", "write"])
    async def test_external_owned_workspace_404_except_admin(
        self,
        monkeypatch: pytest.MonkeyPatch,
        channel: str,
        caller: str,
        level: str,
        need: str,
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _set_sharing(monkeypatch, cos_access=level, web_access=level)
        ws = _sample_workspace(user_id="u-ext")

        with pytest.raises(HTTPException) as exc:
            await dalton_sharing.require_workspace_access(
                ws, caller, channel=channel, need=need
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", ["web", "cos"])
    @pytest.mark.parametrize("level", ["hidden", "ro", "rw"])
    async def test_admin_reads_external_owned_read_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
        channel: str,
        level: str,
    ) -> None:
        """Admin sees an external-owned workspace ro on both channels, whatever
        the (ineffective) sharing settings say; writes are refused."""
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _set_sharing(monkeypatch, cos_access=level, web_access=level)
        ws = _sample_workspace(user_id="u-ext")

        out = await dalton_sharing.require_workspace_access(
            ws, "u-pm", channel=channel, need="read"
        )
        assert out[f"_caller_{channel}_access"] == "ro"

        with pytest.raises(HTTPException) as exc:
            await dalton_sharing.require_workspace_access(
                ws, "u-pm", channel=channel, need="write"
            )
        assert exc.value.status_code == 403
        assert (
            exc.value.detail
            == dalton_sharing.EXTERNAL_OWNED_ADMIN_WRITE_FORBIDDEN_DETAIL
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "caller,level,need,expected_status",
        [
            # Everyone else on an internal-owned workspace: original settings.
            ("u-chao", "rw", "write", None),
            ("u-pm", "rw", "write", None),
            ("u-chao", "ro", "write", 403),
            ("u-chao", "hidden", "read", 404),
        ],
    )
    async def test_internal_owned_workspace_follows_sharing_settings(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caller: str,
        level: str,
        need: str,
        expected_status: int | None,
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _set_sharing(monkeypatch, cos_access=level, web_access=level)
        ws = _sample_workspace(user_id="u-richard")

        if expected_status is None:
            out = await dalton_sharing.require_workspace_access(
                ws, caller, channel="cos", need=need
            )
            assert out["_caller_cos_access"] == level
        else:
            with pytest.raises(HTTPException) as exc:
                await dalton_sharing.require_workspace_access(
                    ws, caller, channel="cos", need=need
                )
            assert exc.value.status_code == expected_status

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", ["web", "cos"])
    async def test_external_owner_unaffected_on_own_workspace(
        self, monkeypatch: pytest.MonkeyPatch, channel: str
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _set_sharing(monkeypatch, cos_access="ro", web_access="hidden")
        own_ws = _sample_workspace(user_id="u-ext")

        out = await dalton_sharing.require_workspace_access(
            own_ws, "u-ext", channel=channel, need="write"
        )
        assert out[f"_caller_{channel}_access"] == "owner"

    @pytest.mark.asyncio
    async def test_thread_access_isolated_for_external(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _set_sharing(monkeypatch, cos_access="rw", web_access="rw")
        meta_mock = create_autospec(
            dalton_sharing.conv_db.get_thread_auth_meta,
            return_value={
                "workspace_id": "11111111-1111-4111-8111-111111111111",
                "user_id": "u-richard",
            },
        )
        monkeypatch.setattr(dalton_sharing.conv_db, "get_thread_auth_meta", meta_mock)
        ws_get_mock = create_autospec(
            dalton_sharing.ws_db.get_workspace,
            return_value=_sample_workspace(user_id="u-richard"),
        )
        monkeypatch.setattr(dalton_sharing.ws_db, "get_workspace", ws_get_mock)

        for need in ("read", "write"):
            with pytest.raises(HTTPException) as exc:
                await dalton_sharing.require_thread_access(
                    "th-1", "u-ext", channel="cos", need=need
                )
            assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_admin_thread_access_on_external_owned_read_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _set_sharing(monkeypatch, cos_access="rw", web_access="rw")
        meta_mock = create_autospec(
            dalton_sharing.conv_db.get_thread_auth_meta,
            return_value={
                "workspace_id": "11111111-1111-4111-8111-111111111111",
                "user_id": "u-ext",
            },
        )
        monkeypatch.setattr(dalton_sharing.conv_db, "get_thread_auth_meta", meta_mock)
        ws_get_mock = create_autospec(
            dalton_sharing.ws_db.get_workspace,
            return_value=_sample_workspace(user_id="u-ext"),
        )
        monkeypatch.setattr(dalton_sharing.ws_db, "get_workspace", ws_get_mock)

        owner_id = await dalton_sharing.require_thread_access(
            "th-1", "u-pm", channel="cos", need="read"
        )
        assert owner_id == "u-ext"
        with pytest.raises(HTTPException) as exc:
            await dalton_sharing.require_thread_access(
                "th-1", "u-pm", channel="cos", need="write"
            )
        assert exc.value.status_code == 403
        assert (
            exc.value.detail
            == dalton_sharing.EXTERNAL_OWNED_ADMIN_WRITE_FORBIDDEN_DETAIL
        )

    @pytest.mark.asyncio
    async def test_knowledge_write_on_colleague_workspace_404(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`knowledge_update` goes through `verify_workspace_access` -> isolation."""
        from src.dalton.tools.knowledge_tools import create_knowledge_tools

        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _set_sharing(monkeypatch, cos_access="rw", web_access="rw")
        ws = _sample_workspace(user_id="u-richard")
        ws_get_mock = create_autospec(
            dalton_sharing.ws_db.get_workspace, return_value=ws
        )
        monkeypatch.setattr(dalton_sharing.ws_db, "get_workspace", ws_get_mock)

        tools = {t.name: t for t in create_knowledge_tools(None)}
        with pytest.raises(HTTPException) as exc:
            await tools["knowledge_update"].ainvoke(
                {
                    "name": "knowledge_update",
                    "id": "tc-1",
                    "type": "tool_call",
                    "args": {
                        "doc": "thesis",
                        "ops": [{"op": "set", "path": "/house_view", "value": "x"}],
                        "reason": "external write attempt",
                        "workspace_id": ws["workspace_id"],
                    },
                },
                config={
                    "configurable": {"user_id": "u-ext", "workspace_id": "own-ws"}
                },
            )
        assert exc.value.status_code == 404


def _fake_list_db(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> None:
    fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    fake_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    fake_cur.fetchall.return_value = rows

    @asynccontextmanager
    async def _cur_ctx(*args: Any, **kwargs: Any):
        yield fake_cur

    @asynccontextmanager
    async def _db_ctx(conn: Any = None):
        yield fake_conn

    fake_conn.cursor.side_effect = _cur_ctx
    monkeypatch.setattr(
        dalton_sharing,
        "get_db_connection",
        create_autospec(dalton_sharing.get_db_connection, side_effect=_db_ctx),
    )


def _list_rows() -> list[dict[str, Any]]:
    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    base = {
        "name": "WS",
        "description": None,
        "status": "running",
        "created_at": now,
        "updated_at": now,
        "last_activity_at": now,
    }
    return [
        {
            **base,
            "workspace_id": "ws-internal-rw",
            "user_id": "u-richard",
            "owner_name": "Richard Lu",
            "owner_email": "richard.lu@dalton.test",
            "web_access": "rw",
            "cos_access": "rw",
        },
        {
            **base,
            "workspace_id": "ws-internal-hidden",
            "user_id": "u-richard",
            "owner_name": "Richard Lu",
            "owner_email": "richard.lu@dalton.test",
            "web_access": "hidden",
            "cos_access": "hidden",
        },
        {
            **base,
            "workspace_id": "ws-external-rw",
            "user_id": "u-ext",
            "owner_name": "Alice (external)",
            "owner_email": "alice01@team.dalton.local",
            "web_access": "rw",
            "cos_access": "rw",
        },
        {
            **base,
            "workspace_id": "ws-external-hidden",
            "user_id": "u-ext",
            "owner_name": "Alice (external)",
            "owner_email": "alice01@team.dalton.local",
            "web_access": "hidden",
            "cos_access": "hidden",
        },
    ]


class TestTeamWorkspaceListIsolation:
    """External callers get empty lists; external-owned rows appear to admins only."""

    @pytest.mark.asyncio
    async def test_external_caller_gets_empty_lists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _fake_list_db(monkeypatch, _list_rows())

        assert await dalton_sharing.list_team_workspaces("u-ext") == []
        assert await dalton_sharing.list_colleague_cos_workspaces("u-ext") == []

    @pytest.mark.asyncio
    async def test_internal_caller_sees_no_external_owned_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _fake_list_db(monkeypatch, _list_rows())

        members = await dalton_sharing.list_team_workspaces("u-chao")
        assert [m["user_id"] for m in members] == ["u-richard"]
        ws_out = {w["workspace_id"]: w for w in members[0]["workspaces"]}
        assert set(ws_out) == {"ws-internal-rw"}  # hidden internal + external owned gone
        assert ws_out["ws-internal-rw"]["web_access"] == "rw"

        cos_items = await dalton_sharing.list_colleague_cos_workspaces("u-chao")
        assert {i["workspace_id"] for i in cos_items} == {"ws-internal-rw"}

    @pytest.mark.asyncio
    async def test_admin_sees_external_owned_rows_read_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Admins see every external-owned workspace as ro, even marked hidden."""
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _fake_list_db(monkeypatch, _list_rows())

        members = await dalton_sharing.list_team_workspaces("u-pm")
        by_owner = {m["user_id"]: m for m in members}
        assert set(by_owner) == {"u-richard", "u-ext"}

        internal_ws = {w["workspace_id"] for w in by_owner["u-richard"]["workspaces"]}
        assert internal_ws == {"ws-internal-rw"}  # internal hidden stays hidden

        external_ws = {
            w["workspace_id"]: w for w in by_owner["u-ext"]["workspaces"]
        }
        assert set(external_ws) == {"ws-external-rw", "ws-external-hidden"}
        for w in external_ws.values():
            assert w["web_access"] == "ro"
            assert w["cos_access"] == "ro"
            assert w["access"] == "ro"

        cos_items = await dalton_sharing.list_colleague_cos_workspaces("u-pm")
        cos_by_id = {i["workspace_id"]: i for i in cos_items}
        assert set(cos_by_id) == {"ws-internal-rw", "ws-external-rw", "ws-external-hidden"}
        for ws_id in ("ws-external-rw", "ws-external-hidden"):
            assert cos_by_id[ws_id]["cos_access"] == "ro"
            assert cos_by_id[ws_id]["web_access"] == "ro"


class TestSharingSettingsEndpoints:
    """GET marks external-owned workspaces (no edit rights); PUT rejects changes."""

    @pytest.mark.asyncio
    async def test_external_owner_gets_notice_and_no_edit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _set_sharing(monkeypatch, cos_access="ro", web_access="rw")
        ws = _sample_workspace(user_id="u-ext")
        ws_get_mock = create_autospec(
            dalton_sharing.ws_db.get_workspace, return_value=ws
        )
        monkeypatch.setattr(dalton_sharing.ws_db, "get_workspace", ws_get_mock)

        res = await dalton_router.get_workspace_sharing_endpoint(
            ws["workspace_id"], user_id="u-ext"
        )
        assert res["owner_external"] is True
        assert res["can_edit"] is False

        from src.dalton.app.router import WorkspaceSharingUpdateRequest

        with pytest.raises(HTTPException) as exc:
            await dalton_router.update_workspace_sharing_endpoint(
                ws["workspace_id"],
                WorkspaceSharingUpdateRequest(cos_access="ro", web_access="ro"),
                user_id="u-ext",
            )
        assert exc.value.status_code == 403
        assert "不提供共享设置" in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_pm_cannot_modify_external_owned_sharing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _set_sharing(monkeypatch, cos_access="ro", web_access="ro")
        ws = _sample_workspace(user_id="u-ext")
        ws_get_mock = create_autospec(
            dalton_sharing.ws_db.get_workspace, return_value=ws
        )
        monkeypatch.setattr(dalton_sharing.ws_db, "get_workspace", ws_get_mock)

        res = await dalton_router.get_workspace_sharing_endpoint(
            ws["workspace_id"], user_id="u-pm"
        )
        assert res["owner_external"] is True
        assert res["can_edit"] is False

        from src.dalton.app.router import WorkspaceSharingUpdateRequest

        with pytest.raises(HTTPException) as exc:
            await dalton_router.update_workspace_sharing_endpoint(
                ws["workspace_id"],
                WorkspaceSharingUpdateRequest(cos_access="rw", web_access="rw"),
                user_id="u-pm",
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_internal_colleague_sharing_get_404_on_external_owned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _set_sharing(monkeypatch, cos_access="rw", web_access="rw")
        ws = _sample_workspace(user_id="u-ext")
        ws_get_mock = create_autospec(
            dalton_sharing.ws_db.get_workspace, return_value=ws
        )
        monkeypatch.setattr(dalton_sharing.ws_db, "get_workspace", ws_get_mock)

        with pytest.raises(HTTPException) as exc:
            await dalton_router.get_workspace_sharing_endpoint(
                ws["workspace_id"], user_id="u-chao"
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_internal_owned_workspace_settings_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _set_sharing(monkeypatch, cos_access="rw", web_access="ro")
        ws = _sample_workspace(user_id="u-richard")
        ws_get_mock = create_autospec(
            dalton_sharing.ws_db.get_workspace, return_value=ws
        )
        monkeypatch.setattr(dalton_sharing.ws_db, "get_workspace", ws_get_mock)

        res = await dalton_router.get_workspace_sharing_endpoint(
            ws["workspace_id"], user_id="u-richard"
        )
        assert res["owner_external"] is False
        assert res["can_edit"] is True


def _patch_wiki_tool_env(monkeypatch: pytest.MonkeyPatch, *, tier: str) -> Any:
    """Patch everything past the external gate; return the bridge mock."""
    monkeypatch.setattr(
        wiki_tools_mod,
        "get_user_tier",
        create_autospec(wiki_tools_mod.get_user_tier, return_value=tier),
    )

    async def _ok_access(workspace_id: str, user_id: str, *args: Any, **kwargs: Any):
        return {"workspace_id": workspace_id, "user_id": user_id}

    monkeypatch.setattr(
        wiki_tools_mod,
        "verify_workspace_access",
        create_autospec(
            wiki_tools_mod.verify_workspace_access, side_effect=_ok_access
        ),
    )
    bridge_mock = create_autospec(wiki_tools_mod.call_bridge_wiki_ingest)
    monkeypatch.setattr(wiki_tools_mod, "call_bridge_wiki_ingest", bridge_mock)
    return bridge_mock


WIKI_ARGS = {
    "target_type": "company",
    "target": "NVDA",
    "content_type": "专家访谈",
    "date": "2026-10-26",
    "content": "Expert call notes.",
}


class TestWikiIngestRefusal:
    """`ingest_company_wiki` refuses external members before any bridge call (preview included)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("confirm", [False, True])
    async def test_external_refused_before_bridge(
        self, monkeypatch: pytest.MonkeyPatch, confirm: bool
    ) -> None:
        bridge_mock = _patch_wiki_tool_env(monkeypatch, tier="external")
        tool_fn = wiki_tools_mod.build_ingest_company_wiki_tool(allow_home_workspace=True)

        raw = await tool_fn.ainvoke(
            {**WIKI_ARGS, "confirm": confirm},
            config={
                "configurable": {
                    "user_id": "u-ext",
                    "workspace_id": "ws-own",
                    "thread_id": "th-1",
                    "user_email": "alice01@team.dalton.local",
                }
            },
        )
        payload = json.loads(raw)
        assert payload["ok"] is False
        assert payload["error_code"] == "external_member_forbidden"
        assert "外部成员不能写入公司 wiki" in payload["error"]
        bridge_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_internal_reaches_bridge_preview(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge_mock = _patch_wiki_tool_env(monkeypatch, tier="internal")
        bridge_mock.return_value = (200, {"ok": True, "status": "dry_run"})
        tool_fn = wiki_tools_mod.build_ingest_company_wiki_tool(allow_home_workspace=True)

        raw = await tool_fn.ainvoke(
            {**WIKI_ARGS, "confirm": False},
            config={
                "configurable": {
                    "user_id": "u-chao",
                    "workspace_id": "ws-own",
                    "thread_id": "th-1",
                    "user_email": "chao.yu@dalton.test",
                }
            },
        )
        assert json.loads(raw)["ok"] is True
        bridge_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_tier_lookup_failure_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raising users-table lookup resolves to external -> refusal (fail closed)."""
        # Do NOT patch get_user_tier here: the real one must swallow the error.
        async def _raise_get_user(uid: str) -> dict[str, Any] | None:
            raise RuntimeError("db down")

        monkeypatch.setattr(
            user_db, "get_user", create_autospec(user_db.get_user, side_effect=_raise_get_user)
        )
        monkeypatch.setenv("DALTON_PM_USER_IDS", "u-pm")
        settings_mod.get_dalton_config.cache_clear()
        assert await get_user_tier("u-ext") == "external"

        bridge_mock = _patch_wiki_tool_env(monkeypatch, tier="external")
        # Restore the real tier gate to exercise the fail-closed path above.
        monkeypatch.setattr(wiki_tools_mod, "get_user_tier", get_user_tier)
        tool_fn = wiki_tools_mod.build_ingest_company_wiki_tool(allow_home_workspace=True)

        raw = await tool_fn.ainvoke(
            {**WIKI_ARGS, "confirm": False},
            config={
                "configurable": {
                    "user_id": "u-ext",
                    "workspace_id": "ws-own",
                    "thread_id": "th-1",
                    "user_email": "alice01@team.dalton.local",
                }
            },
        )
        assert json.loads(raw)["error_code"] == "external_member_forbidden"
        bridge_mock.assert_not_called()


class TestConversationSearchRefusal:
    """`conversation_search` refuses external members; internal members search as before."""

    @pytest.mark.asyncio
    async def test_external_refused_without_search(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            conv_tools_mod,
            "get_user_tier",
            create_autospec(conv_tools_mod.get_user_tier, return_value="external"),
        )
        search_mock = create_autospec(conv_tools_mod.hybrid_search_conversations)
        monkeypatch.setattr(conv_tools_mod, "hybrid_search_conversations", search_mock)
        tool_fn = conv_tools_mod.create_conversation_tools()[0]

        raw = await tool_fn.ainvoke(
            {"query": "NVDA Blackwell", "limit": 5},
            config={"configurable": {"user_id": "u-ext", "thread_id": "th-1"}},
        )
        payload = json.loads(raw)
        assert payload["ok"] is False
        assert payload["error"] == "external_member_forbidden"
        assert (
            payload["message"]
            == conv_tools_mod.EXTERNAL_CONVERSATION_SEARCH_FORBIDDEN_MESSAGE
        )
        assert "全局搜索" in payload["message"]
        search_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_internal_and_admin_still_search(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            conv_tools_mod,
            "get_user_tier",
            create_autospec(conv_tools_mod.get_user_tier, return_value="internal"),
        )
        search_mock = create_autospec(
            conv_tools_mod.hybrid_search_conversations,
            return_value={"ok": True, "count": 0, "cards": []},
        )
        monkeypatch.setattr(conv_tools_mod, "hybrid_search_conversations", search_mock)
        tool_fn = conv_tools_mod.create_conversation_tools()[0]

        raw = await tool_fn.ainvoke(
            {"query": "NVDA Blackwell", "limit": 5},
            config={"configurable": {"user_id": "u-chao", "thread_id": "th-1"}},
        )
        assert json.loads(raw)["ok"] is True
        search_mock.assert_called_once()


class TestPublishReportRefusal:
    """`publish_report` refuses external members before any publish work."""

    @pytest.mark.asyncio
    async def test_external_refused_before_run_publish(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            publish_tools_mod,
            "get_user_tier",
            create_autospec(publish_tools_mod.get_user_tier, return_value="external"),
        )
        run_publish_mock = create_autospec(publish_tools_mod.run_publish)
        monkeypatch.setattr(publish_tools_mod, "run_publish", run_publish_mock)
        tools = {t.name: t for t in publish_tools_mod.create_publish_tools(None)}

        raw = await tools["publish_report"].ainvoke(
            {
                "name": "publish_report",
                "id": "tc-1",
                "type": "tool_call",
                "args": {
                    "draft_path": "drafts/nvda-note.md",
                    "title": "NVDA note",
                    "kind": "event_note",
                    "slug": "nvda-note",
                },
            },
            config={
                "configurable": {
                    "user_id": "u-ext",
                    "workspace_id": "ws-own",
                    "thread_id": "th-1",
                }
            },
        )
        assert "外部成员不能发布正式报告" in raw.content
        assert raw.content.startswith("ERROR:")
        run_publish_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_internal_reaches_run_publish(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            publish_tools_mod,
            "get_user_tier",
            create_autospec(publish_tools_mod.get_user_tier, return_value="internal"),
        )
        run_publish_mock = create_autospec(
            publish_tools_mod.run_publish,
            return_value={"ok": True, "status": "awaiting_pm", "message": "awaiting pm"},
        )
        monkeypatch.setattr(publish_tools_mod, "run_publish", run_publish_mock)
        origin_mock = create_autospec(
            publish_tools_mod._resolve_thread_origin, return_value=(None, None)
        )
        monkeypatch.setattr(publish_tools_mod, "_resolve_thread_origin", origin_mock)
        tools = {t.name: t for t in publish_tools_mod.create_publish_tools(None)}

        raw = await tools["publish_report"].ainvoke(
            {
                "name": "publish_report",
                "id": "tc-1",
                "type": "tool_call",
                "args": {
                    "draft_path": "drafts/nvda-note.md",
                    "title": "NVDA note",
                    "kind": "event_note",
                    "slug": "nvda-note",
                },
            },
            config={
                "configurable": {
                    "user_id": "u-chao",
                    "workspace_id": "ws-own",
                    "thread_id": "th-1",
                    "served_model": "glm-4.7",
                }
            },
        )
        assert raw.content == "awaiting pm"
        run_publish_mock.assert_called_once()


class TestOrderDoesNotLeakExistence:
    """Round 3, item 2: the external-caller 404 fires before the Home/status
    checks, so an external member cannot distinguish any colleague workspace
    state; internal members keep the original 403 semantics."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "ws_kwargs",
        [
            {"is_home": True},
            {"status": "deleted"},
            {"status": "unbound"},
        ],
        ids=["home", "deleted", "unbound"],
    )
    async def test_external_caller_404_before_home_and_status_checks(
        self, monkeypatch: pytest.MonkeyPatch, ws_kwargs: dict[str, Any]
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        ws = _sample_workspace(user_id="u-richard", **ws_kwargs)

        with pytest.raises(HTTPException) as exc:
            await dalton_sharing.require_workspace_access(
                ws, "u-ext2", channel="web", need="read"
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["deleted", "unbound"])
    async def test_internal_caller_keeps_403_on_home_and_deleted(
        self, monkeypatch: pytest.MonkeyPatch, status: str
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard", status=status)
        home_ws = _sample_workspace(user_id="u-richard", is_home=True)

        for target in (ws, home_ws):
            with pytest.raises(HTTPException) as exc:
                await dalton_sharing.require_workspace_access(
                    target, "u-chao", channel="web", need="read"
                )
            assert exc.value.status_code == 403


class TestUnknownOwnerTier:
    """Round 3, item 4: an unresolvable owner tier hides the workspace from
    everyone (admins included) instead of widening admin visibility."""

    @pytest.mark.asyncio
    async def test_admin_cannot_read_workspace_with_unknown_owner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _set_sharing(monkeypatch, cos_access="rw", web_access="rw")
        monkeypatch.setattr(
            dalton_sharing,
            "classify_owner_tier",
            create_autospec(dalton_sharing.classify_owner_tier, return_value="unknown"),
        )
        ws = _sample_workspace(user_id="u-richard")

        for need in ("read", "write"):
            with pytest.raises(HTTPException) as exc:
                await dalton_sharing.require_workspace_access(
                    ws, "u-pm", channel="cos", need=need
                )
            assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_lists_skip_rows_with_unresolvable_owner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
        rows = [
            {
                "workspace_id": "ws-ok",
                "user_id": "u-richard",
                "name": "Internal RW",
                "description": None,
                "status": "running",
                "created_at": now,
                "updated_at": now,
                "last_activity_at": now,
                "owner_name": "Richard Lu",
                "owner_email": "richard.lu@dalton.test",
                "web_access": "rw",
                "cos_access": "rw",
            },
            {
                "workspace_id": "ws-no-email",
                "user_id": "u-ghost",
                "name": "Ghost Owner",
                "description": None,
                "status": "running",
                "created_at": now,
                "updated_at": now,
                "last_activity_at": now,
                "owner_name": None,
                "owner_email": None,
                "web_access": "rw",
                "cos_access": "rw",
            },
        ]
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _fake_list_db(monkeypatch, rows)

        for caller in ("u-chao", "u-pm"):
            members = await dalton_sharing.list_team_workspaces(caller)
            assert [w["workspace_id"] for m in members for w in m["workspaces"]] == [
                "ws-ok"
            ]
            items = await dalton_sharing.list_colleague_cos_workspaces(caller)
            assert [i["workspace_id"] for i in items] == ["ws-ok"]

    @pytest.mark.asyncio
    async def test_classify_owner_tier_fails_to_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.access_tiers import classify_owner_tier

        real_get_user = user_db.get_user

        async def _raise_get_user(uid: str) -> dict[str, Any] | None:
            raise RuntimeError("db down")

        monkeypatch.setattr(
            user_db,
            "get_user",
            create_autospec(real_get_user, side_effect=_raise_get_user),
        )
        monkeypatch.setenv("DALTON_PM_USER_IDS", "u-pm")
        settings_mod.get_dalton_config.cache_clear()
        assert await classify_owner_tier("u-richard") == "unknown"

        monkeypatch.setattr(
            user_db, "get_user", create_autospec(real_get_user, return_value=None)
        )
        assert await classify_owner_tier("u-ghost") == "unknown"  # no row: unknown
        assert await classify_owner_tier("") == "unknown"

    @pytest.mark.asyncio
    async def test_lists_do_not_lookup_owner_tiers_per_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round 3, item 5: owner tiers come from the joined email, never from
        a per-owner `get_user` call (N+1 regression guard)."""
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _fake_list_db(monkeypatch, _list_rows())

        await dalton_sharing.list_team_workspaces("u-chao")
        await dalton_sharing.list_colleague_cos_workspaces("u-chao")

        looked_up = {call.args[0] for call in user_db.get_user.call_args_list}
        assert looked_up == {"u-chao"}  # caller only, never an owner


class TestSharingPutExistenceLeak:
    """Round 3, item 3: non-owner external callers and internal callers on
    external-owned workspaces get 404, not 403."""

    @pytest.mark.asyncio
    async def test_external_non_owner_put_gets_404(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")
        monkeypatch.setattr(
            dalton_sharing.ws_db,
            "get_workspace",
            create_autospec(dalton_sharing.ws_db.get_workspace, return_value=ws),
        )
        from src.dalton.app.router import WorkspaceSharingUpdateRequest

        with pytest.raises(HTTPException) as exc:
            await dalton_router.update_workspace_sharing_endpoint(
                ws["workspace_id"],
                WorkspaceSharingUpdateRequest(cos_access="rw", web_access="rw"),
                user_id="u-ext2",
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_internal_non_owner_put_on_external_owned_gets_404(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        ws = _sample_workspace(user_id="u-ext")
        monkeypatch.setattr(
            dalton_sharing.ws_db,
            "get_workspace",
            create_autospec(dalton_sharing.ws_db.get_workspace, return_value=ws),
        )
        from src.dalton.app.router import WorkspaceSharingUpdateRequest

        with pytest.raises(HTTPException) as exc:
            await dalton_router.update_workspace_sharing_endpoint(
                ws["workspace_id"],
                WorkspaceSharingUpdateRequest(cos_access="rw", web_access="rw"),
                user_id="u-chao",
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_internal_non_owner_put_keeps_403(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")
        monkeypatch.setattr(
            dalton_sharing.ws_db,
            "get_workspace",
            create_autospec(dalton_sharing.ws_db.get_workspace, return_value=ws),
        )
        from src.dalton.app.router import WorkspaceSharingUpdateRequest

        with pytest.raises(HTTPException) as exc:
            await dalton_router.update_workspace_sharing_endpoint(
                ws["workspace_id"],
                WorkspaceSharingUpdateRequest(cos_access="rw", web_access="rw"),
                user_id="u-chao",
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_external_caller_get_on_foreign_home_gets_404(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        home_ws = _sample_workspace(user_id="u-richard", is_home=True)
        monkeypatch.setattr(
            dalton_sharing.ws_db,
            "get_workspace",
            create_autospec(dalton_sharing.ws_db.get_workspace, return_value=home_ws),
        )

        with pytest.raises(HTTPException) as exc:
            await dalton_router.get_workspace_sharing_endpoint(
                home_ws["workspace_id"], user_id="u-ext2"
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_unknown_owner_sharing_get_404_even_for_admin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.dalton.access_tiers as access_tiers_mod

        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")
        monkeypatch.setattr(
            dalton_sharing.ws_db,
            "get_workspace",
            create_autospec(dalton_sharing.ws_db.get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            access_tiers_mod,
            "classify_owner_tier",
            create_autospec(
                access_tiers_mod.classify_owner_tier, return_value="unknown"
            ),
        )

        with pytest.raises(HTTPException) as exc:
            await dalton_router.get_workspace_sharing_endpoint(
                ws["workspace_id"], user_id="u-pm"
            )
        assert exc.value.status_code == 404


class TestTrackingExternalOwned:
    """Round 3, item 1: admins are read-only on external-owned workspaces, so
    tracking toggles stay with the owner; GET reports can_edit=False."""

    @staticmethod
    def _patch_tracking_env(
        monkeypatch: pytest.MonkeyPatch, *, owner: str
    ) -> dict[str, Any]:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        # Internal-owned workspaces fall through to the sharing row lookup.
        _set_sharing(monkeypatch, cos_access="rw", web_access="rw")
        ws = _sample_workspace(user_id=owner)
        monkeypatch.setattr(
            dalton_sharing.ws_db,
            "get_workspace",
            create_autospec(dalton_sharing.ws_db.get_workspace, return_value=ws),
        )
        from src.dalton.autonomy import tracking as tracking_mod

        monkeypatch.setattr(
            tracking_mod,
            "get_tracking_status",
            create_autospec(
                tracking_mod.get_tracking_status,
                return_value={"status": "enabled", "enabled": True},
            ),
        )
        monkeypatch.setattr(
            tracking_mod,
            "enable_tracking",
            create_autospec(
                tracking_mod.enable_tracking,
                return_value={"status": "enabled", "enabled": True},
            ),
        )
        return ws

    @pytest.mark.asyncio
    async def test_pm_get_external_owned_reports_no_edit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = self._patch_tracking_env(monkeypatch, owner="u-ext")

        res = await dalton_router.get_workspace_tracking_endpoint(
            ws["workspace_id"], user_id="u-pm"
        )
        assert res["can_edit"] is False

    @pytest.mark.asyncio
    async def test_pm_post_external_owned_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.app.router import WorkspaceTrackingUpdateRequest

        ws = self._patch_tracking_env(monkeypatch, owner="u-ext")

        with pytest.raises(HTTPException) as exc:
            await dalton_router.update_workspace_tracking_endpoint(
                ws["workspace_id"],
                WorkspaceTrackingUpdateRequest(enabled=False),
                user_id="u-pm",
            )
        assert exc.value.status_code == 403
        assert (
            exc.value.detail
            == dalton_sharing.EXTERNAL_OWNED_ADMIN_WRITE_FORBIDDEN_DETAIL
        )

    @pytest.mark.asyncio
    async def test_external_owner_still_toggles_own_tracking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.app.router import WorkspaceTrackingUpdateRequest

        ws = self._patch_tracking_env(monkeypatch, owner="u-ext")

        res = await dalton_router.update_workspace_tracking_endpoint(
            ws["workspace_id"],
            WorkspaceTrackingUpdateRequest(enabled=True),
            user_id="u-ext",
        )
        assert res["can_edit"] is True

    @pytest.mark.asyncio
    async def test_pm_get_internal_owned_still_editable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = self._patch_tracking_env(monkeypatch, owner="u-richard")

        res = await dalton_router.get_workspace_tracking_endpoint(
            ws["workspace_id"], user_id="u-pm"
        )
        assert res["can_edit"] is True

    @pytest.mark.asyncio
    async def test_unknown_owner_tracking_404_for_admin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.dalton.access_tiers as access_tiers_mod

        ws = self._patch_tracking_env(monkeypatch, owner="u-richard")
        monkeypatch.setattr(
            access_tiers_mod,
            "classify_owner_tier",
            create_autospec(
                access_tiers_mod.classify_owner_tier, return_value="unknown"
            ),
        )

        from src.dalton.app.router import WorkspaceTrackingUpdateRequest

        with pytest.raises(HTTPException) as exc:
            await dalton_router.update_workspace_tracking_endpoint(
                ws["workspace_id"],
                WorkspaceTrackingUpdateRequest(enabled=True),
                user_id="u-pm",
            )
        assert exc.value.status_code == 404


class TestSingleOwnerClassificationPerRequest:
    """Round 4, item 1: GET/PUT classify the owner once and reuse the result,
    so a flaky roster lookup cannot let an admin write external-owned sharing
    (TOCTOU: first call external, second call unknown)."""

    @staticmethod
    def _flaky_classify(monkeypatch: pytest.MonkeyPatch, access_tiers_mod: Any) -> Any:
        mock = create_autospec(
            access_tiers_mod.classify_owner_tier,
            side_effect=["external", "unknown"],
        )
        monkeypatch.setattr(access_tiers_mod, "classify_owner_tier", mock)
        return mock

    @pytest.mark.asyncio
    async def test_admin_put_does_not_write_when_classification_flips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.dalton.access_tiers as access_tiers_mod

        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        ws = _sample_workspace(user_id="u-ext")
        monkeypatch.setattr(
            dalton_sharing.ws_db,
            "get_workspace",
            create_autospec(dalton_sharing.ws_db.get_workspace, return_value=ws),
        )
        classify_mock = self._flaky_classify(monkeypatch, access_tiers_mod)
        upsert_mock = create_autospec(dalton_sharing.upsert_workspace_sharing)
        monkeypatch.setattr(
            dalton_sharing, "upsert_workspace_sharing", upsert_mock
        )
        from src.dalton.app.router import WorkspaceSharingUpdateRequest

        with pytest.raises(HTTPException) as exc:
            await dalton_router.update_workspace_sharing_endpoint(
                ws["workspace_id"],
                WorkspaceSharingUpdateRequest(cos_access="rw", web_access="rw"),
                user_id="u-pm",
            )
        assert exc.value.status_code == 403
        assert classify_mock.call_count == 1  # single classification per request
        upsert_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_admin_get_keeps_owner_external_flag_when_classification_flips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.dalton.access_tiers as access_tiers_mod

        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _set_sharing(monkeypatch, cos_access="ro", web_access="rw")
        ws = _sample_workspace(user_id="u-ext")
        monkeypatch.setattr(
            dalton_sharing.ws_db,
            "get_workspace",
            create_autospec(dalton_sharing.ws_db.get_workspace, return_value=ws),
        )
        classify_mock = self._flaky_classify(monkeypatch, access_tiers_mod)

        res = await dalton_router.get_workspace_sharing_endpoint(
            ws["workspace_id"], user_id="u-pm"
        )
        assert res["owner_external"] is True
        assert res["can_edit"] is False
        assert classify_mock.call_count == 1


class TestClassifierConsistency:
    """Round 4, item 2: the three classifiers share one rule set (PM first,
    normalized allowlisted email -> internal, else external) and differ only
    in failure mode: caller-side fail-closed vs owner-side unknown."""

    PM_UID = "u-pm"
    OWNER_UID = "u-owner"

    def _owner_row(self, email: str | None) -> dict[str, Any]:
        return {"user_id": self.OWNER_UID, "email": email}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "email,pm,expected_caller,expected_owner,expected_row",
        [
            # PM wins regardless of email.
            ("invitee@team.dalton.local", True, "admin", "admin", "admin"),
            # Uppercase and padded emails normalize to the allowlist entry.
            ("Chao.Yu@Dalton.Test", False, "internal", "internal", "internal"),
            ("  chao.yu@dalton.test  ", False, "internal", "internal", "internal"),
            # Empty email: caller fails closed to external, owner is unknown.
            ("", False, "external", "unknown", "unknown"),
            (None, False, "external", "unknown", "unknown"),
            # Off-allowlist email is external everywhere.
            ("invitee@team.dalton.local", False, "external", "external", "external"),
        ],
    )
    async def test_rule_table(
        self,
        monkeypatch: pytest.MonkeyPatch,
        email: str | None,
        pm: bool,
        expected_caller: str,
        expected_owner: str,
        expected_row: str,
    ) -> None:
        import src.dalton.access_tiers as access_tiers_mod

        _enable_dalton_auth(monkeypatch, patch_users=False)  # roster env only
        # Make the PM arm match the row exactly for the pm=True case.
        monkeypatch.setenv(
            "DALTON_PM_USER_IDS", self.OWNER_UID if pm else "u-other-pm"
        )
        settings_mod.get_dalton_config.cache_clear()
        row = self._owner_row(email)
        monkeypatch.setattr(
            user_db,
            "get_user",
            create_autospec(user_db.get_user, return_value=row),
        )
        assert await access_tiers_mod.get_user_tier(self.OWNER_UID) == expected_caller
        assert (
            await access_tiers_mod.classify_owner_tier(self.OWNER_UID) == expected_owner
        )
        assert (
            dalton_sharing._classify_owner_row_tier(self.OWNER_UID, email)
            == expected_row
        )

    @pytest.mark.asyncio
    async def test_pm_check_failure_diverges_by_side(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raising PM check: the caller check falls through (unchanged
        semantics), the owner checks return unknown."""
        import src.dalton.access_tiers as access_tiers_mod

        _enable_dalton_auth(monkeypatch, patch_users=False)
        monkeypatch.setattr(
            user_db,
            "get_user",
            create_autospec(
                user_db.get_user,
                return_value=self._owner_row("chao.yu@dalton.test"),
            ),
        )
        for module in (access_tiers_mod, dalton_sharing):
            monkeypatch.setattr(
                module,
                "is_invite_pm",
                create_autospec(
                    module.is_invite_pm, side_effect=RuntimeError("roster read failed")
                ),
            )
        assert await access_tiers_mod.get_user_tier(self.OWNER_UID) == "internal"
        assert await access_tiers_mod.classify_owner_tier(self.OWNER_UID) == "unknown"
        assert (
            dalton_sharing._classify_owner_row_tier(
                self.OWNER_UID, "chao.yu@dalton.test"
            )
            == "unknown"
        )

    @pytest.mark.asyncio
    async def test_allowlist_read_failure_diverges_by_side(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.dalton.access_tiers as access_tiers_mod

        _enable_dalton_auth(monkeypatch, patch_users=False)
        monkeypatch.setattr(
            user_db,
            "get_user",
            create_autospec(
                user_db.get_user,
                return_value=self._owner_row("chao.yu@dalton.test"),
            ),
        )
        for module in (access_tiers_mod, dalton_sharing):
            monkeypatch.setattr(
                module,
                "get_dalton_allowed_emails",
                create_autospec(
                    module.get_dalton_allowed_emails,
                    side_effect=RuntimeError("env read failed"),
                ),
            )
        assert await access_tiers_mod.get_user_tier(self.OWNER_UID) == "external"
        assert await access_tiers_mod.classify_owner_tier(self.OWNER_UID) == "unknown"
        assert (
            dalton_sharing._classify_owner_row_tier(
                self.OWNER_UID, "chao.yu@dalton.test"
            )
            == "unknown"
        )


class TestOwnerIsolationBeforeStatusChecks:
    """Round 4, item 3: owner isolation runs before the Home/status 403 and
    the owner membership check, on the real chain (only `get_user` and the
    workspace row are doubled)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("caller", ["u-chao", "u-pm"])
    @pytest.mark.parametrize(
        "ws_kwargs",
        [{"is_home": True}, {"status": "deleted"}, {"status": "unbound"}],
        ids=["home", "deleted", "unbound"],
    )
    async def test_external_owned_states_404_for_every_non_owner(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caller: str,
        ws_kwargs: dict[str, Any],
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        ws = _sample_workspace(user_id="u-ext", **ws_kwargs)

        with pytest.raises(HTTPException) as exc:
            await dalton_sharing.require_workspace_access(
                ws, caller, channel="web", need="read"
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "owner_row,patch_mode",
        [
            (None, "missing"),
            ({"user_id": "u-ghost", "email": "   "}, "empty_email"),
            (None, "raise"),
        ],
        ids=["missing_row", "empty_email", "get_user_raises"],
    )
    async def test_unresolvable_owner_404_before_owner_membership_check(
        self,
        monkeypatch: pytest.MonkeyPatch,
        owner_row: dict[str, Any] | None,
        patch_mode: str,
    ) -> None:
        """Real classify_owner_tier + require_team_reader; only get_user is
        doubled. The 404 (not 403/503 from require_team_reader) proves the
        owner classification fires first."""
        _enable_dalton_auth(monkeypatch, patch_users=False)

        async def _fake_get_user(uid: str) -> dict[str, Any] | None:
            if uid == "u-ghost":
                if patch_mode == "raise":
                    raise RuntimeError("db down")
                return owner_row
            return USERS_BY_ID.get(uid)

        monkeypatch.setattr(
            user_db,
            "get_user",
            create_autospec(user_db.get_user, side_effect=_fake_get_user),
        )
        ws = _sample_workspace(user_id="u-ghost")

        for need in ("read", "write"):
            with pytest.raises(HTTPException) as exc:
                await dalton_sharing.require_workspace_access(
                    ws, "u-chao", channel="cos", need=need
                )
            assert exc.value.status_code == 404
