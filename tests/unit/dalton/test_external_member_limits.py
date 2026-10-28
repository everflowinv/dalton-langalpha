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
    _set_sharing(monkeypatch)
    if not patch_users:
        return

    async def _fake_get_user(uid: str) -> dict[str, Any] | None:
        return USERS_BY_ID.get(uid)

    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(user_db.get_user, side_effect=_fake_get_user),
    )


_ORIG_GET_WORKSPACE_SHARING = dalton_sharing.get_workspace_sharing


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
            _ORIG_GET_WORKSPACE_SHARING, side_effect=_fake_get_sharing
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
    @pytest.mark.parametrize("caller", ["u-chao", "u-pm", "u-ext2"])
    @pytest.mark.parametrize("level", ["hidden", "ro", "rw"])
    @pytest.mark.parametrize("need", ["read", "write"])
    async def test_external_owned_workspace_cos_always_404(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caller: str,
        level: str,
        need: str,
    ) -> None:
        """Chief of Staff (cos) channel is always 404 on external-owned workspaces,
        including for admins."""
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _set_sharing(monkeypatch, cos_access=level, web_access=level)
        ws = _sample_workspace(user_id="u-ext")

        with pytest.raises(HTTPException) as exc:
            await dalton_sharing.require_workspace_access(
                ws, caller, channel="cos", need=need
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("caller", ["u-chao", "u-pm"])
    @pytest.mark.parametrize(
        "web_level,need,expected_status",
        [
            ("hidden", "read", 404),
            ("hidden", "write", 404),
            ("ro", "read", None),
            ("ro", "write", 403),
            ("rw", "read", None),
            ("rw", "write", None),
        ],
    )
    async def test_external_owned_workspace_web_follows_sharing_for_internal_and_admin(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caller: str,
        web_level: str,
        need: str,
        expected_status: int | None,
    ) -> None:
        """External-owned workspaces are hidden by default on web (even to admins),
        and accessible to internal/admin callers when the external owner enables
        web_access ('ro' or 'rw')."""
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _set_sharing(monkeypatch, cos_access="rw", web_access=web_level)
        ws = _sample_workspace(user_id="u-ext")

        if expected_status is None:
            out = await dalton_sharing.require_workspace_access(
                ws, caller, channel="web", need=need
            )
            assert out["_caller_web_access"] == web_level
        else:
            with pytest.raises(HTTPException) as exc:
                await dalton_sharing.require_workspace_access(
                    ws, caller, channel="web", need=need
                )
            assert exc.value.status_code == expected_status

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
    async def test_admin_and_internal_thread_access_on_external_owned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
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

        # Default hidden -> 404 for both internal and admin on web & cos
        _set_sharing(monkeypatch, cos_access="rw", web_access="hidden")
        for caller in ("u-chao", "u-pm"):
            for ch in ("web", "cos"):
                with pytest.raises(HTTPException) as exc:
                    await dalton_sharing.require_thread_access(
                        "th-1", caller, channel=ch, need="read"
                    )
                assert exc.value.status_code == 404

        # External owner enables web_access='ro' -> web read allowed for internal & admin, cos still 404
        _set_sharing(monkeypatch, cos_access="rw", web_access="ro")
        for caller in ("u-chao", "u-pm"):
            owner_id = await dalton_sharing.require_thread_access(
                "th-1", caller, channel="web", need="read"
            )
            assert owner_id == "u-ext"
            with pytest.raises(HTTPException) as exc:
                await dalton_sharing.require_thread_access(
                    "th-1", caller, channel="web", need="write"
                )
            assert exc.value.status_code == 403
            with pytest.raises(HTTPException) as exc:
                await dalton_sharing.require_thread_access(
                    "th-1", caller, channel="cos", need="read"
                )
            assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_external_caller_denied_foreign_workspace_thread_even_if_thread_owner(
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
                "thread_user_id": "u-ext",
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
                    "th-ext-in-richard", "u-ext", channel="web", need=need
                )
            assert exc.value.status_code == 404

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
    @pytest.mark.parametrize("caller", ["u-chao", "u-pm"])
    async def test_internal_and_admin_see_external_owned_only_when_web_shared(
        self, monkeypatch: pytest.MonkeyPatch, caller: str
    ) -> None:
        """Both internal members and admins see external-owned workspaces in
        list_team_workspaces only when web_access != 'hidden' (with cos_access
        clamped to 'hidden'), and never in list_colleague_cos_workspaces."""
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _fake_list_db(monkeypatch, _list_rows())

        members = await dalton_sharing.list_team_workspaces(caller)
        by_owner = {m["user_id"]: m for m in members}
        assert set(by_owner) == {"u-richard", "u-ext"}

        internal_ws = {
            w["workspace_id"]: w for w in by_owner["u-richard"]["workspaces"]
        }
        assert set(internal_ws) == {"ws-internal-rw"}
        assert internal_ws["ws-internal-rw"]["web_access"] == "rw"

        external_ws = {
            w["workspace_id"]: w for w in by_owner["u-ext"]["workspaces"]
        }
        assert set(external_ws) == {"ws-external-rw"}  # ws-external-hidden stays hidden
        assert external_ws["ws-external-rw"]["web_access"] == "rw"
        assert external_ws["ws-external-rw"]["cos_access"] == "hidden"
        assert external_ws["ws-external-rw"]["access"] == "rw"

        cos_items = await dalton_sharing.list_colleague_cos_workspaces(caller)
        assert {i["workspace_id"] for i in cos_items} == {"ws-internal-rw"}


class TestSharingSettingsEndpoints:
    """External owner can edit web_access (never cos_access); non-owners cannot edit."""

    @pytest.mark.asyncio
    async def test_external_owner_can_edit_web_access_but_not_cos(
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
        assert res["can_edit"] is True
        assert res["cos_access"] == "hidden"
        assert res["web_access"] == "rw"

        from src.dalton.app.router import WorkspaceSharingUpdateRequest

        async def _fake_upsert(
            workspace_id: str,
            *,
            cos_access: str | None = None,
            web_access: str | None = None,
            updated_by: str | None = None,
        ) -> dict[str, Any]:
            return {
                "workspace_id": workspace_id,
                "cos_access": cos_access or "hidden",
                "web_access": web_access or "hidden",
                "updated_by": updated_by,
                "updated_at": datetime(2026, 10, 26, 12, 0, tzinfo=UTC),
            }

        monkeypatch.setattr(
            dalton_sharing,
            "upsert_workspace_sharing",
            create_autospec(
                dalton_sharing.upsert_workspace_sharing, side_effect=_fake_upsert
            ),
        )

        updated = await dalton_router.update_workspace_sharing_endpoint(
            ws["workspace_id"],
            WorkspaceSharingUpdateRequest(web_access="ro"),
            user_id="u-ext",
        )
        assert updated["web_access"] == "ro"
        assert updated["cos_access"] == "hidden"
        assert updated["can_edit"] is True
        assert updated["owner_external"] is True

        with pytest.raises(HTTPException) as exc:
            await dalton_router.update_workspace_sharing_endpoint(
                ws["workspace_id"],
                WorkspaceSharingUpdateRequest(cos_access="ro", web_access="ro"),
                user_id="u-ext",
            )
        assert exc.value.status_code == 403
        assert "研究总监" in str(exc.value.detail)

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
        assert res["cos_access"] == "hidden"

        from src.dalton.app.router import WorkspaceSharingUpdateRequest

        with pytest.raises(HTTPException) as exc:
            await dalton_router.update_workspace_sharing_endpoint(
                ws["workspace_id"],
                WorkspaceSharingUpdateRequest(cos_access="rw", web_access="rw"),
                user_id="u-pm",
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_internal_and_admin_sharing_get_404_when_external_owned_hidden(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _set_sharing(monkeypatch, cos_access="rw", web_access="hidden")
        ws = _sample_workspace(user_id="u-ext")
        ws_get_mock = create_autospec(
            dalton_sharing.ws_db.get_workspace, return_value=ws
        )
        monkeypatch.setattr(dalton_sharing.ws_db, "get_workspace", ws_get_mock)

        for caller in ("u-chao", "u-pm"):
            with pytest.raises(HTTPException) as exc:
                await dalton_router.get_workspace_sharing_endpoint(
                    ws["workspace_id"], user_id=caller
                )
            assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_internal_and_admin_sharing_put_404_when_external_owned_hidden(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.app.router import WorkspaceSharingUpdateRequest

        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        _set_sharing(monkeypatch, cos_access="rw", web_access="hidden")
        ws = _sample_workspace(user_id="u-ext")
        ws_get_mock = create_autospec(
            dalton_sharing.ws_db.get_workspace, return_value=ws
        )
        monkeypatch.setattr(dalton_sharing.ws_db, "get_workspace", ws_get_mock)

        for caller in ("u-chao", "u-pm"):
            with pytest.raises(HTTPException) as exc:
                await dalton_router.update_workspace_sharing_endpoint(
                    ws["workspace_id"],
                    WorkspaceSharingUpdateRequest(cos_access="rw", web_access="rw"),
                    user_id=caller,
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
        _set_sharing(monkeypatch, cos_access="ro", web_access="rw")
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


class TestCosForeignWorkspaceThreadIsolation:
    """Chief of Staff ('cos') cannot list, read, stop, or delete a thread created
    by internal user A inside an external member's workspace (or an internal
    colleague's workspace with cos_rw_enabled=False), while A can still read/write
    that thread on the web channel."""

    @pytest.mark.asyncio
    async def test_cos_cannot_access_creator_thread_in_external_or_cos_disabled_workspace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.server.database.conversation import threads_read
        from src.tools.secretary import chief_of_staff as cos_mod
        from src.tools.secretary import tools as sec_tools

        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        monkeypatch.setenv("DALTON_COLLEAGUE_DISPATCH_ENABLED", "true")
        settings_mod.get_dalton_config.cache_clear()

        ext_ws_id = "11111111-1111-4111-8111-111111111111"
        own_ws_id = "22222222-2222-4222-8222-222222222222"
        colleague_ws_id = "33333333-3333-4333-8333-333333333333"
        ext_tid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        own_tid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        colleague_tid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        cos_own_tid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

        ext_ws = _sample_workspace(workspace_id=ext_ws_id, user_id="u-ext")
        own_ws = _sample_workspace(workspace_id=own_ws_id, user_id="u-chao")
        colleague_ws = _sample_workspace(
            workspace_id=colleague_ws_id, user_id="u-richard"
        )
        ws_by_id = {
            ext_ws_id: ext_ws,
            own_ws_id: own_ws,
            colleague_ws_id: colleague_ws,
        }

        async def _fake_get_ws(
            wid: str, *, include_deleted: bool = False
        ) -> dict[str, Any] | None:
            return ws_by_id.get(str(wid))

        monkeypatch.setattr(
            dalton_sharing.ws_db,
            "get_workspace",
            create_autospec(
                dalton_sharing.ws_db.get_workspace, side_effect=_fake_get_ws
            ),
        )

        # External workspace has web_access='rw'; internal colleague Richard has cos_rw_enabled toggled.
        cos_rw_state = {"u-richard": True}
        web_state = {ext_ws_id: "rw", colleague_ws_id: "rw"}

        async def _fake_get_sharing(
            workspace_id: str, *, conn: Any = None
        ) -> dict[str, Any]:
            return {
                "workspace_id": workspace_id,
                "cos_access": "rw",
                "web_access": web_state.get(str(workspace_id), "hidden"),
                "updated_by": "u-ext",
                "updated_at": datetime(2026, 10, 26, 12, 0, tzinfo=UTC),
            }

        async def _fake_get_user_cos(
            owner_id: str, *, conn: Any = None
        ) -> dict[str, Any]:
            return {
                "user_id": owner_id,
                "cos_rw_enabled": cos_rw_state.get(str(owner_id), True),
                "updated_at": None,
            }

        monkeypatch.setattr(
            dalton_sharing,
            "get_workspace_sharing",
            create_autospec(
                _ORIG_GET_WORKSPACE_SHARING, side_effect=_fake_get_sharing
            ),
        )
        monkeypatch.setattr(
            dalton_sharing,
            "get_user_cos_sharing",
            create_autospec(
                dalton_sharing.get_user_cos_sharing,
                side_effect=_fake_get_user_cos,
            ),
        )

        meta_by_tid = {
            ext_tid: {
                "conversation_thread_id": ext_tid,
                "workspace_id": ext_ws_id,
                "user_id": "u-chao",
                "thread_user_id": "u-chao",
                "workspace_owner_id": "u-ext",
                "workspace_status": "running",
            },
            own_tid: {
                "conversation_thread_id": own_tid,
                "workspace_id": own_ws_id,
                "user_id": "u-chao",
                "thread_user_id": "u-chao",
                "workspace_owner_id": "u-chao",
                "workspace_status": "running",
            },
            colleague_tid: {
                "conversation_thread_id": colleague_tid,
                "workspace_id": colleague_ws_id,
                "user_id": "u-chao",
                "thread_user_id": "u-chao",
                "workspace_owner_id": "u-richard",
                "workspace_status": "running",
            },
        }

        async def _fake_owner_id(tid: str) -> str | None:
            row = meta_by_tid.get(str(tid))
            return str(row["thread_user_id"]) if row else None

        async def _fake_auth_meta(
            tid: str, *, conn: Any = None
        ) -> dict[str, Any] | None:
            return meta_by_tid.get(str(tid))

        monkeypatch.setattr(
            threads_read,
            "get_thread_owner_id",
            create_autospec(
                threads_read.get_thread_owner_id, side_effect=_fake_owner_id
            ),
        )
        monkeypatch.setattr(
            threads_read,
            "get_thread_auth_meta",
            create_autospec(
                threads_read.get_thread_auth_meta, side_effect=_fake_auth_meta
            ),
        )
        monkeypatch.setattr(
            dalton_sharing.conv_db,
            "get_thread_auth_meta",
            create_autospec(
                dalton_sharing.conv_db.get_thread_auth_meta,
                side_effect=_fake_auth_meta,
            ),
        )

        list_rows = [
            {
                "conversation_thread_id": ext_tid,
                "workspace_id": ext_ws_id,
                "thread_user_id": "u-chao",
                "workspace_owner_id": "u-ext",
                "title": "Thread in External WS",
                "current_status": "completed",
            },
            {
                "conversation_thread_id": colleague_tid,
                "workspace_id": colleague_ws_id,
                "thread_user_id": "u-chao",
                "workspace_owner_id": "u-richard",
                "title": "Thread in Colleague WS",
                "current_status": "completed",
            },
            {
                "conversation_thread_id": own_tid,
                "workspace_id": own_ws_id,
                "thread_user_id": "u-chao",
                "workspace_owner_id": "u-chao",
                "title": "Thread in Own WS",
                "current_status": "completed",
            },
        ]

        async def _fake_list_cos_ws(uid: str) -> list[dict[str, Any]]:
            if cos_rw_state.get("u-richard", True):
                return [{"workspace_id": colleague_ws_id, "user_id": "u-richard"}]
            return []

        monkeypatch.setattr(
            dalton_sharing,
            "list_colleague_cos_workspaces",
            create_autospec(
                dalton_sharing.list_colleague_cos_workspaces,
                side_effect=_fake_list_cos_ws,
            ),
        )

        executed_sql: list[tuple[str, Any]] = []
        cursor = create_autospec(psycopg.AsyncCursor, instance=True)

        def _filter_rows_by_sql(sql: str, params: Any) -> list[dict[str, Any]]:
            assert "AND (w.user_id = %s OR w.workspace_id = ANY(%s::uuid[]))" in sql
            uid = str(params[3])
            cos_ids = set(params[4])
            return [
                r
                for r in list_rows
                if r["workspace_owner_id"] == uid or r["workspace_id"] in cos_ids
            ]

        async def _fake_execute(sql: str, params: Any = None) -> None:
            executed_sql.append((sql, params))

        async def _fake_fetchone() -> dict[str, Any]:
            sql, params = executed_sql[-1]
            return {"total": len(_filter_rows_by_sql(sql, params))}

        async def _fake_fetchall() -> list[dict[str, Any]]:
            sql, params = executed_sql[-1]
            filtered = _filter_rows_by_sql(sql, params)
            limit_val = int(params[-2])
            offset_val = int(params[-1])
            return filtered[offset_val : offset_val + limit_val]

        cursor.execute.side_effect = _fake_execute
        cursor.fetchone.side_effect = _fake_fetchone
        cursor.fetchall.side_effect = _fake_fetchall
        conn = create_autospec(psycopg.AsyncConnection, instance=True)

        @asynccontextmanager
        async def _cursor_cm(*args: Any, **kwargs: Any):
            yield cursor

        conn.cursor.side_effect = _cursor_cm

        @asynccontextmanager
        async def _db_cm(existing_conn: Any = None):
            yield existing_conn or conn

        monkeypatch.setattr(
            threads_read.pool,
            "get_db_connection",
            create_autospec(
                threads_read.pool.get_db_connection, side_effect=_db_cm
            ),
        )

        # 1. On the web channel, internal user A ('u-chao') can read and write T in external workspace (web_access='rw')
        assert (
            await dalton_sharing.require_thread_access(
                ext_tid, "u-chao", channel="web", need="read"
            )
            == "u-chao"
        )
        assert (
            await dalton_sharing.require_thread_access(
                ext_tid, "u-chao", channel="web", need="write"
            )
            == "u-chao"
        )

        # 2. On the Chief of Staff ('cos') channel, require_thread_access denies T in external workspace
        with pytest.raises(HTTPException) as exc_cos_read:
            await dalton_sharing.require_thread_access(
                ext_tid, "u-chao", channel="cos", need="read"
            )
        assert exc_cos_read.value.status_code == 404

        # 3. manage_threads(list) excludes T in external workspace while keeping own and cos-enabled colleague threads
        list_cmd = await sec_tools.threads_list("u-chao", None, "tc-list-1", cos_own_tid)
        list_payload = json.loads(list_cmd.update["messages"][0].content)
        assert list_payload["success"] is True
        listed_ids = [
            r["conversation_thread_id"] for r in list_payload["threads"]
        ]
        assert ext_tid not in listed_ids
        assert set(listed_ids) == {colleague_tid, own_tid}
        assert list_payload["total"] == 2

        # 4. agent_output(T), manage_threads(stop, T), manage_threads(delete, T) all return not found for T in external workspace
        out_cmd = await cos_mod.agent_output.ainvoke(
            {
                "name": "agent_output",
                "args": {"thread_id": ext_tid},
                "id": "tc-out-ext",
                "type": "tool_call",
            },
            config={"configurable": {"user_id": "u-chao", "thread_id": cos_own_tid}},
        )
        out_payload = json.loads(out_cmd.update["messages"][0].content)
        assert out_payload["success"] is False
        assert "not found" in out_payload["error"]

        stop_cmd = await sec_tools.threads_stop(
            "u-chao", ext_tid, cos_own_tid, "tc-stop-ext"
        )
        stop_payload = json.loads(stop_cmd.update["messages"][0].content)
        assert stop_payload["success"] is False
        assert "not found" in stop_payload["error"]

        del_cmd = await sec_tools.threads_delete(
            "u-chao", ext_tid, "tc-del-ext"
        )
        del_payload = json.loads(del_cmd.update["messages"][0].content)
        assert del_payload["success"] is False
        assert "not found" in del_payload["error"]

        # 5. When internal colleague Richard turns off cos_rw_enabled=False, his workspace thread is also excluded/denied on CoS
        cos_rw_state["u-richard"] = False
        list_cmd_2 = await sec_tools.threads_list(
            "u-chao", None, "tc-list-2", cos_own_tid
        )
        list_payload_2 = json.loads(list_cmd_2.update["messages"][0].content)
        assert [
            r["conversation_thread_id"] for r in list_payload_2["threads"]
        ] == [own_tid]
        assert list_payload_2["total"] == 1

        stop_colleague_cmd = await sec_tools.threads_stop(
            "u-chao", colleague_tid, cos_own_tid, "tc-stop-col"
        )
        assert (
            json.loads(stop_colleague_cmd.update["messages"][0].content)["success"]
            is False
        )

        # 6. Even after external owner reverts web_access to 'hidden', A still reads T on web while CoS remains denied
        web_state[ext_ws_id] = "hidden"
        assert (
            await dalton_sharing.require_thread_access(
                ext_tid, "u-chao", channel="web", need="read"
            )
            == "u-chao"
        )
        with pytest.raises(HTTPException) as exc_cos_hidden:
            await dalton_sharing.require_thread_access(
                ext_tid, "u-chao", channel="cos", need="read"
            )
        assert exc_cos_hidden.value.status_code == 404

    @pytest.mark.asyncio
    async def test_cos_threads_list_and_activity_filter_in_sql_before_pagination(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With 25 own threads and 25 newer threads created by A in an external
        workspace (plus 1 in colleague B's workspace), manage_threads(list) and
        <activity> filter in SQL via `AND (w.user_id = %s OR w.workspace_id = ANY(%s::uuid[]))`
        before COUNT / LIMIT / OFFSET."""
        from datetime import timedelta
        from src.server.database.conversation import threads_read
        from src.tools.secretary import activity as activity_mod
        from src.tools.secretary import tools as sec_tools
        from src.tools.secretary.utils import ComputerPlacement

        _enable_dalton_auth(monkeypatch)
        _admit_team_members(monkeypatch)
        monkeypatch.setenv("DALTON_COLLEAGUE_DISPATCH_ENABLED", "true")
        settings_mod.get_dalton_config.cache_clear()

        ext_ws_id = "11111111-1111-4111-8111-111111111111"
        own_ws_id = "22222222-2222-4222-8222-222222222222"
        colleague_ws_id = "33333333-3333-4333-8333-333333333333"
        cos_own_tid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

        base_time = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
        # 25 older own threads + 1 colleague B thread + 25 newest external workspace threads
        own_rows = [
            {
                "conversation_thread_id": f"00000000-0000-4000-8000-{i:012d}",
                "workspace_id": own_ws_id,
                "workspace_name": "Chao Own WS",
                "thread_user_id": "u-chao",
                "workspace_owner_id": "u-chao",
                "title": f"Own Thread {i}",
                "current_status": "completed",
                "updated_at": base_time + timedelta(minutes=i),
                "latest_run_id": f"r-own-{i}",
                "latest_run_seq": 1,
                "latest_status": "completed",
                "latest_interrupt_reason": None,
                "last_seen_run_seq": 1,
            }
            for i in range(1, 26)
        ]
        colleague_row = {
            "conversation_thread_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "workspace_id": colleague_ws_id,
            "workspace_name": "Richard Colleague WS",
            "thread_user_id": "u-chao",
            "workspace_owner_id": "u-richard",
            "title": "Colleague B Thread",
            "current_status": "completed",
            "updated_at": base_time + timedelta(minutes=30),
            "latest_run_id": "r-col-1",
            "latest_run_seq": 1,
            "latest_status": "completed",
            "latest_interrupt_reason": None,
            "last_seen_run_seq": 1,
        }
        ext_rows = [
            {
                "conversation_thread_id": f"eeeeeeee-eeee-4eee-8eee-{i:012d}",
                "workspace_id": ext_ws_id,
                "workspace_name": "Alice External WS",
                "thread_user_id": "u-chao",
                "workspace_owner_id": "u-ext",
                "title": f"External Secret Thread {i}",
                "current_status": "completed",
                "updated_at": base_time + timedelta(minutes=100 + i),
                "latest_run_id": f"r-ext-{i}",
                "latest_run_seq": 1,
                "latest_status": "completed",
                "latest_interrupt_reason": None,
                "last_seen_run_seq": 1,
            }
            for i in range(1, 26)
        ]
        # Ordered newest first: 25 external threads come before colleague_row and 25 own_rows
        all_rows = sorted(
            [*ext_rows, colleague_row, *own_rows],
            key=lambda r: r["updated_at"],
            reverse=True,
        )

        cos_rw_state = {"u-richard": False}

        async def _fake_list_cos_ws(uid: str) -> list[dict[str, Any]]:
            if cos_rw_state.get("u-richard", True):
                return [
                    {
                        "workspace_id": colleague_ws_id,
                        "user_id": "u-richard",
                        "owner_name": "Richard Lu",
                        "owner_email": "richard.lu@dalton.test",
                        "name": "Richard Colleague WS",
                        "status": "running",
                        "access": "rw",
                        "cos_access": "rw",
                        "web_access": "rw",
                        "read_only": False,
                    }
                ]
            return []

        monkeypatch.setattr(
            dalton_sharing,
            "list_colleague_cos_workspaces",
            create_autospec(
                dalton_sharing.list_colleague_cos_workspaces,
                side_effect=_fake_list_cos_ws,
            ),
        )

        executed_sql: list[tuple[str, Any]] = []
        cursor = create_autospec(psycopg.AsyncCursor, instance=True)

        def _eval_sql_rows(sql: str, params: Any) -> list[dict[str, Any]]:
            assert "AND (w.user_id = %s OR w.workspace_id = ANY(%s::uuid[]))" in sql
            if "IS DISTINCT FROM 'automation'" in sql:
                # get_recent_threads_for_user: params = (user_id, exclude_tid, user_id, cos_ws_ids, limit)
                uid = str(params[2])
                cos_ids = set(params[3])
            else:
                # get_threads_for_user: params = (user_id, user_id, user_id, user_id, cos_ws_ids, ...)
                uid = str(params[3])
                cos_ids = set(params[4])
            return [
                r
                for r in all_rows
                if r["workspace_owner_id"] == uid or r["workspace_id"] in cos_ids
            ]

        async def _fake_execute(sql: str, params: Any = None) -> None:
            executed_sql.append((sql, params))

        async def _fake_fetchone() -> dict[str, Any]:
            sql, params = executed_sql[-1]
            return {"total": len(_eval_sql_rows(sql, params))}

        async def _fake_fetchall() -> list[dict[str, Any]]:
            sql, params = executed_sql[-1]
            filtered = _eval_sql_rows(sql, params)
            if "IS DISTINCT FROM 'automation'" in sql:
                limit_val = int(params[-1])
                return filtered[:limit_val]
            limit_val = int(params[-2])
            offset_val = int(params[-1])
            return filtered[offset_val : offset_val + limit_val]

        cursor.execute.side_effect = _fake_execute
        cursor.fetchone.side_effect = _fake_fetchone
        cursor.fetchall.side_effect = _fake_fetchall
        conn = create_autospec(psycopg.AsyncConnection, instance=True)

        @asynccontextmanager
        async def _cursor_cm(*args: Any, **kwargs: Any):
            yield cursor

        conn.cursor.side_effect = _cursor_cm

        @asynccontextmanager
        async def _db_cm(existing_conn: Any = None):
            yield existing_conn or conn

        monkeypatch.setattr(
            threads_read.pool,
            "get_db_connection",
            create_autospec(
                threads_read.pool.get_db_connection, side_effect=_db_cm
            ),
        )

        # 1. Colleague B cos_rw_enabled=False: page 1 returns 20 of A's own threads, total == 25
        list_cmd_off = await sec_tools.threads_list(
            "u-chao", None, "tc-page1-off", cos_own_tid
        )
        payload_off = json.loads(list_cmd_off.update["messages"][0].content)
        assert payload_off["success"] is True
        assert payload_off["total"] == 25
        assert len(payload_off["threads"]) == 20
        assert all(
            t["workspace_id"] == own_ws_id for t in payload_off["threads"]
        )
        assert len(executed_sql) == 2
        for sql, params in executed_sql:
            assert "AND (w.user_id = %s OR w.workspace_id = ANY(%s::uuid[]))" in sql
            assert params[3] == "u-chao"
            assert params[4] == []

        # 2. Colleague B cos_rw_enabled=True: B's workspace thread appears on page 1, total == 26
        executed_sql.clear()
        cos_rw_state["u-richard"] = True
        list_cmd_on = await sec_tools.threads_list(
            "u-chao", None, "tc-page1-on", cos_own_tid
        )
        payload_on = json.loads(list_cmd_on.update["messages"][0].content)
        assert payload_on["success"] is True
        assert payload_on["total"] == 26
        assert len(payload_on["threads"]) == 20
        assert payload_on["threads"][0]["conversation_thread_id"] == colleague_row["conversation_thread_id"]
        for sql, params in executed_sql:
            assert "AND (w.user_id = %s OR w.workspace_id = ANY(%s::uuid[]))" in sql
            assert params[3] == "u-chao"
            assert params[4] == [colleague_ws_id]

        # 3. <activity> via read_activity / get_recent_threads_for_user:
        monkeypatch.setattr(
            activity_mod,
            "get_workspaces_for_user",
            create_autospec(
                activity_mod.get_workspaces_for_user, return_value=([], 0)
            ),
        )
        monkeypatch.setattr(
            activity_mod,
            "list_settled_since",
            create_autospec(activity_mod.list_settled_since, return_value=[]),
        )
        monkeypatch.setattr(
            activity_mod,
            "get_user_portfolio",
            create_autospec(activity_mod.get_user_portfolio, return_value=[]),
        )
        monkeypatch.setattr(
            activity_mod,
            "read_computer_placement",
            create_autospec(
                activity_mod.read_computer_placement,
                return_value=ComputerPlacement(),
            ),
        )

        # With B's cos_rw_enabled=True: <activity> includes B's thread and A's own threads, never external threads
        executed_sql.clear()
        act_on = await activity_mod.read_activity(
            "u-chao",
            home_id="99999999-9999-4999-8999-999999999999",
            thread_id=cos_own_tid,
            timezone="UTC",
        )
        assert act_on is not None
        assert "Colleague B Thread" in act_on
        assert "Own Thread 25" in act_on
        assert "External Secret Thread" not in act_on
        assert "Alice External WS" not in act_on
        assert len(executed_sql) == 1
        assert (
            "AND (w.user_id = %s OR w.workspace_id = ANY(%s::uuid[]))"
            in executed_sql[0][0]
        )
        assert executed_sql[0][1][2] == "u-chao"
        assert executed_sql[0][1][3] == [colleague_ws_id]

        # With B's cos_rw_enabled=False: <activity> includes only A's own threads
        executed_sql.clear()
        cos_rw_state["u-richard"] = False
        act_off = await activity_mod.read_activity(
            "u-chao",
            home_id="99999999-9999-4999-8999-999999999999",
            thread_id=cos_own_tid,
            timezone="UTC",
        )
        assert act_off is not None
        assert "Colleague B Thread" not in act_off
        assert "Own Thread 25" in act_off
        assert "External Secret Thread" not in act_off
        assert executed_sql[0][1][3] == []
