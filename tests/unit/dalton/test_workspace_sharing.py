"""Unit tests for Dalton workspace sharing settings (d016 + 3-level matrix across web & CoS channels)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import create_autospec

import alembic.op as alembic_op
import psycopg
import pytest
from fastapi import HTTPException, UploadFile

from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from src.config import settings as app_settings
from src.dalton import sharing as dalton_sharing
from src.dalton.app import router as dalton_router
from src.dalton.database import knowledge as db_knowledge
from src.dalton.database import verdicts as db_verdicts
from src.dalton.knowledge import service as knowledge_service
from src.dalton.migrations.versions import d018_workspace_sharing
from src.dalton.tools import knowledge_tools as kt_mod
from src.dalton.tools.knowledge_tools import create_knowledge_tools
from src.server.app import workspaces as workspaces_app
from src.server.app.threads import crud as threads_crud
from src.server.app.workspace_files import _shared as ws_files_shared
from src.server.app.workspace_files import crud as ws_files_crud
from src.server.database import computer as computer_db
from src.server.database import conversation as conv_db
from src.server.database import home_workspace as home_ws_db
from src.server.database import user as user_db
from src.server.database import workspace as ws_db
from src.server.database.conversation import threads_read
from src.server.models.workspace import WorkspaceUpdate
from src.server.services.persistence.file import FilePersistenceService
from src.server.utils import secret_redactor
from src.tools.secretary import activity as sec_activity
from src.tools.secretary import dispatch as sec_dispatch
from src.tools.secretary import tools as sec_tools
from src.tools.secretary.utils import ComputerPlacement as _ComputerPlacement
from src.tools.secretary.chief_of_staff import CHIEF_OF_STAFF_TOOLS

TEAM_EMAILS = (
    "richard.lu@dalton.test,chao.yu@dalton.test,"
    "rex.yuan@dalton.test,mark.xie@dalton.test"
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
    "u-outsider": {
        "user_id": "u-outsider",
        "email": "outsider@other.com",
        "name": "Outsider",
    },
}


def _enable_dalton_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setattr(app_settings, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(
        app_settings,
        "background_dispatch_requires_token",
        create_autospec(app_settings.background_dispatch_requires_token, return_value=False),
    )
    monkeypatch.setenv("DALTON_ALLOWED_EMAILS", TEAM_EMAILS)
    monkeypatch.setenv("DALTON_PM_USER_IDS", "u-pm")

    async def _fake_get_user(uid: str) -> dict[str, Any] | None:
        return USERS_BY_ID.get(uid)

    monkeypatch.setattr(
        user_db, "get_user", create_autospec(user_db.get_user, side_effect=_fake_get_user)
    )


def _sample_workspace(
    *,
    workspace_id: str = "11111111-1111-4111-8111-111111111111",
    user_id: str = "u-richard",
    name: str = "NVDA Deep Dive",
    status: str = "running",
    is_home: bool = False,
) -> dict[str, Any]:
    now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    return {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "name": name,
        "description": "Semiconductor research",
        "sandbox_id": "sbx-secret-1",
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


from zoneinfo import ZoneInfo

_ORIG_GET_SHARING = dalton_sharing.get_workspace_sharing


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
            "updated_at": datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
        }

    monkeypatch.setattr(
        dalton_sharing,
        "get_workspace_sharing",
        create_autospec(_ORIG_GET_SHARING, side_effect=_fake_get_sharing),
    )


def _cmd_json(cmd: Any) -> dict[str, Any]:
    return json.loads(cmd.update["messages"][0].content)


class TestMigrationD018:
    """Verify migration d018_workspace_sharing upgrade and downgrade SQL."""

    def test_d018_revision_metadata(self) -> None:
        assert d018_workspace_sharing.revision == "d018"
        assert d018_workspace_sharing.down_revision == "d017"

    def test_d018_upgrade_and_downgrade(self, monkeypatch: pytest.MonkeyPatch) -> None:
        exec_mock = create_autospec(alembic_op.execute)
        monkeypatch.setattr(d018_workspace_sharing.op, "execute", exec_mock)

        d018_workspace_sharing.upgrade()
        assert exec_mock.call_count == 1
        sql_up = exec_mock.call_args[0][0]
        assert "CREATE TABLE IF NOT EXISTS dalton_workspace_sharing" in sql_up
        assert "DEFAULT 'ro'" in sql_up
        assert "DEFAULT 'hidden'" in sql_up
        assert "CHECK (cos_access IN ('rw', 'ro', 'hidden'))" in sql_up
        assert "CHECK (web_access IN ('rw', 'ro', 'hidden'))" in sql_up

        exec_mock.reset_mock()
        d018_workspace_sharing.downgrade()
        assert exec_mock.call_count == 1
        sql_down = exec_mock.call_args[0][0]
        assert "DROP TABLE IF EXISTS dalton_workspace_sharing" in sql_down


class TestDefaultSharingAndDbHelpers:
    """Verify default values when no row exists in dalton_workspace_sharing."""

    @pytest.mark.asyncio
    async def test_no_row_defaults_to_cos_ro_and_web_hidden(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)
        fake_cur = create_autospec(psycopg.AsyncCursor, instance=True)
        fake_cur.fetchone.return_value = None

        @asynccontextmanager
        async def _cur_ctx(*args: Any, **kwargs: Any):
            yield fake_cur

        fake_conn.cursor.side_effect = _cur_ctx

        result = await dalton_sharing.get_workspace_sharing(
            "11111111-1111-4111-8111-111111111111", conn=fake_conn
        )
        assert result == {
            "workspace_id": "11111111-1111-4111-8111-111111111111",
            "cos_access": "ro",
            "web_access": "hidden",
            "updated_by": None,
            "updated_at": None,
        }

    @pytest.mark.asyncio
    async def test_upsert_workspace_sharing_validates_and_returns_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
        fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)
        fake_cur = create_autospec(psycopg.AsyncCursor, instance=True)
        fake_cur.fetchone.return_value = {
            "workspace_id": "11111111-1111-4111-8111-111111111111",
            "cos_access": "rw",
            "web_access": "ro",
            "updated_by": "u-richard",
            "updated_at": now,
        }

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

        saved = await dalton_sharing.upsert_workspace_sharing(
            "11111111-1111-4111-8111-111111111111",
            cos_access="rw",
            web_access="ro",
            updated_by="u-richard",
        )
        assert saved["cos_access"] == "rw"
        assert saved["web_access"] == "ro"
        assert saved["updated_by"] == "u-richard"

        with pytest.raises(HTTPException) as exc_invalid:
            await dalton_sharing.upsert_workspace_sharing(
                "11111111-1111-4111-8111-111111111111",
                cos_access="invalid",
                web_access="ro",
                updated_by="u-richard",
            )
        assert exc_invalid.value.status_code == 422


class TestSharingMatrix:
    """Cover 3 levels × 2 channels × read/write × owner/colleague/PM/outsider × Home."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", ["web", "cos"])
    @pytest.mark.parametrize("level", ["hidden", "ro", "rw"])
    @pytest.mark.parametrize("need", ["read", "write"])
    async def test_owner_always_allowed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        channel: str,
        level: str,
        need: str,
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _set_sharing(monkeypatch, cos_access=level, web_access=level)
        ws = _sample_workspace(user_id="u-richard")

        out = await dalton_sharing.require_workspace_access(
            ws, "u-richard", channel=channel, need=need
        )
        assert out["workspace_id"] == ws["workspace_id"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", ["web", "cos"])
    @pytest.mark.parametrize(
        "level,need,expected_status",
        [
            ("hidden", "read", 404),
            ("hidden", "write", 404),
            ("ro", "read", None),
            ("ro", "write", 403),
            ("rw", "read", None),
            ("rw", "write", None),
        ],
    )
    async def test_colleague_access_matrix(
        self,
        monkeypatch: pytest.MonkeyPatch,
        channel: str,
        level: str,
        need: str,
        expected_status: int | None,
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _set_sharing(
            monkeypatch,
            cos_access=level if channel == "cos" else "hidden",
            web_access=level if channel == "web" else "hidden",
        )
        ws = _sample_workspace(user_id="u-richard")

        if expected_status is None:
            out = await dalton_sharing.require_workspace_access(
                ws, "u-chao", channel=channel, need=need
            )
            assert out["workspace_id"] == ws["workspace_id"]
        else:
            with pytest.raises(HTTPException) as exc:
                await dalton_sharing.require_workspace_access(
                    ws, "u-chao", channel=channel, need=need
                )
            assert exc.value.status_code == expected_status

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", ["web", "cos"])
    @pytest.mark.parametrize("level", ["hidden", "ro", "rw"])
    @pytest.mark.parametrize("need", ["read", "write"])
    @pytest.mark.parametrize("caller", ["u-chao", "u-pm", "u-outsider"])
    async def test_home_workspace_never_open_to_others(
        self,
        monkeypatch: pytest.MonkeyPatch,
        channel: str,
        level: str,
        need: str,
        caller: str,
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _set_sharing(monkeypatch, cos_access=level, web_access=level)
        home_ws = _sample_workspace(user_id="u-richard", name="Home", is_home=True)

        with pytest.raises(HTTPException) as exc:
            await dalton_sharing.require_workspace_access(
                home_ws, caller, channel=channel, need=need
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", ["web", "cos"])
    @pytest.mark.parametrize("level", ["hidden", "ro", "rw"])
    @pytest.mark.parametrize("need", ["read", "write"])
    async def test_outsider_always_403(
        self,
        monkeypatch: pytest.MonkeyPatch,
        channel: str,
        level: str,
        need: str,
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _set_sharing(monkeypatch, cos_access=level, web_access=level)
        ws = _sample_workspace(user_id="u-richard")

        with pytest.raises(HTTPException) as exc:
            await dalton_sharing.require_workspace_access(
                ws, "u-outsider", channel=channel, need=need
            )
        assert exc.value.status_code == 403

    def test_can_edit_workspace_sharing_owner_pm_colleague_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard", is_home=False)
        home_ws = _sample_workspace(user_id="u-richard", is_home=True)

        assert dalton_sharing.can_edit_workspace_sharing(ws, "u-richard") is True
        assert dalton_sharing.can_edit_workspace_sharing(ws, "u-pm") is True
        assert dalton_sharing.can_edit_workspace_sharing(ws, "u-chao") is False
        assert dalton_sharing.can_edit_workspace_sharing(ws, "u-outsider") is False

        # Home workspace cannot be edited by anyone
        assert dalton_sharing.can_edit_workspace_sharing(home_ws, "u-richard") is False
        assert dalton_sharing.can_edit_workspace_sharing(home_ws, "u-pm") is False


class TestSharingRouterEndpoints:
    """Test GET / PUT /api/v1/dalton/workspaces/{id}/sharing."""

    @pytest.mark.asyncio
    async def test_get_sharing_owner_pm_colleague_hidden(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")
        get_ws_mock = create_autospec(ws_db.get_workspace, return_value=ws)
        monkeypatch.setattr(ws_db, "get_workspace", get_ws_mock)

        # Hidden web_access: owner & PM can GET with can_edit=True; colleague gets 404; outsider gets 403
        _set_sharing(monkeypatch, cos_access="ro", web_access="hidden")

        res_owner = await dalton_router.get_workspace_sharing_endpoint(
            ws["workspace_id"], user_id="u-richard"
        )
        assert res_owner["can_edit"] is True
        assert res_owner["cos_access"] == "ro"
        assert res_owner["web_access"] == "hidden"

        res_pm = await dalton_router.get_workspace_sharing_endpoint(
            ws["workspace_id"], user_id="u-pm"
        )
        assert res_pm["can_edit"] is True

        with pytest.raises(HTTPException) as exc_col_hidden:
            await dalton_router.get_workspace_sharing_endpoint(
                ws["workspace_id"], user_id="u-chao"
            )
        assert exc_col_hidden.value.status_code == 404

        with pytest.raises(HTTPException) as exc_out:
            await dalton_router.get_workspace_sharing_endpoint(
                ws["workspace_id"], user_id="u-outsider"
            )
        assert exc_out.value.status_code == 403

        # When web_access='ro', colleague can GET with can_edit=False
        _set_sharing(monkeypatch, cos_access="ro", web_access="ro")
        res_col = await dalton_router.get_workspace_sharing_endpoint(
            ws["workspace_id"], user_id="u-chao"
        )
        assert res_col["can_edit"] is False
        assert res_col["web_access"] == "ro"

    @pytest.mark.asyncio
    async def test_put_sharing_owner_pm_colleague_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")
        get_ws_mock = create_autospec(ws_db.get_workspace, return_value=ws)
        monkeypatch.setattr(ws_db, "get_workspace", get_ws_mock)

        async def _fake_upsert(
            workspace_id: str,
            *,
            cos_access: str | None = None,
            web_access: str | None = None,
            updated_by: str,
        ) -> dict[str, Any]:
            return {
                "workspace_id": workspace_id,
                "cos_access": cos_access or "ro",
                "web_access": web_access or "hidden",
                "updated_by": updated_by,
                "updated_at": datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
            }

        monkeypatch.setattr(
            dalton_sharing,
            "upsert_workspace_sharing",
            create_autospec(dalton_sharing.upsert_workspace_sharing, side_effect=_fake_upsert),
        )

        # Owner can PUT
        body = dalton_router.WorkspaceSharingUpdateRequest(
            cos_access="rw", web_access="ro"
        )
        res_owner = await dalton_router.update_workspace_sharing_endpoint(
            ws["workspace_id"], body=body, user_id="u-richard"
        )
        assert res_owner["cos_access"] == "rw"
        assert res_owner["web_access"] == "ro"
        assert res_owner["can_edit"] is True

        # PM can PUT anyone's workspace
        res_pm = await dalton_router.update_workspace_sharing_endpoint(
            ws["workspace_id"],
            body=dalton_router.WorkspaceSharingUpdateRequest(
                cos_access="hidden", web_access="rw"
            ),
            user_id="u-pm",
        )
        assert res_pm["cos_access"] == "hidden"
        assert res_pm["web_access"] == "rw"
        assert res_pm["updated_by"] == "u-pm"
        assert res_pm["can_edit"] is True

        # Colleague cannot PUT -> 403
        with pytest.raises(HTTPException) as exc_col:
            await dalton_router.update_workspace_sharing_endpoint(
                ws["workspace_id"], body=body, user_id="u-chao"
            )
        assert exc_col.value.status_code == 403

        # Outsider cannot PUT -> 403
        with pytest.raises(HTTPException) as exc_out:
            await dalton_router.update_workspace_sharing_endpoint(
                ws["workspace_id"], body=body, user_id="u-outsider"
            )
        assert exc_out.value.status_code == 403

        # Home workspace rejects PUT even for owner -> 400, and for colleague -> 403
        home_ws = _sample_workspace(user_id="u-richard", is_home=True)
        get_ws_mock.return_value = home_ws
        with pytest.raises(HTTPException) as exc_home_owner:
            await dalton_router.update_workspace_sharing_endpoint(
                home_ws["workspace_id"], body=body, user_id="u-richard"
            )
        assert exc_home_owner.value.status_code == 400

        with pytest.raises(HTTPException) as exc_home_col:
            await dalton_router.update_workspace_sharing_endpoint(
                home_ws["workspace_id"], body=body, user_id="u-chao"
            )
        assert exc_home_col.value.status_code == 403

    @pytest.mark.asyncio
    async def test_partial_update_uses_coalesce_without_overwriting_other_column(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")
        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(ws_db.get_workspace, return_value=ws),
        )

        executed_queries: list[tuple[str, tuple[Any, ...]]] = []
        fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)
        fake_cur = create_autospec(psycopg.AsyncCursor, instance=True)

        async def _capture_exec(sql: str, params: Any = None) -> None:
            executed_queries.append((sql, tuple(params or ())))

        fake_cur.execute.side_effect = _capture_exec
        fake_cur.fetchone.return_value = {
            "workspace_id": ws["workspace_id"],
            "cos_access": "hidden",
            "web_access": "ro",
            "updated_by": "u-richard",
            "updated_at": datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
        }

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

        # Partial update providing only cos_access="hidden" (web_access=None)
        res = await dalton_router.update_workspace_sharing_endpoint(
            ws["workspace_id"],
            body=dalton_router.WorkspaceSharingUpdateRequest(cos_access="hidden"),
            user_id="u-richard",
        )
        assert res["cos_access"] == "hidden"
        assert res["web_access"] == "ro"
        assert len(executed_queries) == 1
        sql, params = executed_queries[0]
        assert "COALESCE(%s, dalton_workspace_sharing.cos_access)" in sql
        assert "COALESCE(%s, dalton_workspace_sharing.web_access)" in sql
        # INSERT defaults unprovided web_access to 'hidden', ON CONFLICT passes None for web_access
        assert params == (
            ws["workspace_id"],
            "hidden",
            "hidden",
            "u-richard",
            "hidden",
            None,
        )


class TestWebChannelEndpoints:
    """Test web endpoints across rw / ro / hidden / outsider / Home."""

    @pytest.mark.asyncio
    async def test_list_team_workspaces_filters_hidden_and_returns_access(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
        rows = [
            {
                "workspace_id": "ws-ro",
                "user_id": "u-richard",
                "name": "Richard RO",
                "description": "ro ws",
                "status": "running",
                "is_home": False,
                "created_at": now,
                "updated_at": now,
                "last_activity_at": now,
                "stopped_at": None,
                "owner_name": "Richard Lu",
                "owner_email": "richard.lu@dalton.test",
                "web_access": "ro",
                "cos_access": "ro",
                "sandbox_id": "sbx-1",
                "config": {"secret": "val"},
            },
            {
                "workspace_id": "ws-rw",
                "user_id": "u-richard",
                "name": "Richard RW",
                "description": "rw ws",
                "status": "running",
                "is_home": False,
                "created_at": now,
                "updated_at": now,
                "last_activity_at": now,
                "stopped_at": None,
                "owner_name": "Richard Lu",
                "owner_email": "richard.lu@dalton.test",
                "web_access": "rw",
                "cos_access": "rw",
                "sandbox_id": "sbx-1",
                "config": {},
            },
            {
                "workspace_id": "ws-hidden",
                "user_id": "u-richard",
                "name": "Richard Hidden",
                "description": "hidden ws",
                "status": "running",
                "is_home": False,
                "created_at": now,
                "updated_at": now,
                "last_activity_at": now,
                "stopped_at": None,
                "owner_name": "Richard Lu",
                "owner_email": "richard.lu@dalton.test",
                "web_access": "hidden",
                "cos_access": "ro",
            },
        ]

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

        members = await dalton_sharing.list_team_workspaces("u-chao")
        assert len(members) == 1
        ws_list = members[0]["workspaces"]
        assert [w["workspace_id"] for w in ws_list] == ["ws-ro", "ws-rw"]
        assert ws_list[0]["access"] == "ro"
        assert ws_list[1]["access"] == "rw"
        assert "sandbox_id" not in ws_list[0]
        assert "config" not in ws_list[0]

    @pytest.mark.asyncio
    async def test_hidden_workspace_returns_404_on_web_read_routes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _set_sharing(monkeypatch, cos_access="ro", web_access="hidden")
        ws = _sample_workspace(user_id="u-richard")

        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(ws_db.get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            workspaces_app,
            "db_get_workspace",
            create_autospec(workspaces_app.db_get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "db_get_workspace",
            create_autospec(ws_files_crud.db_get_workspace, return_value=ws),
        )

        with pytest.raises(HTTPException) as exc_ws:
            await workspaces_app.get_workspace(ws["workspace_id"], x_user_id="u-chao")
        assert exc_ws.value.status_code == 404

        with pytest.raises(HTTPException) as exc_files:
            await ws_files_crud.list_workspace_files(ws["workspace_id"], x_user_id="u-chao")
        assert exc_files.value.status_code == 404

        with pytest.raises(HTTPException) as exc_read:
            await ws_files_crud.read_workspace_file(
                ws["workspace_id"], x_user_id="u-chao", path="note.md"
            )
        assert exc_read.value.status_code == 404

        with pytest.raises(HTTPException) as exc_threads:
            await threads_crud.list_threads(
                x_user_id="u-chao",
                workspace_id=ws["workspace_id"],
                limit=20,
                offset=0,
                sort_by="updated_at",
                sort_order="desc",
            )
        assert exc_threads.value.status_code == 404

    @pytest.mark.asyncio
    async def test_web_file_writes_allowed_for_owner_and_rw_colleague_rejected_for_ro_hidden_outsider_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard", status="running")
        shared_get_ws_mock = create_autospec(
            ws_files_shared.db_get_workspace, return_value=ws
        )
        monkeypatch.setattr(
            ws_files_shared,
            "db_get_workspace",
            shared_get_ws_mock,
        )
        monkeypatch.setattr(
            ws_files_crud,
            "owner_work_dir",
            create_autospec(
                ws_files_crud.owner_work_dir,
                return_value="/home/workspace/NVDA_Deep_Dive",
            ),
        )

        acquired_for_users: list[str] = []
        live_fs: dict[str, bytes] = {}
        symlink_map = {
            "/home/workspace/NVDA_Deep_Dive/notes/symlink_skill.md": (
                "/home/workspace/NVDA_Deep_Dive/.agents/skills/alpha/SKILL.md"
            ),
        }

        @asynccontextmanager
        async def _fake_path_write_lock(normalized_path: str):
            yield

        async def _fake_awrite_file_text(filepath: str, content: str) -> bool:
            live_fs[filepath] = content.encode("utf-8")
            return True

        async def _fake_aupload_file_bytes(
            filepath: str, content: bytes, *, verify: bool = False
        ) -> bool:
            live_fs[filepath] = content
            return True

        async def _fake_aglob_files(
            pattern: str,
            path: str = ".",
            *,
            allow_denied: bool = False,
            hide_history: bool = False,
        ) -> list[str]:
            return [
                *live_fs.keys(),
                "/home/workspace/NVDA_Deep_Dive/.agents/memory/secret.md",
            ]

        fake_sandbox = create_autospec(PTCSandbox, instance=True)
        fake_sandbox.is_ready.return_value = True
        fake_sandbox.validate_path.return_value = True
        fake_sandbox.virtualize_path.side_effect = lambda p, project=None: p.replace(
            "/home/workspace/NVDA_Deep_Dive/", ""
        )
        fake_sandbox.awrite_file_text.side_effect = _fake_awrite_file_text
        fake_sandbox.aupload_file_bytes.side_effect = _fake_aupload_file_bytes
        fake_sandbox.aglob_files.side_effect = _fake_aglob_files
        fake_sandbox.path_write_lock.side_effect = _fake_path_write_lock
        fake_sandbox.execute_bash_command.return_value = {"success": True}

        @asynccontextmanager
        async def _fake_acquire(workspace_id: str, user_id: str):
            acquired_for_users.append(user_id)
            yield fake_sandbox, ws

        monkeypatch.setattr(
            ws_files_crud,
            "_acquire_sandbox_to_change",
            create_autospec(
                ws_files_crud._acquire_sandbox_to_change,
                side_effect=_fake_acquire,
            ),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "db_get_workspace",
            create_autospec(ws_files_crud.db_get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "get_vault_secrets_for_redaction",
            create_autospec(
                ws_files_crud.get_vault_secrets_for_redaction, return_value=[]
            ),
        )

        async def _resolve_sandbox_path(
            sandbox: Any, candidate: str, *, work_dir: str
        ) -> str | None:
            return symlink_map.get(candidate, candidate)

        monkeypatch.setattr(
            ws_files_crud,
            "contained_sandbox_path",
            create_autospec(
                ws_files_crud.contained_sandbox_path,
                side_effect=_resolve_sandbox_path,
            ),
        )

        async def _resolve_sandbox_paths(
            sandbox: Any, candidates: list[str], *, work_dir: str
        ) -> list[str | None]:
            return [symlink_map.get(c, c) for c in candidates]

        monkeypatch.setattr(
            ws_files_crud,
            "contained_sandbox_paths",
            create_autospec(
                ws_files_crud.contained_sandbox_paths,
                side_effect=_resolve_sandbox_paths,
            ),
        )

        async def _read_sandbox_file(
            sandbox: Any, candidate: str, *, work_dir: str
        ) -> tuple[str, bytes] | None:
            resolved = symlink_map.get(candidate, candidate)
            if resolved not in live_fs:
                return None
            return resolved, live_fs[resolved]

        monkeypatch.setattr(
            ws_files_crud,
            "read_contained_sandbox_file",
            create_autospec(
                ws_files_crud.read_contained_sandbox_file,
                side_effect=_read_sandbox_file,
            ),
        )

        # 1. rw colleague -> write, then immediately read & list from live sandbox
        _set_sharing(monkeypatch, cos_access="ro", web_access="rw")

        res_write = await ws_files_crud.write_workspace_file(
            ws["workspace_id"],
            x_user_id="u-chao",
            path="notes/thesis.md",
            body=ws_files_crud.WriteFileRequest(content="updated by colleague"),
        )
        assert res_write["path"] == "notes/thesis.md"
        assert acquired_for_users[-1] == "u-richard"

        res_read_live = await ws_files_crud.read_workspace_file(
            ws["workspace_id"],
            x_user_id="u-chao",
            path="notes/thesis.md",
            offset=0,
            limit=100,
            unlimited=False,
        )
        assert res_read_live["content"] == "updated by colleague"
        assert res_read_live.get("source") != "database"
        assert acquired_for_users[-1] == "u-richard"

        res_list_live = await ws_files_crud.list_workspace_files(
            ws["workspace_id"],
            x_user_id="u-chao",
            path=".",
            include_system=False,
            pattern="**/*",
            wait_for_sandbox=True,
            auto_start=False,
        )
        assert res_list_live["sandbox_ready"] is True
        assert res_list_live["files"] == ["notes/thesis.md"]

        # rw colleague blocked from writing direct hidden/system files
        with pytest.raises(HTTPException) as exc_sys_write:
            await ws_files_crud.write_workspace_file(
                ws["workspace_id"],
                x_user_id="u-chao",
                path=".agents/memory/secret.md",
                body=ws_files_crud.WriteFileRequest(content="SECRET=1"),
            )
        assert exc_sys_write.value.status_code == 403

        # rw colleague blocked when normal path resolves via symlink to .agents/skills/...
        with pytest.raises(HTTPException) as exc_symlink_write:
            await ws_files_crud.write_workspace_file(
                ws["workspace_id"],
                x_user_id="u-chao",
                path="notes/symlink_skill.md",
                body=ws_files_crud.WriteFileRequest(content="hijack skill"),
            )
        assert exc_symlink_write.value.status_code == 403

        fake_upload = create_autospec(UploadFile, instance=True)
        fake_upload.filename = "model.csv"
        fake_upload.size = 12
        monkeypatch.setattr(
            ws_files_crud,
            "read_capped",
            create_autospec(ws_files_crud.read_capped, return_value=b"a,b,c\n1,2,3\n"),
        )
        res_up = await ws_files_crud.upload_workspace_file(
            ws["workspace_id"],
            x_user_id="u-chao",
            path="data/model.csv",
            file=fake_upload,
        )
        assert res_up["path"] == "data/model.csv"
        assert acquired_for_users[-1] == "u-richard"

        with pytest.raises(HTTPException) as exc_symlink_upload:
            await ws_files_crud.upload_workspace_file(
                ws["workspace_id"],
                x_user_id="u-chao",
                path="notes/symlink_skill.md",
                file=fake_upload,
            )
        assert exc_symlink_upload.value.status_code == 403

        res_del = await ws_files_crud.delete_workspace_files(
            ws["workspace_id"],
            x_user_id="u-chao",
            body=ws_files_crud.DeleteFilesRequest(paths=["notes/thesis.md"]),
        )
        assert res_del["deleted"] == ["notes/thesis.md"]

        with pytest.raises(HTTPException) as exc_sys_del:
            await ws_files_crud.delete_workspace_files(
                ws["workspace_id"],
                x_user_id="u-chao",
                body=ws_files_crud.DeleteFilesRequest(
                    paths=[".agents/memory/secret.md"]
                ),
            )
        assert exc_sys_del.value.status_code == 403

        with pytest.raises(HTTPException) as exc_symlink_del:
            await ws_files_crud.delete_workspace_files(
                ws["workspace_id"],
                x_user_id="u-chao",
                body=ws_files_crud.DeleteFilesRequest(
                    paths=["notes/symlink_skill.md"]
                ),
            )
        assert exc_symlink_del.value.status_code == 403

        # 2. ro colleague -> 403 on write/upload/delete
        _set_sharing(monkeypatch, cos_access="ro", web_access="ro")
        with pytest.raises(HTTPException) as exc_ro_w:
            await ws_files_crud.write_workspace_file(
                ws["workspace_id"],
                x_user_id="u-chao",
                path="notes/thesis.md",
                body=ws_files_crud.WriteFileRequest(content="x"),
            )
        assert exc_ro_w.value.status_code == 403

        # 3. hidden colleague -> 404 on write (does not leak existence)
        _set_sharing(monkeypatch, cos_access="ro", web_access="hidden")
        with pytest.raises(HTTPException) as exc_hid_w:
            await ws_files_crud.write_workspace_file(
                ws["workspace_id"],
                x_user_id="u-chao",
                path="notes/thesis.md",
                body=ws_files_crud.WriteFileRequest(content="x"),
            )
        assert exc_hid_w.value.status_code == 404

        # 4. outsider -> 403 even when rw
        _set_sharing(monkeypatch, cos_access="rw", web_access="rw")
        with pytest.raises(HTTPException) as exc_out_w:
            await ws_files_crud.write_workspace_file(
                ws["workspace_id"],
                x_user_id="u-outsider",
                path="notes/thesis.md",
                body=ws_files_crud.WriteFileRequest(content="x"),
            )
        assert exc_out_w.value.status_code == 403

        # 5. Home workspace -> 403 for colleague even when rw
        home_ws = _sample_workspace(user_id="u-richard", is_home=True)
        shared_get_ws_mock.return_value = home_ws
        with pytest.raises(HTTPException) as exc_home_w:
            await ws_files_crud.write_workspace_file(
                home_ws["workspace_id"],
                x_user_id="u-chao",
                path="notes/thesis.md",
                body=ws_files_crud.WriteFileRequest(content="x"),
            )
        assert exc_home_w.value.status_code == 403

    @pytest.mark.asyncio
    async def test_owner_only_operations_still_rejected_for_rw_colleague(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _set_sharing(monkeypatch, cos_access="rw", web_access="rw")
        ws = _sample_workspace(user_id="u-richard", status="running")
        monkeypatch.setattr(
            workspaces_app,
            "db_get_workspace",
            create_autospec(workspaces_app.db_get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            workspaces_app.WorkspaceManager,
            "get_instance",
            create_autospec(
                workspaces_app.WorkspaceManager.get_instance,
                return_value=create_autospec(
                    workspaces_app.WorkspaceManager, instance=True
                ),
            ),
        )
        monkeypatch.setattr(
            ws_files_shared,
            "db_get_workspace",
            create_autospec(ws_files_shared.db_get_workspace, return_value=ws),
        )

        # Rename / config update -> 403
        with pytest.raises(HTTPException) as exc_upd:
            await workspaces_app.update_workspace(
                ws["workspace_id"],
                WorkspaceUpdate(name="Renamed"),
                x_user_id="u-chao",
            )
        assert exc_upd.value.status_code == 403

        # Delete workspace -> 403
        with pytest.raises(HTTPException) as exc_del:
            await workspaces_app.delete_workspace(ws["workspace_id"], x_user_id="u-chao")
        assert exc_del.value.status_code == 403

        # Start / Stop workspace -> 403
        with pytest.raises(HTTPException) as exc_start:
            await workspaces_app.start_workspace(
                ws["workspace_id"], x_user_id="u-chao", lazy=False
            )
        assert exc_start.value.status_code == 403

        # Backup files -> 403
        with pytest.raises(HTTPException) as exc_bak:
            await ws_files_crud.backup_workspace_files(
                ws["workspace_id"], x_user_id="u-chao"
            )
        assert exc_bak.value.status_code == 403


class TestChiefOfStaffChannel:
    """Test Chief of Staff tools across cos_access = hidden / ro / rw."""

    @pytest.mark.asyncio
    async def test_cos_workspaces_list_and_activity_include_visible_colleagues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
        own_ws = [
            {
                "workspace_id": "ws-own",
                "user_id": "u-chao",
                "name": "Chao Own",
                "description": "my ws",
                "dir_name": "Chao_Own",
                "computer_id": "c-1",
                "status": "running",
                "is_home": False,
                "created_at": now,
                "updated_at": now,
                "last_activity_at": now,
            }
        ]
        colleague_ws = [
            {
                "workspace_id": "ws-col-ro",
                "user_id": "u-richard",
                "owner_name": "Richard Lu",
                "owner_email": "richard.lu@dalton.test",
                "name": "NVDA RO",
                "description": "semi",
                "status": "running",
                "access": "ro",
                "cos_access": "ro",
                "web_access": "hidden",
                "read_only": True,
                "updated_at": now,
                "last_activity_at": now,
            },
            {
                "workspace_id": "ws-col-rw",
                "user_id": "u-richard",
                "owner_name": "Richard Lu",
                "owner_email": "richard.lu@dalton.test",
                "name": "TSM RW",
                "description": "foundry",
                "status": "stopped",
                "access": "rw",
                "cos_access": "rw",
                "web_access": "ro",
                "read_only": False,
                "updated_at": now,
                "last_activity_at": now,
            },
        ]

        monkeypatch.setattr(
            ws_db,
            "get_workspaces_for_user",
            create_autospec(
                ws_db.get_workspaces_for_user,
                return_value=(own_ws, 1),
            ),
        )
        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(
                ws_db.get_workspace,
                return_value={"workspace_id": "ws-home", "computer_id": "c-1"},
            ),
        )
        monkeypatch.setattr(
            home_ws_db,
            "get_home_workspace_id",
            create_autospec(
                home_ws_db.get_home_workspace_id,
                return_value="ws-home",
            ),
        )
        monkeypatch.setattr(
            dalton_sharing,
            "list_colleague_cos_workspaces",
            create_autospec(
                dalton_sharing.list_colleague_cos_workspaces,
                return_value=colleague_ws,
            ),
        )
        # Upstream's listing reads computer placement (names + home) instead of
        # the old get_workspace/get_home_workspace_id pair Dalton's version used.
        monkeypatch.setattr(
            computer_db,
            "get_computer_names_and_home",
            create_autospec(
                computer_db.get_computer_names_and_home,
                return_value=[{"computer_id": "c-1", "name": "Cloud computer", "is_home": True}],
            ),
        )

        cmd = await sec_tools.workspaces_list("u-chao", "tc-1")
        res = _cmd_json(cmd)
        assert res["success"] is True
        assert len(res["workspaces"]) == 1
        assert len(res["colleague_workspaces"]) == 2
        assert res["colleague_workspaces"][0]["access"] == "ro"
        assert res["colleague_workspaces"][0]["read_only"] is True
        assert res["colleague_workspaces"][1]["access"] == "rw"
        assert res["colleague_workspaces"][1]["read_only"] is False

        # Activity rendering includes colleague workspaces with owner and access annotations
        rendered = sec_activity.render_activity(
            workspaces=own_ws,
            workspace_total=1,
            threads=[],
            runs=[],
            holdings=[],
            home_id="ws-home",
            zone=ZoneInfo("UTC"),
            today=now.date(),
            placement=_ComputerPlacement(home_computer_id="c-1"),
            colleague_workspaces=colleague_ws,
        )
        assert "Colleague workspaces shared with your Chief of Staff" in rendered
        assert "NVDA RO" in rendered
        assert "owner: Richard Lu" in rendered
        assert "access: ro (read-only)" in rendered
        assert "access: rw (read-write)" in rendered

    @pytest.mark.asyncio
    async def test_cos_file_read_and_write_across_levels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard", status="stopped")
        cos_get_ws_mock = create_autospec(ws_db.get_workspace, return_value=ws)
        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            cos_get_ws_mock,
        )
        monkeypatch.setattr(
            ws_files_shared,
            "owner_work_dir",
            create_autospec(
                ws_files_shared.owner_work_dir,
                return_value="/home/workspace/NVDA_Deep_Dive",
            ),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "owner_work_dir",
            create_autospec(
                ws_files_crud.owner_work_dir,
                return_value="/home/workspace/NVDA_Deep_Dive",
            ),
        )
        monkeypatch.setattr(
            FilePersistenceService,
            "get_file_tree",
            create_autospec(
                FilePersistenceService.get_file_tree,
                return_value=[
                    {"path": "notes/summary.md"},
                    {"path": ".agents/memory/secret.md"},
                ],
            ),
        )
        monkeypatch.setattr(
            FilePersistenceService,
            "get_file_content",
            create_autospec(
                FilePersistenceService.get_file_content,
                return_value={
                    "file_path": "notes/summary.md",
                    "content_text": "Summary content",
                    "is_binary": False,
                },
            ),
        )
        monkeypatch.setattr(
            ws_files_shared,
            "http_file_text",
            create_autospec(ws_files_shared.http_file_text, return_value="Summary content"),
        )
        monkeypatch.setattr(
            secret_redactor,
            "get_vault_secrets_for_redaction",
            create_autospec(secret_redactor.get_vault_secrets_for_redaction, return_value=[]),
        )

        # 1. hidden -> list_files & read_file rejected
        _set_sharing(monkeypatch, cos_access="hidden", web_access="ro")
        res_hid = _cmd_json(
            await sec_tools.workspace_files_list_cos(
                "u-chao", ws["workspace_id"], None, "tc-1"
            )
        )
        assert res_hid["success"] is False
        assert "error" in res_hid

        # 2. ro -> list_files & read_file succeed (and hide .agents/...), write_file rejected
        _set_sharing(monkeypatch, cos_access="ro", web_access="hidden")
        res_ro_list = _cmd_json(
            await sec_tools.workspace_files_list_cos(
                "u-chao", ws["workspace_id"], None, "tc-2"
            )
        )
        assert res_ro_list["files"] == ["notes/summary.md"]

        res_ro_read = _cmd_json(
            await sec_tools.workspace_file_read_cos(
                "u-chao", ws["workspace_id"], "notes/summary.md", "tc-3"
            )
        )
        assert res_ro_read["content"] == "Summary content"

        res_ro_write = _cmd_json(
            await sec_tools.workspace_file_write_cos(
                "u-chao", ws["workspace_id"], "notes/summary.md", "new", "tc-4"
            )
        )
        assert res_ro_write["success"] is False
        assert res_ro_write["error"] == "该工作区对你只读"

        # 3. rw -> write_file succeeds on running workspace (and blocks .agents/... including symlink targets)
        _set_sharing(monkeypatch, cos_access="rw", web_access="hidden")
        running_ws = _sample_workspace(user_id="u-richard", status="running")
        cos_get_ws_mock.return_value = running_ws
        cos_live_fs: dict[str, bytes] = {}
        cos_symlinks = {
            "/home/workspace/NVDA_Deep_Dive/notes/symlink_skill.md": (
                "/home/workspace/NVDA_Deep_Dive/.agents/skills/alpha/SKILL.md"
            ),
        }

        async def _cos_awrite_file_text(filepath: str, content: str) -> bool:
            cos_live_fs[filepath] = content.encode("utf-8")
            return True

        async def _cos_aglob_files(
            pattern: str,
            path: str = ".",
            *,
            allow_denied: bool = False,
            hide_history: bool = False,
        ) -> list[str]:
            return [
                *cos_live_fs.keys(),
                "/home/workspace/NVDA_Deep_Dive/.agents/memory/secret.md",
            ]

        fake_cos_sandbox = create_autospec(PTCSandbox, instance=True)
        fake_cos_sandbox.validate_path.return_value = True
        fake_cos_sandbox.virtualize_path.side_effect = (
            lambda p, project=None: p.replace("/home/workspace/NVDA_Deep_Dive/", "")
        )
        fake_cos_sandbox.awrite_file_text.side_effect = _cos_awrite_file_text
        fake_cos_sandbox.aglob_files.side_effect = _cos_aglob_files

        @asynccontextmanager
        async def _fake_cos_acquire(workspace_id: str, user_id: str):
            assert user_id == "u-richard"
            yield fake_cos_sandbox, running_ws

        monkeypatch.setattr(
            ws_files_shared,
            "_acquire_sandbox_to_change",
            create_autospec(
                ws_files_shared._acquire_sandbox_to_change,
                side_effect=_fake_cos_acquire,
            ),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "_acquire_sandbox_to_change",
            create_autospec(
                ws_files_crud._acquire_sandbox_to_change,
                side_effect=_fake_cos_acquire,
            ),
        )

        async def _cos_resolve_path(
            sandbox: Any, candidate: str, *, work_dir: str
        ) -> str | None:
            return cos_symlinks.get(candidate, candidate)

        monkeypatch.setattr(
            ws_files_crud,
            "contained_sandbox_path",
            create_autospec(
                ws_files_crud.contained_sandbox_path,
                side_effect=_cos_resolve_path,
            ),
        )

        async def _cos_read_file(
            sandbox: Any, candidate: str, *, work_dir: str
        ) -> tuple[str, bytes] | None:
            resolved = cos_symlinks.get(candidate, candidate)
            if resolved not in cos_live_fs:
                return None
            return resolved, cos_live_fs[resolved]

        monkeypatch.setattr(
            ws_files_crud,
            "read_contained_sandbox_file",
            create_autospec(
                ws_files_crud.read_contained_sandbox_file,
                side_effect=_cos_read_file,
            ),
        )

        res_rw_sys = _cmd_json(
            await sec_tools.workspace_file_write_cos(
                "u-chao",
                running_ws["workspace_id"],
                ".agents/memory/secret.md",
                "SECRET=1",
                "tc-5",
            )
        )
        assert res_rw_sys["success"] is False

        res_rw_symlink = _cmd_json(
            await sec_tools.workspace_file_write_cos(
                "u-chao",
                running_ws["workspace_id"],
                "notes/symlink_skill.md",
                "hijack",
                "tc-5b",
            )
        )
        assert res_rw_symlink["success"] is False
        assert "403" in res_rw_symlink["error"]

        res_rw_ok = _cmd_json(
            await sec_tools.workspace_file_write_cos(
                "u-chao",
                running_ws["workspace_id"],
                "notes/colleague_note.md",
                "Hello from CoS",
                "tc-6",
            )
        )
        assert res_rw_ok["success"] is True
        assert res_rw_ok["path"] == "notes/colleague_note.md"

        # Immediately read & list from running workspace via CoS -> reads live sandbox
        res_rw_read_live = _cmd_json(
            await sec_tools.workspace_file_read_cos(
                "u-chao",
                running_ws["workspace_id"],
                "notes/colleague_note.md",
                "tc-7",
            )
        )
        assert res_rw_read_live["success"] is True
        assert res_rw_read_live["content"] == "Hello from CoS"

        res_rw_list_live = _cmd_json(
            await sec_tools.workspace_files_list_cos(
                "u-chao",
                running_ws["workspace_id"],
                None,
                "tc-8",
            )
        )
        assert res_rw_list_live["success"] is True
        assert res_rw_list_live["files"] == ["notes/colleague_note.md"]

    @pytest.mark.asyncio
    async def test_cos_threads_and_dispatch_across_levels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "test-internal-token")
        ws = _sample_workspace(user_id="u-richard", status="running")
        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(ws_db.get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            threads_read,
            "get_thread_owner_id",
            create_autospec(threads_read.get_thread_owner_id, return_value="u-richard"),
        )
        monkeypatch.setattr(
            conv_db,
            "get_thread_owner_id",
            create_autospec(conv_db.get_thread_owner_id, return_value="u-richard"),
        )
        monkeypatch.setattr(
            threads_read,
            "get_workspace_threads",
            create_autospec(
                threads_read.get_workspace_threads,
                return_value=(
                    [
                        {
                            "conversation_thread_id": "44444444-4444-4444-8444-444444444444",
                            "title": "Analysis",
                        }
                    ],
                    1,
                ),
            ),
        )

        cfg = {
            "configurable": {
                "user_id": "u-chao",
                "thread_id": "55555555-5555-4555-8555-555555555555",
            }
        }
        target_tid = "44444444-4444-4444-8444-444444444444"
        own_tid = "55555555-5555-4555-8555-555555555555"

        # hidden -> threads_list fails, dispatch fails with not found
        _set_sharing(monkeypatch, cos_access="hidden", web_access="ro")
        res_t_hid = _cmd_json(
            await sec_tools.threads_list("u-chao", ws["workspace_id"], "tc-1")
        )
        assert res_t_hid["success"] is False

        res_d_hid = _cmd_json(
            await sec_dispatch.dispatch(
                "run",
                cfg,
                ws["workspace_id"],
                None,
                False,
                "tc-2",
            )
        )
        assert "not found" in res_d_hid["error"].lower()

        # ro -> threads_list succeeds, threads_stop/delete rejected (owner-only), dispatch rejected as read-only
        _set_sharing(monkeypatch, cos_access="ro", web_access="hidden")
        res_t_ro = _cmd_json(
            await sec_tools.threads_list("u-chao", ws["workspace_id"], "tc-3")
        )
        assert res_t_ro["success"] is True
        assert len(res_t_ro["threads"]) == 1

        res_stop = _cmd_json(
            await sec_tools.threads_stop("u-chao", target_tid, own_tid, "tc-4")
        )
        assert res_stop["success"] is False

        res_del = _cmd_json(await sec_tools.threads_delete("u-chao", target_tid, "tc-5"))
        assert res_del["success"] is False

        res_d_ro = _cmd_json(
            await sec_dispatch.dispatch(
                "run",
                cfg,
                ws["workspace_id"],
                None,
                False,
                "tc-6",
            )
        )
        assert "read-only" in res_d_ro["error"].lower()

        # rw -> dispatch returns Phase 2 deferral prompt
        _set_sharing(monkeypatch, cos_access="rw", web_access="hidden")
        res_d_rw = _cmd_json(
            await sec_dispatch.dispatch(
                "run",
                cfg,
                ws["workspace_id"],
                None,
                False,
                "tc-7",
            )
        )
        assert "该功能下一期开放" in res_d_rw["error"]

    @pytest.mark.asyncio
    async def test_cos_knowledge_tools_across_levels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")
        get_ws_mock = create_autospec(ws_db.get_workspace, return_value=ws)
        monkeypatch.setattr(ws_db, "get_workspace", get_ws_mock)
        monkeypatch.setattr(
            db_knowledge,
            "list_all_latest_for_workspace",
            create_autospec(
                db_knowledge.list_all_latest_for_workspace,
                return_value={
                    "thesis": {
                        "version_no": 2,
                        "content": {"summary": "NVDA Thesis"},
                    }
                },
            ),
        )
        monkeypatch.setattr(
            db_verdicts,
            "list_verdicts",
            create_autospec(
                db_verdicts.list_verdicts,
                return_value=[{"verdict_id": "v-1", "summary": "Bullish"}],
            ),
        )
        monkeypatch.setattr(
            kt_mod,
            "apply_update",
            create_autospec(
                knowledge_service.apply_update,
                return_value={
                    "ok": True,
                    "version": 3,
                    "message": "Updated thesis to version 3",
                },
            ),
        )
        monkeypatch.setattr(
            kt_mod,
            "append_verdict",
            create_autospec(
                knowledge_service.append_verdict,
                return_value={
                    "ok": True,
                    "verdict_id": "v-2",
                    "message": "Appended verdict v-2",
                },
            ),
        )
        monkeypatch.setattr(
            kt_mod,
            "_resolve_thread_origin",
            create_autospec(kt_mod._resolve_thread_origin, return_value=(None, None)),
        )

        tools = {t.name: t for t in create_knowledge_tools(None)}
        assert {"knowledge_read", "knowledge_update", "knowledge_append_verdict"} <= set(
            tools.keys()
        )
        cfg = {
            "configurable": {
                "user_id": "u-chao",
                "workspace_id": "ws-home",
                "thread_id": "t-cos",
            }
        }

        def _tc(name: str, args: dict[str, Any]) -> dict[str, Any]:
            return {
                "name": name,
                "id": "tc-k",
                "type": "tool_call",
                "args": args,
            }

        # 1. hidden -> knowledge_read & knowledge_update raise 404
        _set_sharing(monkeypatch, cos_access="hidden", web_access="ro")
        with pytest.raises(HTTPException) as exc_r_hid:
            await tools["knowledge_read"].ainvoke(
                {"workspace_id": ws["workspace_id"]}, config=cfg
            )
        assert exc_r_hid.value.status_code == 404

        with pytest.raises(HTTPException) as exc_u_hid:
            await tools["knowledge_update"].ainvoke(
                _tc(
                    "knowledge_update",
                    {
                        "workspace_id": ws["workspace_id"],
                        "doc": "thesis",
                        "ops": [],
                        "reason": "test",
                    },
                ),
                config=cfg,
            )
        assert exc_u_hid.value.status_code == 404

        # 2. ro -> knowledge_read succeeds, knowledge_update & knowledge_append_verdict raise 403
        _set_sharing(monkeypatch, cos_access="ro", web_access="hidden")
        r_ro = await tools["knowledge_read"].ainvoke(
            {"workspace_id": ws["workspace_id"]}, config=cfg
        )
        assert "NVDA Thesis" in str(r_ro)

        with pytest.raises(HTTPException) as exc_u_ro:
            await tools["knowledge_update"].ainvoke(
                _tc(
                    "knowledge_update",
                    {
                        "workspace_id": ws["workspace_id"],
                        "doc": "thesis",
                        "ops": [],
                        "reason": "test",
                    },
                ),
                config=cfg,
            )
        assert exc_u_ro.value.status_code == 403

        with pytest.raises(HTTPException) as exc_v_ro:
            await tools["knowledge_append_verdict"].ainvoke(
                _tc(
                    "knowledge_append_verdict",
                    {
                        "workspace_id": ws["workspace_id"],
                        "verdict": {"verdict": "NO_CHANGE"},
                    },
                ),
                config=cfg,
            )
        assert exc_v_ro.value.status_code == 403

        # 3. rw -> knowledge_read, knowledge_update, knowledge_append_verdict all succeed
        _set_sharing(monkeypatch, cos_access="rw", web_access="hidden")
        u_rw = await tools["knowledge_update"].ainvoke(
            _tc(
                "knowledge_update",
                {
                    "workspace_id": ws["workspace_id"],
                    "doc": "thesis",
                    "ops": [],
                    "reason": "test",
                },
            ),
            config=cfg,
        )
        u_msg = u_rw.content if hasattr(u_rw, "content") else str(u_rw)
        assert "Updated thesis to version 3" in str(u_msg)

        v_rw = await tools["knowledge_append_verdict"].ainvoke(
            _tc(
                "knowledge_append_verdict",
                {
                    "workspace_id": ws["workspace_id"],
                    "verdict": {"verdict": "NO_CHANGE"},
                },
            ),
            config=cfg,
        )
        v_msg = v_rw.content if hasattr(v_rw, "content") else str(v_rw)
        assert "Appended verdict v-2" in str(v_msg)

    def test_chief_of_staff_tools_schema_includes_file_params(self) -> None:
        cos_tools = {t.name: t for t in CHIEF_OF_STAFF_TOOLS}
        assert "manage_workspaces" in cos_tools
        mw = cos_tools["manage_workspaces"]
        schema = mw.args_schema.model_json_schema()
        assert "path" in schema.get("properties", {})
        assert "content" in schema.get("properties", {})
        for expected_doc_token in ("list_files", "read_file", "write_file", "path", "content"):
            assert expected_doc_token in (mw.description or "")

    @pytest.mark.asyncio
    async def test_owner_sees_colleague_authored_knowledge_versions_and_verdicts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard", status="running")
        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(ws_db.get_workspace, return_value=ws),
        )

        colleague_version = {
            "knowledge_version_id": "kv-2",
            "workspace_id": ws["workspace_id"],
            "user_id": "u-chao",
            "doc_key": "thesis",
            "version_no": 2,
            "content": {"summary": "Colleague update"},
        }
        monkeypatch.setattr(
            db_knowledge,
            "list_versions",
            create_autospec(
                db_knowledge.list_versions,
                return_value=[colleague_version],
            ),
        )
        monkeypatch.setattr(
            db_knowledge,
            "get_version",
            create_autospec(
                db_knowledge.get_version,
                return_value=colleague_version,
            ),
        )

        # Owner can list and get version authored by colleague u-chao
        versions_res = await dalton_router.list_knowledge_versions_endpoint(
            workspace_id=ws["workspace_id"],
            doc="thesis",
            user_id="u-richard",
            limit=20,
            before=None,
        )
        assert versions_res["items"] == [colleague_version]
        assert versions_res["items"][0]["user_id"] == "u-chao"

        detail_res = await dalton_router.get_knowledge_version_endpoint(
            workspace_id=ws["workspace_id"],
            doc="thesis",
            n=2,
            user_id="u-richard",
        )
        assert detail_res["version_no"] == 2
        assert detail_res["user_id"] == "u-chao"

        # Verdicts query by workspace_id does not filter out colleague-authored verdicts
        executed_verdict_queries: list[tuple[str, tuple[Any, ...]]] = []
        fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)
        fake_cur = create_autospec(psycopg.AsyncCursor, instance=True)

        async def _capture_verdict_exec(sql: str, params: Any = None) -> None:
            executed_verdict_queries.append((sql, tuple(params or ())))

        fake_cur.execute.side_effect = _capture_verdict_exec
        fake_cur.fetchall.return_value = [
            {
                "verdict_row_id": "vr-1",
                "user_id": "u-chao",
                "workspace_id": ws["workspace_id"],
                "verdict_id": "VD-20260420-01",
                "verdict_at": datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
                "word": "REINFORCE",
                "payload": {"event": "Colleague verdict"},
                "conversation_thread_id": None,
                "run_id": None,
                "automation_execution_id": None,
                "origin_type": "manual",
                "created_at": datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
            }
        ]

        @asynccontextmanager
        async def _cur_ctx(*args: Any, **kwargs: Any):
            yield fake_cur

        @asynccontextmanager
        async def _db_ctx(conn: Any = None):
            yield fake_conn

        fake_conn.cursor.side_effect = _cur_ctx
        monkeypatch.setattr(
            db_verdicts,
            "get_db_connection",
            create_autospec(db_verdicts.get_db_connection, side_effect=_db_ctx),
        )

        verdicts_res = await dalton_router.list_verdicts_endpoint(
            user_id="u-richard",
            workspace_id=ws["workspace_id"],
            since=None,
            until=None,
            limit=20,
        )
        assert len(verdicts_res["items"]) == 1
        assert verdicts_res["items"][0]["user_id"] == "u-chao"
        assert len(executed_verdict_queries) == 1
        v_sql, v_params = executed_verdict_queries[0]
        assert "workspace_id = %s" in v_sql
        assert "user_id = %s" not in v_sql
        assert v_params == (ws["workspace_id"], 20)
