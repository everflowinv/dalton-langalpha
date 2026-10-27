"""Unit tests for Dalton team read-only sharing (src/dalton/sharing.py + hooked routes)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import create_autospec

import psycopg
import pytest
from fastapi import HTTPException, Request, UploadFile
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
from src.server.app import workspaces as workspaces_app
from src.server.app.threads import crud as threads_crud
from src.server.app.threads import messaging as threads_messaging
from src.server.app.threads import provenance as threads_provenance
from src.server.app.threads import tasks as threads_tasks
from src.server.app.workspace_files import crud as ws_files_crud
from src.server.app.workspace_files.file_refs import ResolveFileRefRequest
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


def _disable_dalton_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
    monkeypatch.setenv("DALTON_ALLOWED_EMAILS", TEAM_EMAILS)


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
            x_user_id="u-chao",
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
        assert exc_new_thread.value.detail == "该功能下一期开放"
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
                request=ChatRequest(workspace_id=ws["workspace_id"], messages=[]),
                auth=auth_ctx,
                raw_request=_make_request(),
            )
        assert exc_route_new.value.status_code == 403
        assert exc_route_new.value.detail == "该功能下一期开放"

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
                thread_id=thread_id,
                request=ChatRequest(workspace_id=ws["workspace_id"], messages=[]),
                auth=auth_ctx,
                raw_request=_make_request(),
            )
        assert exc_send.value.status_code == 403
        assert exc_send.value.detail == "该功能下一期开放"

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
