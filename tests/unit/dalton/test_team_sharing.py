"""Unit tests for Dalton team read-only sharing (src/dalton/sharing.py + hooked routes)."""

from __future__ import annotations

import io
import zipfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import create_autospec
from uuid import uuid4

import psycopg
import pytest
from fastapi import HTTPException, Request, Response, UploadFile
from starlette.datastructures import Headers

from src.config import settings as app_settings
from src.dalton import sharing as dalton_sharing
from src.dalton.app import router as dalton_router
from src.dalton.auth import _verified_token_identity
from src.dalton.knowledge import service as knowledge_service
from src.dalton.sharing import (
    list_team_workspaces,
    require_team_reader,
    require_thread_reader,
    require_workspace_reader,
)
from src.server.app import skills as skills_app
from src.server.app import workspaces as workspaces_app
from src.server.app.threads import crud as threads_crud
from src.server.app.threads import messaging as threads_messaging
from src.server.app.threads import provenance as threads_provenance
from src.server.app.threads import tasks as threads_tasks
from src.server.app.workspace_files import crud as ws_files_crud
from src.server.app.workspace_files.file_refs import ResolveFileRefRequest
from src.server.dependencies.principal import Principal
from src.server.database import conversation as conv_db
from src.server.database import user as user_db
from src.server.database import workspace as ws_db
from src.server.database.conversation.replay_rows import ThreadRows
from src.server.dependencies.usage_limits import ChatAuthResult
from src.server.models.chat import ChatRequest
from src.server.models.conversation import ThreadUpdateRequest
from src.server.models.workspace import WorkspaceUpdate
from src.server.services import workspace_files as ws_files_service
from src.server.services.persistence.file import FilePersistenceService
from src.server.services.turn_runtime import requested_workspace
from src.server.services.workspace_file_containment import (
    ContainedSlice,
    FileTooLargeToServe,
)
from src.ptc_agent.core.sandbox import PTCSandbox

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
    "u-rex": {
        "user_id": "u-rex",
        "email": "rex.yuan@dalton.test",
        "name": "Rex Yuan",
    },
    "u-mark": {
        "user_id": "u-mark",
        "email": "mark.xie@dalton.test",
        "name": "Mark Xie",
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
    monkeypatch.setenv("DALTON_ALLOWED_EMAILS", TEAM_EMAILS)

    async def _fake_get_user(uid: str) -> dict[str, Any] | None:
        return USERS_BY_ID.get(uid)

    get_user_mock = create_autospec(user_db.get_user, side_effect=_fake_get_user)
    monkeypatch.setattr(user_db, "get_user", get_user_mock)

    async def _fake_get_sharing(
        workspace_id: str, *, conn: Any | None = None
    ) -> dict[str, Any]:
        return {
            "workspace_id": workspace_id,
            "cos_access": "ro",
            "web_access": "ro",
            "updated_by": None,
            "updated_at": None,
        }

    get_sharing_mock = create_autospec(
        dalton_sharing.get_workspace_sharing, side_effect=_fake_get_sharing
    )
    monkeypatch.setattr(dalton_sharing, "get_workspace_sharing", get_sharing_mock)
    monkeypatch.setattr(
        conv_db,
        "user_has_threads_in_workspace",
        create_autospec(conv_db.user_has_threads_in_workspace, return_value=False),
    )


def _disable_dalton_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
    monkeypatch.setenv("DALTON_ALLOWED_EMAILS", TEAM_EMAILS)


# Captured at import, before any fixture patches the module: the spec source
# for re-patching get_workspace_sharing with a different level mid-test.
_REAL_GET_WORKSPACE_SHARING = dalton_sharing.get_workspace_sharing


def _set_web_sharing(monkeypatch: pytest.MonkeyPatch, *, web_access: str) -> None:
    """Point the sharing lookup at one fixed web level (fixture default: ro)."""

    async def _fake(workspace_id: str, *, conn: Any | None = None) -> dict[str, Any]:
        return {
            "workspace_id": workspace_id,
            "cos_access": "ro",
            "web_access": web_access,
            "updated_by": None,
            "updated_at": None,
        }

    monkeypatch.setattr(
        dalton_sharing,
        "get_workspace_sharing",
        create_autospec(_REAL_GET_WORKSPACE_SHARING, side_effect=_fake),
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
        "is_pinned": False,
        "sort_order": 0,
        "resource_tier": "standard",
        "is_always_on": False,
        "files_restore_incomplete": False,
    }


def _make_request(headers: dict[str, str] | None = None) -> Request:
    req = create_autospec(Request, instance=True)
    req.headers = Headers(headers or {})
    return req


class TestPermissionPrimitives:
    """Tests for require_team_reader, require_workspace_reader, require_thread_reader."""

    @pytest.mark.asyncio
    async def test_colleague_can_read_workspace_when_dalton_auth_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")
        result = await require_workspace_reader(ws, "u-chao")
        assert result["workspace_id"] == ws["workspace_id"]

    @pytest.mark.asyncio
    async def test_colleague_cannot_read_home_workspace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        home_ws = _sample_workspace(user_id="u-richard", is_home=True, name="Home")
        with pytest.raises(HTTPException) as exc_info:
            await require_workspace_reader(home_ws, "u-chao")
        assert exc_info.value.status_code == 403

        # Owner can still read their own Home workspace
        own_res = await require_workspace_reader(home_ws, "u-richard")
        assert own_res["user_id"] == "u-richard"

    @pytest.mark.asyncio
    async def test_non_allowlisted_or_unauthenticated_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")

        with pytest.raises(HTTPException) as exc_unauth:
            await require_workspace_reader(ws, "")
        assert exc_unauth.value.status_code == 401

        with pytest.raises(HTTPException) as exc_outsider:
            await require_workspace_reader(ws, "u-outsider")
        assert exc_outsider.value.status_code == 403

        # Off-allowlist owners classify as external-owned: hidden (404), not 403
        outsider_ws = _sample_workspace(user_id="u-outsider")
        with pytest.raises(HTTPException) as exc_outsider_owner:
            await require_workspace_reader(outsider_ws, "u-chao")
        assert exc_outsider_owner.value.status_code == 404

        token = _verified_token_identity.set(("u-outsider", "outsider@other.com"))
        try:
            with pytest.raises(HTTPException) as exc_ctx:
                await require_team_reader("u-outsider")
            assert exc_ctx.value.status_code == 403
        finally:
            _verified_token_identity.reset(token)

    @pytest.mark.asyncio
    async def test_require_thread_reader_rejects_home_deleted_and_outsider_owner_threads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        thread_id = "33333333-3333-4333-8333-333333333333"

        # 1. Colleague reading a Home workspace thread -> 403
        home_ws = _sample_workspace(user_id="u-richard", is_home=True, name="Home")
        auth_meta_mock = create_autospec(
            conv_db.get_thread_auth_meta,
            return_value={
                "user_id": "u-richard",
                "workspace_id": home_ws["workspace_id"],
                "is_shared": False,
                "msg_type": "ptc",
                "llm_model": None,
            },
        )
        monkeypatch.setattr(conv_db, "get_thread_auth_meta", auth_meta_mock)
        get_ws_mock = create_autospec(ws_db.get_workspace, return_value=home_ws)
        monkeypatch.setattr(ws_db, "get_workspace", get_ws_mock)
        with pytest.raises(HTTPException) as exc_home_th:
            await require_thread_reader(thread_id, "u-chao")
        assert exc_home_th.value.status_code == 403

        # Owner can still read their own Home thread
        assert await require_thread_reader(thread_id, "u-richard") == "u-richard"

        # 2. Colleague reading a deleted workspace thread -> 403
        deleted_ws = _sample_workspace(user_id="u-richard", status="deleted")
        get_ws_mock.return_value = deleted_ws
        with pytest.raises(HTTPException) as exc_del_th:
            await require_thread_reader(thread_id, "u-chao")
        assert exc_del_th.value.status_code == 403

        # 3. Colleague reading a thread owned by a non-allowlisted user -> 404
        #    (external-owned: hidden, no existence leak)
        outsider_ws = _sample_workspace(user_id="u-outsider", status="running")
        auth_meta_mock.return_value = {
            "user_id": "u-outsider",
            "workspace_id": outsider_ws["workspace_id"],
            "is_shared": False,
            "msg_type": "ptc",
            "llm_model": None,
        }
        get_ws_mock.return_value = outsider_ws
        with pytest.raises(HTTPException) as exc_out_th:
            await require_thread_reader(thread_id, "u-chao")
        assert exc_out_th.value.status_code == 404

    @pytest.mark.asyncio
    async def test_switch_off_enforces_strict_owner_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _disable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")

        with pytest.raises(HTTPException) as exc_ws:
            await require_workspace_reader(ws, "u-chao")
        assert exc_ws.value.status_code == 403

        with pytest.raises(HTTPException) as exc_th:
            await require_thread_reader(
                "33333333-3333-4333-8333-333333333333",
                "u-chao",
                owner_id="u-richard",
            )
        assert exc_th.value.status_code == 403

        assert await list_team_workspaces("u-chao") == []


class TestTeamWorkspacesEndpoint:
    """Tests for GET /api/v1/dalton/workspaces/team."""

    @pytest.mark.asyncio
    async def test_lists_colleagues_workspaces_grouped_by_person_excluding_self(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        now = datetime(2026, 4, 20, 15, 30, tzinfo=UTC)

        fake_rows = [
            {
                "workspace_id": "ws-richard-1",
                "user_id": "u-richard",
                "name": "META Q1 Earnings",
                "description": "Meta analysis",
                "status": "running",
                "created_at": now,
                "updated_at": now,
                "last_activity_at": now,
                "owner_email": "richard.lu@dalton.test",
                "owner_name": "Richard Lu",
                "owner_avatar_url": None,
                "web_access": "ro",
            },
            {
                "workspace_id": "ws-rex-1",
                "user_id": "u-rex",
                "name": "TSMC Supply Chain",
                "description": None,
                "status": "stopped",
                "created_at": now,
                "updated_at": now,
                "last_activity_at": now,
                "owner_email": "rex.yuan@dalton.test",
                "owner_name": "",
                "owner_avatar_url": None,
                "web_access": "ro",
            },
        ]

        mock_cur = create_autospec(psycopg.AsyncCursor, instance=True)
        mock_cur.fetchall.return_value = fake_rows
        mock_cur.__aenter__.return_value = mock_cur
        mock_cur.__aexit__.return_value = False

        mock_conn = create_autospec(psycopg.AsyncConnection, instance=True)
        mock_conn.cursor.return_value = mock_cur
        mock_conn.__aenter__.return_value = mock_conn
        mock_conn.__aexit__.return_value = False

        get_db_mock = create_autospec(
            dalton_sharing.get_db_connection, return_value=mock_conn
        )
        monkeypatch.setattr(dalton_sharing, "get_db_connection", get_db_mock)

        resp = await dalton_router.list_team_workspaces_endpoint(user_id="u-chao")
        members = resp["members"]
        assert len(members) == 2
        assert members[0]["user_id"] == "u-richard"
        assert members[0]["display_name"] == "Richard Lu"
        assert len(members[0]["workspaces"]) == 1
        assert members[0]["workspaces"][0]["name"] == "META Q1 Earnings"
        assert members[1]["user_id"] == "u-rex"
        assert members[1]["display_name"] == "rex.yuan"
        assert all(m["user_id"] != "u-chao" for m in members)

    @pytest.mark.asyncio
    async def test_lists_colleagues_workspaces_hides_unconfigured_workspaces_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        now = datetime(2026, 4, 20, 15, 30, tzinfo=UTC)

        fake_rows = [
            {
                "workspace_id": "ws-unconfigured",
                "user_id": "u-richard",
                "name": "Private Draft",
                "description": None,
                "status": "running",
                "created_at": now,
                "updated_at": now,
                "last_activity_at": now,
                "owner_email": "richard.lu@dalton.test",
                "owner_name": "Richard Lu",
                "owner_avatar_url": None,
                "web_access": None,
                "cos_access": None,
            },
            {
                "workspace_id": "ws-shared-ro",
                "user_id": "u-richard",
                "name": "Shared Note",
                "description": None,
                "status": "running",
                "created_at": now,
                "updated_at": now,
                "last_activity_at": now,
                "owner_email": "richard.lu@dalton.test",
                "owner_name": "Richard Lu",
                "owner_avatar_url": None,
                "web_access": "ro",
                "cos_access": "rw",
            },
        ]

        mock_cur = create_autospec(psycopg.AsyncCursor, instance=True)
        mock_cur.fetchall.return_value = fake_rows
        mock_cur.__aenter__.return_value = mock_cur
        mock_cur.__aexit__.return_value = False

        mock_conn = create_autospec(psycopg.AsyncConnection, instance=True)
        mock_conn.cursor.return_value = mock_cur
        mock_conn.__aenter__.return_value = mock_conn
        mock_conn.__aexit__.return_value = False

        get_db_mock = create_autospec(
            dalton_sharing.get_db_connection, return_value=mock_conn
        )
        monkeypatch.setattr(dalton_sharing, "get_db_connection", get_db_mock)

        resp = await dalton_router.list_team_workspaces_endpoint(user_id="u-chao")
        members = resp["members"]
        assert len(members) == 1
        assert [w["workspace_id"] for w in members[0]["workspaces"]] == ["ws-shared-ro"]


class TestReadEndpointsAllowColleagues:
    """Tests that hooked read endpoints allow teammates when Dalton auth is on and reject when off."""

    @pytest.mark.asyncio
    async def test_get_workspace_masks_computer_and_sandbox_for_colleague(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")
        get_ws_mock = create_autospec(workspaces_app.db_get_workspace, return_value=ws)
        monkeypatch.setattr(workspaces_app, "db_get_workspace", get_ws_mock)

        # Colleague read succeeds and masks sandbox_id / computer_id
        colleague_resp = await workspaces_app.get_workspace(
            ws["workspace_id"], x_user_id="u-chao"
        )
        assert colleague_resp.workspace_id == ws["workspace_id"]
        assert colleague_resp.user_id == "u-richard"
        assert colleague_resp.sandbox_id is None
        assert colleague_resp.computer_id is None

        # Owner read keeps sandbox_id / computer_id
        owner_resp = await workspaces_app.get_workspace(
            ws["workspace_id"], x_user_id="u-richard"
        )
        assert owner_resp.sandbox_id == "sbx-secret-1"
        assert owner_resp.computer_id == "22222222-2222-4222-8222-222222222222"

        # Switch off -> colleague rejected with 403
        _disable_dalton_auth(monkeypatch)
        with pytest.raises(HTTPException) as exc_off:
            await workspaces_app.get_workspace(ws["workspace_id"], x_user_id="u-chao")
        assert exc_off.value.status_code == 403

    @pytest.mark.asyncio
    async def test_thread_list_and_detail_allow_colleague(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")
        thread_id = "33333333-3333-4333-8333-333333333333"
        now = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
        sample_thread = {
            "conversation_thread_id": thread_id,
            "workspace_id": ws["workspace_id"],
            "thread_index": 0,
            "current_status": "completed",
            "msg_type": "ptc",
            "title": "NVDA Margin Model",
            "first_query_content": "Analyze gross margin",
            "platform": "web",
            "metadata": {},
            "is_shared": False,
            "is_pinned": False,
            "archived_at": None,
            "llm_model": "claude-opus-4-6",
            "subagents_allowed": True,
            "turn_count": 2,
            "created_at": now,
            "updated_at": now,
        }

        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(ws_db.get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            threads_crud,
            "get_workspace_threads",
            create_autospec(
                threads_crud.get_workspace_threads,
                return_value=([sample_thread], 1),
            ),
        )
        monkeypatch.setattr(
            conv_db,
            "get_thread_auth_meta",
            create_autospec(
                conv_db.get_thread_auth_meta,
                return_value={
                    "user_id": "u-richard",
                    "workspace_id": ws["workspace_id"],
                    "is_shared": False,
                    "msg_type": "ptc",
                    "llm_model": None,
                },
            ),
        )
        monkeypatch.setattr(
            threads_crud,
            "get_thread_by_id",
            create_autospec(threads_crud.get_thread_by_id, return_value=sample_thread),
        )

        list_resp = await threads_crud.list_threads(
            principal=Principal(user_id="u-chao", auth="session"),
            workspace_id=ws["workspace_id"],
            limit=20,
            offset=0,
            sort_by="updated_at",
            sort_order="desc",
            platform_prefix=None,
            archived=False,
        )
        assert list_resp.total == 1
        assert list_resp.threads[0].thread_id == thread_id

        detail_resp = await threads_crud.get_thread(thread_id, x_user_id="u-chao")
        assert detail_resp.thread_id == thread_id

    @pytest.mark.asyncio
    async def test_replay_and_thread_status_allow_colleague_without_live_reconnect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")
        thread_id = "33333333-3333-4333-8333-333333333333"
        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(ws_db.get_workspace, return_value=ws),
        )
        from langchain_core.messages import AIMessage, HumanMessage
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from src.server.database.conversation import turn_slices as slices_db
        from src.server.services.history.reader import CheckpointHistoryReader, TurnAnchor
        from src.server.services.history.slices import TurnSlice

        anchor = TurnAnchor(
            turn_ordinal=0,
            turn_index=0,
            input_checkpoint_id="cp-0",
            tail_checkpoint_id="cp-1",
        )
        replay_rows = ThreadRows(
            thread_id=thread_id,
            owner_id="u-richard",
            thread={
                "conversation_thread_id": thread_id,
                "workspace_id": ws["workspace_id"],
                "latest_run_id": "run-1",
                "latest_checkpoint_id": "cp-1",
            },
            queries=[
                {
                    "conversation_query_id": "11111111-1111-4111-8111-111111111111",
                    "turn_index": 0,
                    "content": "Evaluate NVDA",
                    "type": "initial",
                    "created_at": datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
                }
            ],
            responses_by_turn={
                0: {
                    "conversation_response_id": "22222222-2222-4222-8222-222222222222",
                    "turn_index": 0,
                    "status": "completed",
                    "replay_facts": {},
                    "created_at": datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
                    "updated_at": datetime(2026, 4, 20, 12, 1, tzinfo=UTC),
                }
            },
            usages=[],
            provenance=[],
            run_facts=[],
        )
        monkeypatch.setattr(
            threads_messaging,
            "get_replay_thread_data",
            create_autospec(
                threads_messaging.get_replay_thread_data, return_value=replay_rows
            ),
        )
        fake_reader = create_autospec(CheckpointHistoryReader, instance=True)
        fake_reader.serde = JsonPlusSerializer()
        fake_reader.aget_turn_anchors.return_value = ([anchor], "cp-1")
        fake_reader.aget_tip_interrupts.return_value = []
        fake_reader.aget_turn_slices.return_value = [
            TurnSlice(
                anchor=anchor,
                messages=[
                    HumanMessage(content="Evaluate NVDA", id="msg-u-1"),
                    AIMessage(content="Strong buy thesis", id="msg-a-1"),
                ],
            )
        ]
        monkeypatch.setattr(
            CheckpointHistoryReader,
            "get_instance",
            create_autospec(
                CheckpointHistoryReader.get_instance, return_value=fake_reader
            ),
        )
        monkeypatch.setattr(
            slices_db,
            "get_turn_lines",
            create_autospec(slices_db.get_turn_lines, return_value={}),
        )
        monkeypatch.setattr(
            slices_db,
            "get_turn_slices",
            create_autospec(slices_db.get_turn_slices, return_value={}),
        )
        upsert_runs_spy = create_autospec(slices_db.upsert_run_slices)
        upsert_turns_spy = create_autospec(slices_db.upsert_turn_slices)
        update_turns_spy = create_autospec(slices_db.update_turns)
        monkeypatch.setattr(slices_db, "upsert_run_slices", upsert_runs_spy)
        monkeypatch.setattr(slices_db, "upsert_turn_slices", upsert_turns_spy)
        monkeypatch.setattr(slices_db, "update_turns", update_turns_spy)

        replay_resp = await threads_messaging.replay_thread_messages(
            thread_id=thread_id,
            x_user_id="u-rex",
            source="auto",
            limit=None,
            before_turn=None,
        )
        assert replay_resp.media_type == "text/event-stream"
        # Full projection chain ran through read_replay_page -> build_replay_page -> cold.project_stale
        fake_reader.aget_turn_slices.assert_called_once()
        upsert_runs_spy.assert_not_called()
        upsert_turns_spy.assert_not_called()
        update_turns_spy.assert_not_called()

        monkeypatch.setattr(
            conv_db,
            "get_thread_auth_meta",
            create_autospec(
                conv_db.get_thread_auth_meta,
                return_value={
                    "user_id": "u-richard",
                    "is_shared": True,
                    "msg_type": "ptc",
                    "workspace_id": ws["workspace_id"],
                    "llm_model": None,
                },
            ),
        )
        from src.server.database.runs import lifecycle as tl_db
        from src.server.database.runs import outbox as outbox_db
        from src.server.services import thread_status as ts_mod
        from src.server.services.report_back.handoff import status as rb_status_mod

        runtime_status_spy = create_autospec(ts_mod.read_thread_runtime_status)
        report_back_spy = create_autospec(rb_status_mod.read_report_back_slice)
        pending_steers_spy = create_autospec(outbox_db.list_pending_steers)
        monkeypatch.setattr(ts_mod, "read_thread_runtime_status", runtime_status_spy)
        monkeypatch.setattr(rb_status_mod, "read_report_back_slice", report_back_spy)
        monkeypatch.setattr(outbox_db, "list_pending_steers", pending_steers_spy)
        monkeypatch.setattr(
            tl_db,
            "get_latest_attempt",
            create_autospec(
                tl_db.get_latest_attempt,
                return_value={
                    "conversation_response_id": "run-secret-1",
                    "status": "in_progress",
                    "cancel_requested_at": None,
                    "created_at": datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
                },
            ),
        )
        monkeypatch.setattr(
            conv_db,
            "get_latest_turn_index",
            create_autospec(conv_db.get_latest_turn_index, return_value=3),
        )

        status_resp = await threads_messaging.get_thread_status(
            thread_id=thread_id, x_user_id="u-rex", fields=None
        )
        assert status_resp["status"] == "running"
        assert status_resp["can_reconnect"] is False
        assert status_resp["run_id"] is None
        assert status_resp["token_totals"] is None
        assert status_resp["owed_threads"] == []
        assert status_resp["pending_steers"] == []
        assert status_resp["active_tasks"] == []
        assert status_resp["is_shared"] is False

        rb_resp = await threads_messaging.get_thread_status(
            thread_id=thread_id, x_user_id="u-rex", fields="report_back"
        )
        assert rb_resp["owed_threads"] == []
        assert rb_resp["pending_report_back"] is False

        runtime_status_spy.assert_not_called()
        report_back_spy.assert_not_called()
        pending_steers_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_provenance_and_subagent_tasks_allow_colleague(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")
        thread_id = "33333333-3333-4333-8333-333333333333"
        monkeypatch.setattr(
            conv_db,
            "get_thread_auth_meta",
            create_autospec(
                conv_db.get_thread_auth_meta,
                return_value={
                    "user_id": "u-richard",
                    "workspace_id": ws["workspace_id"],
                    "is_shared": False,
                    "msg_type": "ptc",
                    "llm_model": None,
                },
            ),
        )
        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(ws_db.get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            threads_provenance,
            "get_provenance_for_thread",
            create_autospec(
                threads_provenance.get_provenance_for_thread, return_value=[]
            ),
        )
        prov = await threads_provenance.get_provenance(thread_id, x_user_id="u-mark")
        assert prov["thread_id"] == thread_id

        from src.server.services.history import task_status as task_status_mod
        from src.server.services.history.replay import run_lane as run_lane_mod

        monkeypatch.setattr(
            task_status_mod,
            "resolve_task_details",
            create_autospec(
                task_status_mod.resolve_task_details,
                return_value={"k1": {"status": "completed", "error": None}},
            ),
        )
        monkeypatch.setattr(
            run_lane_mod,
            "project_task_transcript",
            create_autospec(run_lane_mod.project_task_transcript, return_value=[]),
        )

        st = await threads_tasks.get_subagent_task_status(
            thread_id, "k1", x_user_id="u-mark"
        )
        assert st["status"] == "completed"
        hist = await threads_tasks.get_subagent_task_history(
            thread_id, "k1", x_user_id="u-mark"
        )
        assert hist["items"] == []

    @pytest.mark.asyncio
    async def test_workspace_files_read_mirror_only_never_touching_sandbox(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        # Workspace is 'stopped': colleague reads MUST come from DB mirror and never start/acquire sandbox even with auto_start=True
        ws = _sample_workspace(user_id="u-richard", status="stopped")
        monkeypatch.setattr(
            ws_files_crud,
            "db_get_workspace",
            create_autospec(ws_files_crud.db_get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            ws_files_service,
            "db_get_workspace",
            create_autospec(ws_files_service.db_get_workspace, return_value=ws),
        )
        acquire_spy = create_autospec(ws_files_crud.acquire_sandbox_to_change)
        monkeypatch.setattr(ws_files_crud, "acquire_sandbox_to_change", acquire_spy)
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
                return_value=[{"path": "results/memo.md"}, {"path": "data/rev.csv"}],
            ),
        )
        get_file_content_mock = create_autospec(
            FilePersistenceService.get_file_content,
            return_value={
                "file_name": "memo.md",
                "mime_type": "text/markdown",
                "is_binary": False,
                "content_text": "# Thesis\nStrong buy",
            },
        )
        monkeypatch.setattr(
            FilePersistenceService,
            "get_file_content",
            get_file_content_mock,
        )
        monkeypatch.setattr(
            ws_files_crud,
            "get_vault_secrets_for_redaction",
            create_autospec(
                ws_files_crud.get_vault_secrets_for_redaction, return_value=[]
            ),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "http_file_text",
            create_autospec(
                ws_files_crud.http_file_text, return_value="# Thesis\nStrong buy"
            ),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "http_file_bytes",
            create_autospec(
                ws_files_crud.http_file_bytes, return_value=b"# Thesis\nStrong buy"
            ),
        )

        # 1. list_workspace_files (even with auto_start=True!)
        listing = await ws_files_crud.list_workspace_files(
            ws["workspace_id"],
            x_user_id="u-chao",
            path=".",
            include_system=False,
            pattern="**/*",
            wait_for_sandbox=False,
            auto_start=True,
        )
        assert listing["source"] == "database"
        assert listing["sandbox_ready"] is False
        assert listing["files"] == ["results/memo.md", "data/rev.csv"]

        # 2. resolve_workspace_file
        resolved = await ws_files_crud.resolve_workspace_file(
            ws["workspace_id"],
            x_user_id="u-chao",
            body=ResolveFileRefRequest(candidates=["memo.md"], recent_writes=[]),
        )
        assert resolved["source"] == "database"
        assert resolved["path"] == "results/memo.md"

        # 3. read_workspace_file
        read_res = await ws_files_crud.read_workspace_file(
            ws["workspace_id"],
            x_user_id="u-chao",
            path="results/memo.md",
            offset=0,
            limit=100,
            unlimited=False,
        )
        assert read_res["source"] == "database"
        assert "Strong buy" in read_res["content"]

        # 4. download_workspace_file
        dl_res = await ws_files_crud.download_workspace_file(
            ws["workspace_id"],
            x_user_id="u-chao",
            request=_make_request(),
            path="results/memo.md",
            attachment=True,
        )
        assert dl_res.status_code == 200
        assert b"Strong buy" in dl_res.body

        # 5. workspace_file_download_url forces fallback to /files/download (url: None)
        dl_url = await ws_files_crud.workspace_file_download_url(
            ws["workspace_id"],
            x_user_id="u-chao",
            path="results/memo.md",
        )
        assert dl_url == {"url": None}

        # 6. Hidden/system/always-hidden paths are rejected with 404 in read & download
        for blocked_path in (
            "_internal/secret.txt",
            ".agents/scratchpad/note.md",
            ".file_sync_marker",
        ):
            with pytest.raises(HTTPException) as exc_read_hidden:
                await ws_files_crud.read_workspace_file(
                    ws["workspace_id"],
                    x_user_id="u-chao",
                    path=blocked_path,
                    offset=0,
                    limit=100,
                    unlimited=False,
                )
            assert exc_read_hidden.value.status_code == 404

            with pytest.raises(HTTPException) as exc_dl_hidden:
                await ws_files_crud.download_workspace_file(
                    ws["workspace_id"],
                    x_user_id="u-chao",
                    request=_make_request(),
                    path=blocked_path,
                    attachment=True,
                )
            assert exc_dl_hidden.value.status_code == 404

        # 7. Binary file downloads are rejected with 403 for non-owner readers
        get_file_content_mock.return_value = {
            "file_name": "model.pdf",
            "mime_type": "application/pdf",
            "is_binary": True,
        }
        with pytest.raises(HTTPException) as exc_bin_dl:
            await ws_files_crud.download_workspace_file(
                ws["workspace_id"],
                x_user_id="u-chao",
                request=_make_request(),
                path="results/model.pdf",
                attachment=True,
            )
        assert exc_bin_dl.value.status_code == 403

        # Sandbox was never acquired across any of the file operations
        acquire_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_knowledge_verdicts_and_publications_allow_colleague(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")
        from src.dalton.database import (
            knowledge as db_knowledge,
            publications as pub_db,
            verdicts as verdicts_db,
        )

        get_ws_mock = create_autospec(ws_db.get_workspace, return_value=ws)
        monkeypatch.setattr(ws_db, "get_workspace", get_ws_mock)
        monkeypatch.setattr(
            db_knowledge,
            "list_versions",
            create_autospec(
                db_knowledge.list_versions,
                return_value=[{"version_no": 1, "doc_key": "thesis", "user_id": "u-richard"}],
            ),
        )
        monkeypatch.setattr(
            db_knowledge,
            "get_version",
            create_autospec(
                db_knowledge.get_version,
                return_value={"version_no": 1, "doc_key": "thesis", "user_id": "u-richard", "content": "ok"},
            ),
        )
        monkeypatch.setattr(
            knowledge_service,
            "diff_versions",
            create_autospec(
                knowledge_service.diff_versions,
                return_value=["@@ -1 +1 @@"],
            ),
        )
        monkeypatch.setattr(
            verdicts_db,
            "list_user_verdicts",
            create_autospec(
                verdicts_db.list_user_verdicts,
                return_value=[{"verdict_id": "v1", "status": "pass"}],
            ),
        )
        monkeypatch.setattr(
            pub_db,
            "list_publications",
            create_autospec(
                pub_db.list_publications,
                return_value=[{"publication_id": "p1", "title": "NVDA Q1"}],
            ),
        )
        get_pub_mock = create_autospec(
            pub_db.get_publication,
            return_value={
                "publication_id": "p1",
                "workspace_id": ws["workspace_id"],
                "user_id": "u-richard",
                "title": "NVDA Q1",
            },
        )
        monkeypatch.setattr(pub_db, "get_publication", get_pub_mock)

        kv = await dalton_router.list_knowledge_versions_endpoint(
            workspace_id=ws["workspace_id"],
            doc="thesis",
            user_id="u-chao",
            limit=20,
            before=None,
        )
        assert len(kv["items"]) == 1

        kv_one = await dalton_router.get_knowledge_version_endpoint(
            workspace_id=ws["workspace_id"],
            doc="thesis",
            n=1,
            user_id="u-chao",
        )
        assert kv_one["version_no"] == 1

        kv_diff = await dalton_router.diff_knowledge_versions_endpoint(
            workspace_id=ws["workspace_id"],
            doc="thesis",
            user_id="u-chao",
            from_version=1,
            to_version=2,
        )
        assert kv_diff["diffs"] == ["@@ -1 +1 @@"]

        vd = await dalton_router.list_verdicts_endpoint(
            user_id="u-chao",
            workspace_id=ws["workspace_id"],
            since=None,
            until=None,
            limit=20,
        )
        assert len(vd["items"]) == 1

        pubs = await dalton_router.list_publications_endpoint(
            user_id="u-chao",
            workspace_id=ws["workspace_id"],
        )
        assert len(pubs["items"]) == 1

        pub = await dalton_router.get_publication_endpoint("p1", user_id="u-chao")
        assert pub["publication_id"] == "p1"

        # Workspace-less publication is owner-only even when Dalton auth is enabled
        get_pub_mock.return_value = {
            "publication_id": "p-no-ws",
            "workspace_id": None,
            "user_id": "u-richard",
            "title": "Private Draft",
        }
        with pytest.raises(HTTPException) as exc_pub_no_ws:
            await dalton_router.get_publication_endpoint("p-no-ws", user_id="u-chao")
        assert exc_pub_no_ws.value.status_code == 403
        own_pub = await dalton_router.get_publication_endpoint(
            "p-no-ws", user_id="u-richard"
        )
        assert own_pub["publication_id"] == "p-no-ws"

        # Switch off -> knowledge endpoints use _verify_workspace_access and allow owner on deleted workspace
        _disable_dalton_auth(monkeypatch)
        deleted_ws = _sample_workspace(user_id="u-richard", status="deleted")
        get_ws_mock.return_value = deleted_ws
        owner_kv = await dalton_router.list_knowledge_versions_endpoint(
            workspace_id=deleted_ws["workspace_id"],
            doc="thesis",
            user_id="u-richard",
            limit=20,
            before=None,
        )
        assert len(owner_kv["items"]) == 1


class TestWriteEndpointsRejectColleagues:
    """Every write endpoint must continue to reject requests targeting a colleague's resource."""

    @pytest.mark.asyncio
    async def test_send_message_and_create_thread_rejected_on_colleague_resources(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")
        thread_id = "33333333-3333-4333-8333-333333333333"

        # 1. Creating a new thread / turn in a colleague's workspace -> 403 via requested_workspace
        get_ws_mock = create_autospec(ws_db.get_workspace, return_value=ws)
        monkeypatch.setattr(ws_db, "get_workspace", get_ws_mock)
        with pytest.raises(HTTPException) as exc_new_thread:
            await requested_workspace(
                user_id="u-chao", workspace_id=ws["workspace_id"]
            )
        assert exc_new_thread.value.status_code == 403
        assert exc_new_thread.value.detail == "该功能尚未开放"
        get_ws_mock.assert_called_once_with(ws["workspace_id"])

        # Route-level send_new_thread_message also rejects creating a thread in colleague's workspace
        auth_ctx = ChatAuthResult(
            user_id="u-chao", is_byok=False, burst_slot_id=None
        )
        auth_meta_mock = create_autospec(
            threads_messaging.get_thread_auth_meta, return_value=None
        )
        monkeypatch.setattr(
            threads_messaging, "get_thread_auth_meta", auth_meta_mock
        )
        monkeypatch.setattr(
            threads_messaging.setup, "agent_config", object(), raising=False
        )
        with pytest.raises(HTTPException) as exc_route_new:
            await threads_messaging.send_new_thread_message(
                turn=threads_messaging.AdmittedTurn(
                    thread_id=str(uuid4()),
                    body=ChatRequest(workspace_id=ws["workspace_id"], messages=[]),
                    auth=auth_ctx,
                ),
                raw_request=_make_request(),
            )
        assert exc_route_new.value.status_code == 403
        assert exc_route_new.value.detail == "该功能尚未开放"

        # 2. Route-level send_thread_message to an existing colleague thread -> 403
        auth_meta_mock.return_value = {
            "user_id": "u-richard",
            "is_shared": False,
            "msg_type": "ptc",
            "workspace_id": ws["workspace_id"],
            "llm_model": None,
        }
        with pytest.raises(HTTPException) as exc_send:
            await threads_messaging.send_thread_message(
                turn=threads_messaging.AdmittedTurn(
                    thread_id=thread_id,
                    body=ChatRequest(workspace_id=ws["workspace_id"], messages=[]),
                    auth=auth_ctx,
                ),
                raw_request=_make_request(),
            )
        assert exc_send.value.status_code == 403
        assert exc_send.value.detail == "该功能尚未开放"

    @pytest.mark.asyncio
    async def test_file_write_delete_upload_backup_rejected_on_colleague_workspace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard", status="running")
        monkeypatch.setattr(
            ws_files_service,
            "db_get_workspace",
            create_autospec(ws_files_service.db_get_workspace, return_value=ws),
        )

        # Write file -> 403
        with pytest.raises(HTTPException) as exc_write:
            await ws_files_crud.write_workspace_file(
                ws["workspace_id"],
                x_user_id="u-chao",
                path="results/memo.md",
                body=ws_files_crud.WriteFileRequest(content="hack"),
            )
        assert exc_write.value.status_code == 403

        # Delete files -> 403
        with pytest.raises(HTTPException) as exc_del:
            await ws_files_crud.delete_workspace_files(
                ws["workspace_id"],
                x_user_id="u-chao",
                body=ws_files_crud.DeleteFilesRequest(paths=["results/memo.md"]),
            )
        assert exc_del.value.status_code == 403

        # Upload file -> 403
        fake_upload = create_autospec(UploadFile, instance=True)
        with pytest.raises(HTTPException) as exc_up:
            await ws_files_crud.upload_workspace_file(
                ws["workspace_id"],
                x_user_id="u-chao",
                path="results/new.txt",
                file=fake_upload,
            )
        assert exc_up.value.status_code == 403

        # Backup files -> 403
        with pytest.raises(HTTPException) as exc_bak:
            await ws_files_crud.backup_workspace_files(
                ws["workspace_id"],
                x_user_id="u-chao",
            )
        assert exc_bak.value.status_code == 403

    @pytest.mark.asyncio
    async def test_workspace_settings_and_lifecycle_rejected_for_colleague(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard", status="stopped")
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

        # Update workspace settings -> 403
        with pytest.raises(HTTPException) as exc_upd:
            await workspaces_app.update_workspace(
                ws["workspace_id"],
                WorkspaceUpdate(name="Renamed"),
                x_user_id="u-chao",
            )
        assert exc_upd.value.status_code == 403

        # Delete workspace -> 403
        with pytest.raises(HTTPException) as exc_del:
            await workspaces_app.delete_workspace(
                ws["workspace_id"], x_user_id="u-chao"
            )
        assert exc_del.value.status_code == 403

        # Start / Stop / Archive workspace or computer -> 403
        with pytest.raises(HTTPException) as exc_start:
            await workspaces_app.start_workspace(
                ws["workspace_id"], x_user_id="u-chao", lazy=False
            )
        assert exc_start.value.status_code == 403

        from src.server.app import computers as computers_app

        monkeypatch.setattr(
            computers_app,
            "get_computer",
            create_autospec(
                computers_app.get_computer,
                return_value={
                    "computer_id": ws["computer_id"],
                    "user_id": "u-richard",
                    "status": "running",
                },
            ),
        )
        with pytest.raises(HTTPException) as exc_stop:
            await computers_app.stop_computer(
                ws["computer_id"], x_user_id="u-chao"
            )
        assert exc_stop.value.status_code == 403

        with pytest.raises(HTTPException) as exc_arch:
            await workspaces_app.archive_workspace(
                ws["workspace_id"], x_user_id="u-chao"
            )
        assert exc_arch.value.status_code == 403

    @pytest.mark.asyncio
    async def test_thread_mutations_rejected_for_colleague(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        thread_id = "33333333-3333-4333-8333-333333333333"
        monkeypatch.setattr(
            conv_db,
            "get_thread_owner_id",
            create_autospec(conv_db.get_thread_owner_id, return_value="u-richard"),
        )

        with pytest.raises(HTTPException) as exc_del:
            await threads_crud.delete_thread_endpoint(thread_id, x_user_id="u-chao")
        assert exc_del.value.status_code == 403

        with pytest.raises(HTTPException) as exc_upd:
            await threads_crud.update_thread_endpoint(
                thread_id,
                ThreadUpdateRequest(title="Hijack"),
                x_user_id="u-chao",
            )
        assert exc_upd.value.status_code == 403

    @pytest.mark.asyncio
    async def test_switch_off_owner_mirror_reads_always_hidden_path_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _disable_dalton_auth(monkeypatch)
        stopped_ws = _sample_workspace(user_id="u-richard", status="stopped")
        monkeypatch.setattr(
            ws_files_crud,
            "db_get_workspace",
            create_autospec(ws_files_crud.db_get_workspace, return_value=stopped_ws),
        )
        owned_mock = create_autospec(
            ws_files_service.owned_workspace_on_computer, return_value=stopped_ws
        )
        monkeypatch.setattr(
            ws_files_service,
            "owned_workspace_on_computer",
            owned_mock,
        )
        monkeypatch.setattr(
            ws_files_crud,
            "owned_workspace_on_computer",
            owned_mock,
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
                return_value=[{"path": ".env"}, {"path": ".mcp.json"}],
            ),
        )
        monkeypatch.setattr(
            FilePersistenceService,
            "get_file_content",
            create_autospec(
                FilePersistenceService.get_file_content,
                return_value={
                    "file_name": ".env",
                    "mime_type": "text/plain",
                    "is_binary": False,
                    "content_text": "FOO=bar",
                },
            ),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "get_vault_secrets_for_redaction",
            create_autospec(
                ws_files_crud.get_vault_secrets_for_redaction, return_value=[]
            ),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "http_file_text",
            create_autospec(ws_files_crud.http_file_text, return_value="FOO=bar"),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "http_file_bytes",
            create_autospec(ws_files_crud.http_file_bytes, return_value=b"FOO=bar"),
        )

        resolved = await ws_files_crud.resolve_workspace_file(
            stopped_ws["workspace_id"],
            x_user_id="u-richard",
            body=ResolveFileRefRequest(candidates=[".env"], recent_writes=[]),
        )
        assert resolved["source"] == "database"
        assert resolved["path"] == ".env"

        read_res = await ws_files_crud.read_workspace_file(
            stopped_ws["workspace_id"],
            x_user_id="u-richard",
            path=".env",
            offset=0,
            limit=100,
            unlimited=False,
        )
        assert read_res["source"] == "database"
        assert read_res["content"] == "FOO=bar"

        dl_res = await ws_files_crud.download_workspace_file(
            stopped_ws["workspace_id"],
            x_user_id="u-richard",
            request=_make_request(),
            path=".env",
            attachment=True,
        )
        assert dl_res.status_code == 200
        assert dl_res.body == b"FOO=bar"


class TestRangeDownloadPermissionMatrix:
    """P0-2 (B6): the HTTP Range download path (#613) under Dalton sharing.

    Every ranged read enters through the same gate as the whole-file read
    (``owned_workspace_on_computer(need="read")``); ro colleagues never
    receive a 206 from the mirror, warm, or live range helpers.
    """

    def _mirror_patches(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        row: dict[str, Any],
        file_bytes: bytes = b"%PDF-1.7",
    ) -> None:
        ws = _sample_workspace(user_id="u-richard", status="stopped")
        monkeypatch.setattr(
            ws_files_crud,
            "db_get_workspace",
            create_autospec(ws_files_crud.db_get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            ws_files_service,
            "db_get_workspace",
            create_autospec(ws_files_service.db_get_workspace, return_value=ws),
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
            "get_file_content",
            create_autospec(FilePersistenceService.get_file_content, return_value=row),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "get_vault_secrets_for_redaction",
            create_autospec(
                ws_files_crud.get_vault_secrets_for_redaction, return_value=[]
            ),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "http_file_bytes",
            create_autospec(ws_files_crud.http_file_bytes, return_value=file_bytes),
        )

    @pytest.mark.asyncio
    async def test_owner_range_download_of_mirror_binary_is_206(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Owner keeps upstream's ranged mirror read (no Dalton gate bites)."""
        _enable_dalton_auth(monkeypatch)
        pdf = b"%PDF-1.7" + bytes(range(256)) * 4
        row = {
            "file_name": "report.pdf",
            "mime_type": "application/pdf",
            "is_binary": True,
            "blob_sha256": "digest-of-report",
            "file_size": len(pdf),
        }
        self._mirror_patches(monkeypatch, row=row)
        monkeypatch.setattr(
            ws_files_crud,
            "http_file_byte_range",
            create_autospec(
                ws_files_crud.http_file_byte_range,
                side_effect=lambda _row, start, n, **_: (
                    pdf[start : start + n],
                    len(pdf),
                ),
            ),
        )
        response = await ws_files_crud.download_workspace_file(
            "11111111-1111-4111-8111-111111111111",
            x_user_id="u-richard",
            request=_make_request({"range": "bytes=0-99"}),
            path="report.pdf",
            attachment=False,
        )
        assert response.status_code == 206
        assert response.body == pdf[:100]

    @pytest.mark.asyncio
    async def test_ro_colleague_range_of_mirror_binary_is_403_not_206(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        row = {
            "file_name": "report.pdf",
            "mime_type": "application/pdf",
            "is_binary": True,
        }
        self._mirror_patches(monkeypatch, row=row)
        ranged_spy = create_autospec(ws_files_crud._stored_range_response)
        monkeypatch.setattr(ws_files_crud, "_stored_range_response", ranged_spy)
        with pytest.raises(HTTPException) as exc:
            await ws_files_crud.download_workspace_file(
                "11111111-1111-4111-8111-111111111111",
                x_user_id="u-chao",
                request=_make_request({"range": "bytes=0-99"}),
                path="report.pdf",
                attachment=False,
            )
        assert exc.value.status_code == 403
        ranged_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_ro_colleague_range_of_mirror_text_goes_whole_and_redacted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        row = {
            "file_name": "memo.md",
            "mime_type": "text/markdown",
            "is_binary": False,
        }
        self._mirror_patches(
            monkeypatch, row=row, file_bytes=b"# Thesis\nStrong buy"
        )
        ranged_spy = create_autospec(ws_files_crud._stored_range_response)
        monkeypatch.setattr(ws_files_crud, "_stored_range_response", ranged_spy)
        response = await ws_files_crud.download_workspace_file(
            "11111111-1111-4111-8111-111111111111",
            x_user_id="u-chao",
            request=_make_request({"range": "bytes=0-9"}),
            path="memo.md",
            attachment=False,
        )
        assert response.status_code == 200
        assert b"Strong buy" in response.body
        ranged_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_ro_colleague_range_of_hidden_path_is_404(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        self._mirror_patches(
            monkeypatch,
            row={"file_name": "note.md", "mime_type": "text/markdown"},
        )
        with pytest.raises(HTTPException) as exc:
            await ws_files_crud.download_workspace_file(
                "11111111-1111-4111-8111-111111111111",
                x_user_id="u-chao",
                request=_make_request({"range": "bytes=0-9"}),
                path=".agents/scratchpad/note.md",
                attachment=False,
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reader", ["u-outsider", ""])
    async def test_non_member_and_unauthenticated_range_rejected_at_gate(
        self, monkeypatch: pytest.MonkeyPatch, reader: str
    ) -> None:
        """Off-allowlist caller / no identity: the Range request dies in the
        same entry check as every other read, before any range logic."""
        _enable_dalton_auth(monkeypatch)
        self._mirror_patches(
            monkeypatch,
            row={"file_name": "report.pdf", "mime_type": "application/pdf"},
        )
        with pytest.raises(HTTPException) as exc:
            await ws_files_crud.download_workspace_file(
                "11111111-1111-4111-8111-111111111111",
                x_user_id=reader,
                request=_make_request({"range": "bytes=0-9"}),
                path="report.pdf",
                attachment=False,
            )
        assert exc.value.status_code in (401, 403, 404)

    @pytest.mark.asyncio
    async def test_ro_colleague_range_of_external_owned_workspace_is_404(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-outsider", status="stopped")
        monkeypatch.setattr(
            ws_files_crud,
            "db_get_workspace",
            create_autospec(ws_files_crud.db_get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            ws_files_service,
            "db_get_workspace",
            create_autospec(ws_files_service.db_get_workspace, return_value=ws),
        )
        with pytest.raises(HTTPException) as exc:
            await ws_files_crud.download_workspace_file(
                ws["workspace_id"],
                x_user_id="u-chao",
                request=_make_request({"range": "bytes=0-9"}),
                path="report.pdf",
                attachment=False,
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_ro_colleague_range_of_live_binary_never_reaches_range_helpers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running workspace: the warm and live range reads are both skipped
        for a colleague, and the whole-file rules reject the binary with 403."""
        _enable_dalton_auth(monkeypatch)
        ws = _sample_workspace(user_id="u-richard", status="running")
        monkeypatch.setattr(
            ws_files_crud,
            "db_get_workspace",
            create_autospec(ws_files_crud.db_get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            ws_files_service,
            "db_get_workspace",
            create_autospec(ws_files_service.db_get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "owner_work_dir",
            create_autospec(
                ws_files_crud.owner_work_dir,
                return_value="/home/workspace/NVDA_Deep_Dive",
            ),
        )
        warm_spy = create_autospec(ws_files_crud._warm_range_response)
        live_spy = create_autospec(ws_files_crud._live_range_response)
        monkeypatch.setattr(ws_files_crud, "_warm_range_response", warm_spy)
        monkeypatch.setattr(ws_files_crud, "_live_range_response", live_spy)

        sandbox = create_autospec(PTCSandbox, instance=True)
        sandbox.validate_path = lambda _p: True

        @asynccontextmanager
        async def _acquire(*_args):
            yield sandbox, ws

        monkeypatch.setattr(ws_files_crud, "acquire_sandbox_to_change", _acquire)

        read_mock = create_autospec(ws_files_crud.read_contained_sandbox_file)
        read_mock.return_value = (
            "/home/workspace/NVDA_Deep_Dive/report.pdf",
            b"%PDF-1.7 binary",
        )
        monkeypatch.setattr(
            ws_files_crud, "read_contained_sandbox_file", read_mock
        )
        monkeypatch.setattr(
            ws_files_crud,
            "contained_absolute_path",
            create_autospec(
                ws_files_crud.contained_absolute_path,
                return_value="/home/workspace/NVDA_Deep_Dive/report.pdf",
            ),
        )

        with pytest.raises(HTTPException) as exc:
            await ws_files_crud.download_workspace_file(
                ws["workspace_id"],
                x_user_id="u-chao",
                request=_make_request({"range": "bytes=0-99"}),
                path="report.pdf",
                attachment=False,
            )
        assert exc.value.status_code == 403
        warm_spy.assert_not_called()
        live_spy.assert_not_called()


WS_ID = "11111111-1111-4111-8111-111111111111"
XLSX_MIME = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


class TestRwColleagueFileDownloads:
    """rw colleagues (#152) download binaries like the owner on both serve
    paths, Range answers included; the owner's private paths and virtual
    files stay hidden, and ro colleagues keep every read-only limit."""

    def _mirror_patches(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        row: dict[str, Any],
        file_bytes: bytes = b"PK\x03\x04 sheet",
    ) -> None:
        ws = _sample_workspace(user_id="u-richard", status="stopped")
        monkeypatch.setattr(
            ws_files_crud,
            "db_get_workspace",
            create_autospec(ws_files_crud.db_get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            ws_files_service,
            "db_get_workspace",
            create_autospec(ws_files_service.db_get_workspace, return_value=ws),
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
            "get_file_content",
            create_autospec(FilePersistenceService.get_file_content, return_value=row),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "get_vault_secrets_for_redaction",
            create_autospec(
                ws_files_crud.get_vault_secrets_for_redaction, return_value=[]
            ),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "http_file_bytes",
            create_autospec(ws_files_crud.http_file_bytes, return_value=file_bytes),
        )

    @pytest.mark.asyncio
    async def test_rw_colleague_downloads_mirror_binary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported case: an rw colleague saving the owner's xlsx from a
        stopped workspace's DB mirror."""
        _enable_dalton_auth(monkeypatch)
        _set_web_sharing(monkeypatch, web_access="rw")
        # Not valid UTF-8, like a real xlsx's zip body, so the route serves it
        # verbatim rather than through the text redaction path.
        xlsx = b"PK\x03\x04\xff\xfe\x00\xb2sheet-bytes"
        self._mirror_patches(
            monkeypatch,
            row={
                "file_name": "Anthropic_sheet_updated_20261102.xlsx",
                "mime_type": XLSX_MIME,
                "is_binary": True,
            },
            file_bytes=xlsx,
        )
        response = await ws_files_crud.download_workspace_file(
            WS_ID,
            x_user_id="u-chao",
            request=_make_request(),
            path="ant_sheet_update/Anthropic_sheet_updated_20261102.xlsx",
            attachment=True,
        )
        assert response.status_code == 200
        assert response.body == xlsx
        # The mirror's bytes are the owner's stored object, fetched with the
        # owner's storage identity — the colleague never needs their own.
        ws_files_crud.http_file_bytes.assert_awaited_once()
        assert ws_files_crud.http_file_bytes.await_args.kwargs["user_id"] == "u-richard"

    @pytest.mark.asyncio
    async def test_rw_colleague_range_of_mirror_binary_is_206(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _set_web_sharing(monkeypatch, web_access="rw")
        pdf = b"%PDF-1.7" + bytes(range(256)) * 4
        self._mirror_patches(
            monkeypatch,
            row={
                "file_name": "report.pdf",
                "mime_type": "application/pdf",
                "is_binary": True,
                "blob_sha256": "digest-of-report",
                "file_size": len(pdf),
            },
            file_bytes=pdf,
        )
        monkeypatch.setattr(
            ws_files_crud,
            "http_file_byte_range",
            create_autospec(
                ws_files_crud.http_file_byte_range,
                side_effect=lambda _row, start, n, **_: (
                    pdf[start : start + n],
                    len(pdf),
                ),
            ),
        )
        response = await ws_files_crud.download_workspace_file(
            WS_ID,
            x_user_id="u-chao",
            request=_make_request({"range": "bytes=0-99"}),
            path="report.pdf",
            attachment=False,
        )
        assert response.status_code == 206
        assert response.body == pdf[:100]

    @pytest.mark.asyncio
    async def test_rw_colleague_virtual_and_blocked_paths_remain_404(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Write access lifts the binary limits only: the owner's private
        data (virtual DB files, system/hidden paths) stays invisible."""
        _enable_dalton_auth(monkeypatch)
        _set_web_sharing(monkeypatch, web_access="rw")
        self._mirror_patches(
            monkeypatch,
            row={
                "file_name": "portfolio.json",
                "mime_type": "application/json",
                "is_binary": False,
            },
        )
        for blocked_path in (
            ".agents/user/profile/portfolio.json",
            "_internal/secret.txt",
        ):
            with pytest.raises(HTTPException) as exc:
                await ws_files_crud.download_workspace_file(
                    WS_ID,
                    x_user_id="u-chao",
                    request=_make_request(),
                    path=blocked_path,
                    attachment=True,
                )
            assert exc.value.status_code == 404
        FilePersistenceService.get_file_content.assert_not_called()

    @pytest.mark.asyncio
    async def test_ro_colleague_mirror_binary_still_403(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _set_web_sharing(monkeypatch, web_access="ro")
        self._mirror_patches(
            monkeypatch,
            row={
                "file_name": "Anthropic_sheet_updated_20261102.xlsx",
                "mime_type": XLSX_MIME,
                "is_binary": True,
            },
        )
        with pytest.raises(HTTPException) as exc:
            await ws_files_crud.download_workspace_file(
                WS_ID,
                x_user_id="u-chao",
                request=_make_request(),
                path="ant_sheet_update/Anthropic_sheet_updated_20261102.xlsx",
                attachment=True,
            )
        assert exc.value.status_code == 403
        assert "read-only" in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_rw_colleague_download_url_still_falls_back_to_download(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The signed store link skips secret redaction, so it stays
        owner-only; an rw colleague gets ``url: null`` and downloads through
        /files/download, where the text bodies stay redacted."""
        _enable_dalton_auth(monkeypatch)
        _set_web_sharing(monkeypatch, web_access="rw")
        ws = _sample_workspace(user_id="u-richard", status="running")
        monkeypatch.setattr(
            ws_files_crud,
            "db_get_workspace",
            create_autospec(ws_files_crud.db_get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            ws_files_service,
            "db_get_workspace",
            create_autospec(ws_files_service.db_get_workspace, return_value=ws),
        )
        result = await ws_files_crud.workspace_file_download_url(
            WS_ID,
            x_user_id="u-chao",
            path="ant_sheet_update/Anthropic_sheet_updated_20261102.xlsx",
        )
        assert result == {"url": None}

    def _live_patches(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        status: str = "running",
        returns: bytes,
        name: str = "sheet.xlsx",
    ) -> dict[str, Any]:
        ws = _sample_workspace(user_id="u-richard", status=status)
        monkeypatch.setattr(
            ws_files_crud,
            "db_get_workspace",
            create_autospec(ws_files_crud.db_get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            ws_files_service,
            "db_get_workspace",
            create_autospec(ws_files_service.db_get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "owner_work_dir",
            create_autospec(
                ws_files_crud.owner_work_dir,
                return_value="/home/workspace/NVDA_Deep_Dive",
            ),
        )
        sandbox = create_autospec(PTCSandbox, instance=True)
        sandbox.validate_path = lambda _p: True

        @asynccontextmanager
        async def _acquire(*_args):
            yield sandbox, ws

        monkeypatch.setattr(ws_files_crud, "acquire_sandbox_to_change", _acquire)
        canonical = f"/home/workspace/NVDA_Deep_Dive/ant_sheet_update/{name}"
        monkeypatch.setattr(
            ws_files_crud,
            "contained_absolute_path",
            create_autospec(
                ws_files_crud.contained_absolute_path, return_value=canonical
            ),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "read_contained_sandbox_file",
            create_autospec(
                ws_files_crud.read_contained_sandbox_file,
                return_value=(canonical, returns),
            ),
        )
        return ws

    @pytest.mark.asyncio
    async def test_rw_colleague_downloads_live_binary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running workspace: the same download straight off the sandbox."""
        _enable_dalton_auth(monkeypatch)
        _set_web_sharing(monkeypatch, web_access="rw")
        # A real xlsx's zip body is not valid UTF-8; neither may this be, or
        # the route would (correctly) treat it as redactable text.
        xlsx = b"PK\x03\x04\xff\xfe\x00\xb2live-sheet"
        self._live_patches(monkeypatch, returns=xlsx)
        response = await ws_files_crud.download_workspace_file(
            WS_ID,
            x_user_id="u-chao",
            request=_make_request(),
            path="ant_sheet_update/sheet.xlsx",
            attachment=True,
        )
        assert response.status_code == 200
        assert response.body == xlsx

    @pytest.mark.asyncio
    async def test_rw_colleague_live_range_reaches_range_helpers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _set_web_sharing(monkeypatch, web_access="rw")
        self._live_patches(monkeypatch, returns=b"%PDF-1.7", name="report.pdf")
        monkeypatch.setattr(
            ws_files_crud,
            "_warm_range_response",
            create_autospec(
                ws_files_crud._warm_range_response, return_value=(False, None)
            ),
        )
        ranged = Response(
            content=b"%PDF",
            status_code=206,
            headers={"Content-Range": "bytes 0-3/2048"},
        )
        live_spy = create_autospec(ws_files_crud._live_range_response, return_value=ranged)
        monkeypatch.setattr(ws_files_crud, "_live_range_response", live_spy)
        response = await ws_files_crud.download_workspace_file(
            WS_ID,
            x_user_id="u-chao",
            request=_make_request({"range": "bytes=0-3"}),
            path="ant_sheet_update/report.pdf",
            attachment=False,
        )
        assert response.status_code == 206
        assert response.body == b"%PDF"
        live_spy.assert_awaited_once()


class TestSkillsListingAccess:
    """GET /api/v1/skills?workspace_id=… follows the sharing read gate; every
    skill write keeps the strict owner check."""

    def _listing_patches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name, returns in (
            ("get_disabled_builtin_skills", set()),
            ("get_skill_command_overrides", {}),
            ("list_workspace_skill_disables", set()),
            ("list_user_skills", []),
            ("list_enabled_user_skills", []),
            ("list_account_disables", SimpleNamespace(bundles=set())),
        ):
            monkeypatch.setattr(
                skills_app,
                name,
                create_autospec(getattr(skills_app, name), return_value=returns),
            )

        def _component_owners():
            return SimpleNamespace(skills={})

        monkeypatch.setattr(
            "src.server.services.plugins.bundled.component_owners",
            _component_owners,
        )

    @pytest.mark.asyncio
    async def test_workspace_view_allows_read_colleague(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        self._listing_patches(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")
        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(ws_db.get_workspace, return_value=ws),
        )
        result = await skills_app.get_skills(
            user_id="u-chao",
            include_disabled=False,
            workspace_id=ws["workspace_id"],
            all_scopes=False,
        )
        assert isinstance(result["skills"], list)

    @pytest.mark.asyncio
    async def test_workspace_view_hides_hidden_and_external_callers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        self._listing_patches(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")
        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(ws_db.get_workspace, return_value=ws),
        )
        _set_web_sharing(monkeypatch, web_access="hidden")
        with pytest.raises(HTTPException) as exc_hidden:
            await skills_app.get_skills(
                user_id="u-chao",
                include_disabled=False,
                workspace_id=ws["workspace_id"],
                all_scopes=False,
            )
        assert exc_hidden.value.status_code == 404

        _set_web_sharing(monkeypatch, web_access="rw")
        # An off-allowlist caller fails the team check itself (403), the same
        # answer require_workspace_reader gives them on any read route.
        with pytest.raises(HTTPException) as exc_outsider:
            await skills_app.get_skills(
                user_id="u-outsider",
                include_disabled=False,
                workspace_id=ws["workspace_id"],
                all_scopes=False,
            )
        assert exc_outsider.value.status_code == 403

    @pytest.mark.asyncio
    async def test_workspace_view_strict_owner_when_dalton_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _disable_dalton_auth(monkeypatch)
        self._listing_patches(monkeypatch)
        ws = _sample_workspace(user_id="u-richard")
        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(ws_db.get_workspace, return_value=ws),
        )
        with pytest.raises(HTTPException) as exc:
            await skills_app.get_skills(
                user_id="u-chao",
                include_disabled=False,
                workspace_id=ws["workspace_id"],
                all_scopes=False,
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_skills_write_ops_stay_owner_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        _set_web_sharing(monkeypatch, web_access="rw")
        ws = _sample_workspace(user_id="u-richard")
        monkeypatch.setattr(
            skills_app,
            "db_get_workspace",
            create_autospec(skills_app.db_get_workspace, return_value=ws),
        )

        with pytest.raises(HTTPException) as exc_del:
            await skills_app.delete_workspace_skill(
                ws["workspace_id"], "memo-skill", user_id="u-chao"
            )
        assert exc_del.value.status_code == 403

        fake_upload = create_autospec(UploadFile, instance=True)
        with pytest.raises(HTTPException) as exc_up:
            await skills_app.upload_workspace_skill(
                ws["workspace_id"], user_id="u-chao", file=fake_upload
            )
        assert exc_up.value.status_code == 403

        monkeypatch.setattr(
            skills_app,
            "delete_user_skill",
            create_autospec(
                skills_app.delete_user_skill, return_value={"archive_key": None}
            ),
        )
        monkeypatch.setattr(
            skills_app,
            "drop_archive_if_unused",
            create_autospec(skills_app.drop_archive_if_unused),
        )
        monkeypatch.setattr(
            skills_app.WorkspaceManager,
            "schedule_skill_reconcile",
            create_autospec(skills_app.WorkspaceManager.schedule_skill_reconcile),
        )
        deleted = await skills_app.delete_workspace_skill(
            ws["workspace_id"], "memo-skill", user_id="u-richard"
        )
        assert deleted.status_code == 204


class TestSkillContentAccess:
    """GET /api/v1/skills/{name}/content?workspace_id=… reads through the
    sharing gate like the listing; opening the page does not open the writes."""

    def _content_patches(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        skill_dir = tmp_path / "note-demo"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Note demo\n", encoding="utf-8")
        monkeypatch.setattr(
            skills_app,
            "get_user_skill",
            create_autospec(skills_app.get_user_skill, return_value=None),
        )
        monkeypatch.setattr(
            skills_app,
            "get_disabled_builtin_skills",
            create_autospec(skills_app.get_disabled_builtin_skills, return_value=set()),
        )
        monkeypatch.setattr(
            skills_app,
            "build_effective_skill_registry",
            create_autospec(
                skills_app.build_effective_skill_registry,
                return_value={"note-demo": SimpleNamespace(hidden=False)},
            ),
        )
        monkeypatch.setattr(
            skills_app,
            "configured_skill_dirs",
            create_autospec(skills_app.configured_skill_dirs, return_value=[tmp_path]),
        )
        monkeypatch.setattr(
            skills_app,
            "resolve_skill_dir",
            create_autospec(skills_app.resolve_skill_dir, return_value=skill_dir),
        )

    def _sample_ws(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        ws = _sample_workspace(user_id="u-richard")
        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(ws_db.get_workspace, return_value=ws),
        )
        return ws

    async def _read_content(self, ws: dict[str, Any], user_id: str):
        return await skills_app.get_skill_content(
            "note-demo",
            user_id=user_id,
            workspace_id=ws["workspace_id"],
            path="SKILL.md",
        )

    @pytest.mark.asyncio
    async def test_content_allows_read_colleague(self, monkeypatch, tmp_path) -> None:
        # The fixture's default sharing row is web_access="ro": read is enough.
        _enable_dalton_auth(monkeypatch)
        self._content_patches(monkeypatch, tmp_path)
        response = await self._read_content(self._sample_ws(monkeypatch), "u-chao")
        assert response["name"] == "note-demo"
        assert response["content"] == "# Note demo\n"

    @pytest.mark.asyncio
    async def test_content_hides_hidden_and_rejects_outsiders(
        self, monkeypatch, tmp_path
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        self._content_patches(monkeypatch, tmp_path)
        ws = self._sample_ws(monkeypatch)
        _set_web_sharing(monkeypatch, web_access="hidden")
        with pytest.raises(HTTPException) as exc_hidden:
            await self._read_content(ws, "u-chao")
        assert exc_hidden.value.status_code == 404

        _set_web_sharing(monkeypatch, web_access="rw")
        # An off-roster caller fails the team check itself (403), the same
        # answer the listing gives them.
        with pytest.raises(HTTPException) as exc_outsider:
            await self._read_content(ws, "u-outsider")
        assert exc_outsider.value.status_code == 403

    @pytest.mark.asyncio
    async def test_content_strict_owner_when_dalton_off(
        self, monkeypatch, tmp_path
    ) -> None:
        _disable_dalton_auth(monkeypatch)
        self._content_patches(monkeypatch, tmp_path)
        with pytest.raises(HTTPException) as exc:
            await self._read_content(self._sample_ws(monkeypatch), "u-chao")
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_content_read_does_not_open_writes(
        self, monkeypatch, tmp_path
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        self._content_patches(monkeypatch, tmp_path)
        ws = self._sample_ws(monkeypatch)
        response = await self._read_content(ws, "u-chao")
        assert response["name"] == "note-demo"

        monkeypatch.setattr(
            skills_app,
            "db_get_workspace",
            create_autospec(skills_app.db_get_workspace, return_value=ws),
        )
        with pytest.raises(HTTPException) as exc_del:
            await skills_app.delete_workspace_skill(
                ws["workspace_id"], "note-demo", user_id="u-chao"
            )
        assert exc_del.value.status_code == 403

        fake_upload = create_autospec(UploadFile, instance=True)
        with pytest.raises(HTTPException) as exc_up:
            await skills_app.upload_workspace_skill(
                ws["workspace_id"], user_id="u-chao", file=fake_upload
            )
        assert exc_up.value.status_code == 403


class TestRwColleagueLargeFileStreams:
    """Round 3, fix 1: a streamed body skips redaction, so a non-owner streams
    a file past the exec read limit only when its name and first bytes rule
    out text. The owner's stream is unchanged."""

    _WORK_DIR = "/home/workspace/NVDA_Deep_Dive"

    def _too_large_patches(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        name: str,
        head: bytes,
        size: int = 9_000_000,
    ) -> tuple[Any, Any]:
        ws = _sample_workspace(user_id="u-richard", status="running")
        monkeypatch.setattr(
            ws_files_crud,
            "db_get_workspace",
            create_autospec(ws_files_crud.db_get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            ws_files_service,
            "db_get_workspace",
            create_autospec(ws_files_service.db_get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "owner_work_dir",
            create_autospec(
                ws_files_crud.owner_work_dir, return_value=self._WORK_DIR
            ),
        )
        sandbox = create_autospec(PTCSandbox, instance=True)
        sandbox.validate_path = lambda _p: True

        @asynccontextmanager
        async def _acquire(*_args):
            yield sandbox, ws

        monkeypatch.setattr(ws_files_crud, "acquire_sandbox_to_change", _acquire)
        canonical = f"{self._WORK_DIR}/ant_sheet_update/{name}"
        monkeypatch.setattr(
            ws_files_crud,
            "contained_absolute_path",
            create_autospec(
                ws_files_crud.contained_absolute_path, return_value=canonical
            ),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "read_contained_sandbox_file",
            create_autospec(
                ws_files_crud.read_contained_sandbox_file,
                side_effect=FileTooLargeToServe(canonical, size),
            ),
        )
        sniff_mock = create_autospec(
            ws_files_crud.read_contained_sandbox_range,
            return_value=ContainedSlice(
                canonical=canonical,
                size=size,
                version="9000000 1 2 3",
                head=head,
                content=head,
            ),
        )
        monkeypatch.setattr(ws_files_crud, "read_contained_sandbox_range", sniff_mock)
        stream_spy = create_autospec(
            ws_files_crud._stream_large_file,
            return_value=Response(content=b"streamed-bytes", status_code=200),
        )
        monkeypatch.setattr(ws_files_crud, "_stream_large_file", stream_spy)
        return sniff_mock, stream_spy

    @pytest.mark.asyncio
    async def test_rw_colleague_large_text_mimed_stream_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A text-typed name refuses before any sniff: the streamed body could
        not be redacted."""
        _enable_dalton_auth(monkeypatch)
        _set_web_sharing(monkeypatch, web_access="rw")
        sniff_mock, stream_spy = self._too_large_patches(
            monkeypatch, name="transcript.md", head=b"# notes\n"
        )
        with pytest.raises(HTTPException) as exc:
            await ws_files_crud.download_workspace_file(
                WS_ID,
                x_user_id="u-chao",
                request=_make_request(),
                path="ant_sheet_update/transcript.md",
                attachment=True,
            )
        assert exc.value.status_code == 413
        assert "redaction" in str(exc.value.detail)
        sniff_mock.assert_not_called()
        stream_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_rw_colleague_large_text_sniffed_stream_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown extension refuses on the sniff: a UTF-8 head is text."""
        _enable_dalton_auth(monkeypatch)
        _set_web_sharing(monkeypatch, web_access="rw")
        sniff_mock, stream_spy = self._too_large_patches(
            monkeypatch, name="export.xyz", head="收益说明与持仓 notes".encode()
        )
        with pytest.raises(HTTPException) as exc:
            await ws_files_crud.download_workspace_file(
                WS_ID,
                x_user_id="u-chao",
                request=_make_request(),
                path="ant_sheet_update/export.xyz",
                attachment=True,
            )
        assert exc.value.status_code == 413
        sniff_mock.assert_awaited_once()
        stream_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_rw_colleague_large_text_renamed_xlsx_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A large UTF-8 text renamed to .xlsx is sniffed and refused: the name
        alone must not let an unredactable body stream."""
        _enable_dalton_auth(monkeypatch)
        _set_web_sharing(monkeypatch, web_access="rw")
        sniff_mock, stream_spy = self._too_large_patches(
            monkeypatch, name="export.xlsx", head=b"API_KEY=sk-live-secret\nrow,1,2\n"
        )
        with pytest.raises(HTTPException) as exc:
            await ws_files_crud.download_workspace_file(
                WS_ID,
                x_user_id="u-chao",
                request=_make_request(),
                path="ant_sheet_update/export.xlsx",
                attachment=True,
            )
        assert exc.value.status_code == 413
        sniff_mock.assert_awaited_once()
        stream_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_rw_colleague_large_binary_mimed_streams(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real binary under a binary name streams, after the head is sniffed:
        the name alone is never trusted."""
        _enable_dalton_auth(monkeypatch)
        _set_web_sharing(monkeypatch, web_access="rw")
        sniff_mock, stream_spy = self._too_large_patches(
            monkeypatch, name="sheet.xlsx", head=b"PK\x03\x04\xff\xfe\x00\xb2"
        )
        response = await ws_files_crud.download_workspace_file(
            WS_ID,
            x_user_id="u-chao",
            request=_make_request(),
            path="ant_sheet_update/sheet.xlsx",
            attachment=True,
        )
        assert response.status_code == 200
        assert response.body == b"streamed-bytes"
        sniff_mock.assert_awaited_once()
        stream_spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rw_colleague_large_binary_sniffed_streams(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown extension streams when the head rules out UTF-8."""
        _enable_dalton_auth(monkeypatch)
        _set_web_sharing(monkeypatch, web_access="rw")
        sniff_mock, stream_spy = self._too_large_patches(
            monkeypatch, name="export.xyz", head=b"\xff\xfe\x00\xfa\xfbbinary"
        )
        response = await ws_files_crud.download_workspace_file(
            WS_ID,
            x_user_id="u-chao",
            request=_make_request(),
            path="ant_sheet_update/export.xyz",
            attachment=True,
        )
        assert response.status_code == 200
        assert response.body == b"streamed-bytes"
        sniff_mock.assert_awaited_once()
        stream_spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_owner_large_text_still_streams(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        sniff_mock, stream_spy = self._too_large_patches(
            monkeypatch, name="transcript.md", head=b"# notes\n"
        )
        response = await ws_files_crud.download_workspace_file(
            WS_ID,
            x_user_id="u-richard",
            request=_make_request(),
            path="ant_sheet_update/transcript.md",
            attachment=True,
        )
        assert response.status_code == 200
        assert response.body == b"streamed-bytes"
        stream_spy.assert_awaited_once()
        sniff_mock.assert_not_called()


class TestRwColleagueRangeBlockedPaths:
    """Round 3, fix 2: both live Range paths run the whole-file canonical
    checks, so a symlink in the served tree that resolves into the owner's
    private dirs answers 404 to a colleague. The real ``_live_range_response``
    runs; only the sandbox read is replaced."""

    _WORK_DIR = "/home/workspace/NVDA_Deep_Dive"
    # Under .agents/ (a colleague-blocked system tree) but not itself an
    # always-hidden name and not text-typed, so the colleague's 404 below can
    # only come from the non-owner canonical check, and the owner's control
    # answer survives the helper's text- and validator-based "goes whole" outs.
    _BLOCKED_CANONICAL = "/home/workspace/NVDA_Deep_Dive/.agents/memory/notes.pdf"

    def _blocked_slice(self) -> ContainedSlice:
        return ContainedSlice(
            canonical=self._BLOCKED_CANONICAL,
            size=2048,
            version="2048 1 2 3",
            # Bytes UTF-8 has already ruled out (0xff), so the helper's own
            # "might be text" out cannot swallow the owner's control answer.
            head=b"%PDF-1.7\xff\xfe\x00binary-head",
            content=b"%PDF-1.7 sliced-bytes",
        )

    @pytest.mark.asyncio
    async def test_live_range_helper_404s_blocked_canonical_for_non_owner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sandbox = create_autospec(PTCSandbox, instance=True)
        monkeypatch.setattr(
            ws_files_crud,
            "read_contained_sandbox_range",
            create_autospec(
                ws_files_crud.read_contained_sandbox_range,
                return_value=self._blocked_slice(),
            ),
        )
        with pytest.raises(HTTPException) as exc:
            await ws_files_crud._live_range_response(
                sandbox,
                f"{self._WORK_DIR}/link.pdf",
                self._WORK_DIR,
                (0, 1023),
                "inline",
                blocked_for_non_owner=True,
            )
        assert exc.value.status_code == 404

        # The same resolved target answers the owner: a 206 with the slice.
        owner_answer = await ws_files_crud._live_range_response(
            sandbox,
            f"{self._WORK_DIR}/link.pdf",
            self._WORK_DIR,
            (0, 1023),
            "inline",
            blocked_for_non_owner=False,
        )
        assert owner_answer is not None
        assert owner_answer.status_code == 206
        assert owner_answer.body == b"%PDF-1.7 sliced-bytes"

    @pytest.mark.asyncio
    async def test_warm_range_path_serves_nothing_from_blocked_canonical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The warm path runs the same check through the real helper; its 404
        is swallowed by design (the route then answers from the acquisition,
        which raises it), so the warm answer here must be nothing at all."""
        ws = _sample_workspace(user_id="u-richard", status="running")
        sandbox = create_autospec(PTCSandbox, instance=True)
        sandbox.validate_path = lambda _p: True
        monkeypatch.setattr(
            ws_files_crud,
            "warm_sandbox",
            create_autospec(ws_files_crud.warm_sandbox, return_value=sandbox),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "read_contained_sandbox_range",
            create_autospec(
                ws_files_crud.read_contained_sandbox_range,
                return_value=self._blocked_slice(),
            ),
        )
        settled, answer = await ws_files_crud._warm_range_response(
            ws,
            WS_ID,
            f"{self._WORK_DIR}/link.pdf",
            self._WORK_DIR,
            (0, 1023),
            "inline",
            blocked_for_non_owner=True,
        )
        assert settled is False
        assert answer is None

    @pytest.mark.asyncio
    async def test_rw_colleague_range_of_symlink_into_blocked_dir_is_404(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Route level: an rw colleague's Range request for a symlink whose
        canonical target sits under .agents/ dies with 404 on the acquisition
        path, with the whole-file read never reached."""
        _enable_dalton_auth(monkeypatch)
        _set_web_sharing(monkeypatch, web_access="rw")
        ws = _sample_workspace(user_id="u-richard", status="running")
        monkeypatch.setattr(
            ws_files_crud,
            "db_get_workspace",
            create_autospec(ws_files_crud.db_get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            ws_files_service,
            "db_get_workspace",
            create_autospec(ws_files_service.db_get_workspace, return_value=ws),
        )
        monkeypatch.setattr(
            ws_files_crud,
            "owner_work_dir",
            create_autospec(
                ws_files_crud.owner_work_dir, return_value=self._WORK_DIR
            ),
        )
        # No warm session: the request goes straight through the acquisition.
        monkeypatch.setattr(
            ws_files_crud,
            "warm_sandbox",
            create_autospec(ws_files_crud.warm_sandbox, return_value=None),
        )
        sandbox = create_autospec(PTCSandbox, instance=True)
        sandbox.validate_path = lambda _p: True

        @asynccontextmanager
        async def _acquire(*_args):
            yield sandbox, ws

        monkeypatch.setattr(ws_files_crud, "acquire_sandbox_to_change", _acquire)
        monkeypatch.setattr(
            ws_files_crud,
            "contained_absolute_path",
            create_autospec(
                ws_files_crud.contained_absolute_path,
                return_value=f"{self._WORK_DIR}/link.pdf",
            ),
        )
        range_mock = create_autospec(
            ws_files_crud.read_contained_sandbox_range,
            return_value=self._blocked_slice(),
        )
        monkeypatch.setattr(ws_files_crud, "read_contained_sandbox_range", range_mock)
        whole_read_mock = create_autospec(ws_files_crud.read_contained_sandbox_file)
        monkeypatch.setattr(
            ws_files_crud, "read_contained_sandbox_file", whole_read_mock
        )
        with pytest.raises(HTTPException) as exc:
            await ws_files_crud.download_workspace_file(
                WS_ID,
                x_user_id="u-chao",
                request=_make_request({"range": "bytes=0-1023"}),
                path="link.pdf",
                attachment=False,
            )
        assert exc.value.status_code == 404
        range_mock.assert_awaited_once()
        whole_read_mock.assert_not_called()


class TestSkillsOwnerIdentityReads:
    """Round 3, fix 3: a workspace view queries the owner's tiers, with real
    row shapes — a colleague sees the owner's workspace skills, never their own
    same-name personal skill, and the detail page returns the owner's bytes."""

    _OWNER_WS_ROW: ClassVar[dict[str, Any]] = {
        "name": "note-demo",
        "description": "Owner's workspace copy",
        "command": None,
        "user_id": "u-richard",
        "workspace_id": WS_ID,
        "enabled": True,
        "confirmed": True,
        "plugin_id": None,
        "plugin_name": None,
        "plugin_enabled": None,
        "user_skill_id": 101,
        "archive_key": "archives/owner-ws-note-demo.zip",
        "content_hash": "hash-owner-ws",
    }
    _OWNER_PERSONAL_ROW: ClassVar[dict[str, Any]] = {
        "name": "owner-personal",
        "description": "Owner's user-tier skill",
        "command": None,
        "user_id": "u-richard",
        "workspace_id": None,
        "enabled": True,
        "confirmed": True,
        "plugin_id": None,
        "plugin_name": None,
        "plugin_enabled": None,
        "user_skill_id": 102,
        "archive_key": "archives/owner-personal.zip",
        "content_hash": "hash-owner-personal",
    }
    _CHAO_ROWS: ClassVar[dict[str, dict[str, Any]]] = {
        "note-demo": {
            "name": "note-demo",
            "description": "Chao's same-name personal skill",
            "command": None,
            "user_id": "u-chao",
            "workspace_id": None,
            "enabled": True,
            "confirmed": True,
            "plugin_id": None,
            "plugin_name": None,
            "plugin_enabled": None,
            "user_skill_id": 201,
            "archive_key": "archives/chao-note-demo.zip",
            "content_hash": "hash-chao-note-demo",
        },
        "chao-private": {
            "name": "chao-private",
            "description": "Chao's own tier, invisible in A's workspace",
            "command": None,
            "user_id": "u-chao",
            "workspace_id": None,
            "enabled": True,
            "confirmed": True,
            "plugin_id": None,
            "plugin_name": None,
            "plugin_enabled": None,
            "user_skill_id": 202,
            "archive_key": "archives/chao-private.zip",
            "content_hash": "hash-chao-private",
        },
    }

    def _owner_rows(
        self, user_id: str, workspace_id: str | None
    ) -> list[dict[str, Any]]:
        """Real-shaped branching: the owner's rows under the owner's id, the
        colleague's own rows under theirs — so a leak shows up in the result
        instead of an assertion inside the fake."""
        if workspace_id == WS_ID:
            return [dict(self._OWNER_WS_ROW)] if user_id == "u-richard" else []
        if user_id == "u-richard":
            return [dict(self._OWNER_PERSONAL_ROW)]
        return [dict(row) for row in self._CHAO_ROWS.values()]

    def _identity_listing_patches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> dict[str, Any]:
        mocks: dict[str, Any] = {}
        for fn_name in ("list_user_skills", "list_enabled_user_skills"):

            async def _list(
                user_id: str, workspace_id: str | None = None
            ) -> list[dict[str, Any]]:
                return self._owner_rows(user_id, workspace_id)

            mock = create_autospec(getattr(skills_app, fn_name), side_effect=_list)
            mocks[fn_name] = mock
            monkeypatch.setattr(skills_app, fn_name, mock)
        for name, returns in (
            ("get_disabled_builtin_skills", set()),
            ("get_skill_command_overrides", {}),
            ("list_workspace_skill_disables", set()),
            ("list_account_disables", SimpleNamespace(bundles=set())),
        ):
            monkeypatch.setattr(
                skills_app,
                name,
                create_autospec(getattr(skills_app, name), return_value=returns),
            )

        def _component_owners():
            return SimpleNamespace(skills={})

        monkeypatch.setattr(
            "src.server.services.plugins.bundled.component_owners",
            _component_owners,
        )
        return mocks

    def _sample_ws(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        ws = _sample_workspace(user_id="u-richard")
        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(ws_db.get_workspace, return_value=ws),
        )
        return ws

    @staticmethod
    def _zip_of(name: str, text: str) -> bytes:
        """A stored archive's shape: every file sits under one ``<name>/``
        folder, which is what ZipFolder lists."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr(f"{name}/SKILL.md", text)
        return buf.getvalue()

    @pytest.mark.asyncio
    async def test_colleague_sees_owner_skills_not_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        mocks = self._identity_listing_patches(monkeypatch)
        self._sample_ws(monkeypatch)
        result = await skills_app.get_skills(
            user_id="u-chao",
            include_disabled=False,
            workspace_id=WS_ID,
            all_scopes=False,
        )
        by_name = {s.name: s for s in result["skills"]}
        # The workspace row is the owner's, and it is what the view carries.
        assert by_name["note-demo"].origin == "workspace"
        assert by_name["note-demo"].description == "Owner's workspace copy"
        # The owner's user tier shows; the colleague's own tier does not.
        assert "owner-personal" in by_name
        assert "chao-private" not in by_name
        # Every tier query ran under the owner's id, never the caller's.
        for mock in (mocks["list_user_skills"], mocks["list_enabled_user_skills"]):
            assert mock.await_count > 0
            for call in mock.await_args_list:
                assert call.args[0] == "u-richard"

    @pytest.mark.asyncio
    async def test_colleague_reads_owner_workspace_skill_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_dalton_auth(monkeypatch)
        self._sample_ws(monkeypatch)
        owner_archive = self._zip_of("note-demo", "# Owner's workspace skill\n")
        chao_archive = self._zip_of("note-demo", "# Chao's personal copy\n")

        async def _get_user_skill(
            user_id: str, name: str, workspace_id: str | None = None
        ) -> dict[str, Any] | None:
            if user_id == "u-richard" and workspace_id == WS_ID:
                return dict(self._OWNER_WS_ROW)
            if user_id == "u-chao" and name in self._CHAO_ROWS:
                return dict(self._CHAO_ROWS[name])
            return None

        get_mock = create_autospec(
            skills_app.get_user_skill, side_effect=_get_user_skill
        )
        monkeypatch.setattr(skills_app, "get_user_skill", get_mock)

        async def _fetch(user_id: str, row: dict[str, Any]) -> bytes:
            return owner_archive if row["user_id"] == "u-richard" else chao_archive

        fetch_mock = create_autospec(
            skills_app.fetch_skill_archive, side_effect=_fetch
        )
        monkeypatch.setattr(skills_app, "fetch_skill_archive", fetch_mock)

        response = await skills_app.get_skill_content(
            "note-demo",
            user_id="u-chao",
            workspace_id=WS_ID,
            path="SKILL.md",
        )
        # The page carries the owner's workspace row, served from the owner's
        # archive — a leak would show Chao's personal copy instead.
        assert response["content"] == "# Owner's workspace skill\n"
        assert response["name"] == "note-demo"
        for call in get_mock.await_args_list:
            assert call.args[0] == "u-richard"
        fetch_mock.assert_awaited_once()
        assert fetch_mock.await_args.args[0] == "u-richard"
