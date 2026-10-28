"""Unit tests for colleague thread ownership, soft-delete retention, workspace shell fallback, and CoS tools."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.config import settings as app_settings
from src.dalton import sharing as dalton_sharing
from src.dalton.sharing import (
    COLLEAGUE_ACCESS_REVOKED_CODE,
    build_dispatched_by_meta,
    require_thread_access,
)
from src.server.app import workspaces as workspaces_app
from src.server.database import conversation as conv_db
from src.server.database import user as user_db
from src.server.database import workspace as ws_db
from src.server.database.conversation import threads_read as conv_threads_read
from src.server.services.report_back.handoff import executor as handoff_executor
from src.tools.secretary import _commands as sec_commands

USERS_BY_ID: dict[str, dict[str, Any]] = {
    "u-alice": {
        "user_id": "u-alice",
        "email": "alice@dalton.test",
        "name": "Alice Chen",
    },
    "u-bob": {
        "user_id": "u-bob",
        "email": "bob@dalton.test",
        "name": "Bob Wang",
    },
    "u-carol": {
        "user_id": "u-carol",
        "email": "carol@dalton.test",
        "name": "Carol Li",
    },
}

_ORIG_GET_USER = user_db.get_user
_ORIG_GET_USER_TIER = dalton_sharing.get_user_tier
_ORIG_CLASSIFY_OWNER_TIER = dalton_sharing.classify_owner_tier
_ORIG_IS_TEAM_MEMBER = dalton_sharing.is_team_member
_ORIG_GET_USER_COS_SHARING = dalton_sharing.get_user_cos_sharing
_ORIG_GET_WORKSPACE_SHARING = dalton_sharing.get_workspace_sharing


def _setup_internal_team(
    monkeypatch: pytest.MonkeyPatch,
    *,
    dispatch_enabled: bool = True,
    cos_sharing_by_owner: dict[str, bool] | None = None,
    web_sharing_by_ws: dict[str, str] | None = None,
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setattr(app_settings, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv(
        "DALTON_COLLEAGUE_DISPATCH_ENABLED", "true" if dispatch_enabled else "false"
    )

    async def _fake_get_user(uid: str) -> dict[str, Any] | None:
        return USERS_BY_ID.get(uid)

    monkeypatch.setattr(
        user_db, "get_user", create_autospec(_ORIG_GET_USER, side_effect=_fake_get_user)
    )
    async def _fake_get_user_tier(uid: str) -> str:
        return "internal"

    async def _fake_classify_owner_tier(owner_id: str | None) -> str:
        return "internal"

    async def _fake_is_team_member(email: str | None) -> bool:
        return str(email or "").lower() in {
            "alice@dalton.test",
            "bob@dalton.test",
            "carol@dalton.test",
        }

    monkeypatch.setattr(
        dalton_sharing,
        "get_user_tier",
        create_autospec(_ORIG_GET_USER_TIER, side_effect=_fake_get_user_tier),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "classify_owner_tier",
        create_autospec(
            _ORIG_CLASSIFY_OWNER_TIER, side_effect=_fake_classify_owner_tier
        ),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "is_team_member",
        create_autospec(_ORIG_IS_TEAM_MEMBER, side_effect=_fake_is_team_member),
    )

    cos_map = cos_sharing_by_owner or {}

    async def _fake_get_user_cos(
        owner_user_id: str, *, conn: Any | None = None
    ) -> dict[str, Any]:
        return {
            "user_id": owner_user_id,
            "cos_rw_enabled": cos_map.get(owner_user_id, True),
            "updated_at": None,
        }

    monkeypatch.setattr(
        dalton_sharing,
        "get_user_cos_sharing",
        create_autospec(_ORIG_GET_USER_COS_SHARING, side_effect=_fake_get_user_cos),
    )

    web_map = web_sharing_by_ws or {}

    async def _fake_get_ws_sharing(
        workspace_id: str, *, conn: Any | None = None
    ) -> dict[str, Any]:
        return {
            "workspace_id": workspace_id,
            "cos_access": "ro",
            "web_access": web_map.get(workspace_id, "ro"),
            "updated_by": None,
            "updated_at": None,
        }

    monkeypatch.setattr(
        dalton_sharing,
        "get_workspace_sharing",
        create_autospec(
            _ORIG_GET_WORKSPACE_SHARING, side_effect=_fake_get_ws_sharing
        ),
    )


@pytest.mark.asyncio
async def test_require_thread_access_three_actor_matrix_and_soft_delete_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify thread owner A, workspace owner B, and colleague C access matrix + soft-delete retention."""
    _setup_internal_team(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-bob": True},
        web_sharing_by_ws={"ws-bob": "ro"},
    )

    thread_id = str(uuid4())
    ws_state = {"status": "running"}

    async def _fake_get_thread(tid: str, *, conn: Any = None) -> dict[str, Any] | None:
        if tid != thread_id:
            return None
        return {
            "conversation_thread_id": thread_id,
            "workspace_id": "ws-bob",
            "user_id": "u-alice",
            "owner_id": "u-alice",
            "thread_user_id": "u-alice",
            "workspace_owner_id": "u-bob",
            "workspace_status": ws_state["status"],
            "title": "Alice Dispatched Thread",
            "metadata": {
                "dispatched_by": {
                    "user_id": "u-alice",
                    "name": "Alice Chen",
                    "channel": "cos",
                }
            },
        }

    async def _fake_get_ws(
        wid: str, *, include_deleted: bool = False
    ) -> dict[str, Any] | None:
        if wid != "ws-bob":
            return None
        if ws_state["status"] == "deleted" and not include_deleted:
            return None
        return {
            "workspace_id": "ws-bob",
            "user_id": "u-bob",
            "name": "Bob Workspace",
            "is_home": False,
            "status": ws_state["status"],
        }

    monkeypatch.setattr(
        conv_db,
        "get_thread_auth_meta",
        create_autospec(conv_db.get_thread_auth_meta, side_effect=_fake_get_thread),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, side_effect=_fake_get_ws),
    )

    # Actor A (thread owner): can read and write when Bob's cos_rw_enabled is True
    t_read_a = await require_thread_access(
        thread_id, "u-alice", need="read", channel="cos"
    )
    assert t_read_a == "u-alice"

    t_write_a = await require_thread_access(
        thread_id, "u-alice", need="write", channel="cos"
    )
    assert t_write_a == "u-alice"

    # Actor B (workspace owner): can read Alice's thread, but CANNOT write/mutate it
    t_read_b = await require_thread_access(
        thread_id, "u-bob", need="read", channel="web"
    )
    assert t_read_b == "u-alice"

    with pytest.raises(HTTPException) as exc_b_write:
        await require_thread_access(thread_id, "u-bob", need="write", channel="web")
    assert exc_b_write.value.status_code == 403
    assert "工作区所有者仅可查看" in str(exc_b_write.value.detail)

    # Actor C (other colleague): can read when web_access is 'ro', cannot write
    t_read_c = await require_thread_access(
        thread_id, "u-carol", need="read", channel="web"
    )
    assert t_read_c == "u-alice"

    with pytest.raises(HTTPException) as exc_c_write:
        await require_thread_access(thread_id, "u-carol", need="write", channel="web")
    assert exc_c_write.value.status_code == 403

    # Bob revokes cos_rw_enabled -> Alice can still read her thread, but new write fails with colleague_access_revoked
    _setup_internal_team(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-bob": False},
        web_sharing_by_ws={"ws-bob": "hidden"},
    )
    await require_thread_access(thread_id, "u-alice", need="read", channel="web")

    with pytest.raises(HTTPException) as exc_a_revoked:
        await require_thread_access(thread_id, "u-alice", need="write", channel="cos")
    assert exc_a_revoked.value.status_code == 403
    assert isinstance(exc_a_revoked.value.detail, dict)
    assert exc_a_revoked.value.detail["code"] == COLLEAGUE_ACCESS_REVOKED_CODE

    # Verified tail turn for Alice still passes even after revocation
    await require_thread_access(
        thread_id, "u-alice", need="write", channel="cos", is_tail_turn=True
    )

    # Bob soft-deletes workspace -> Alice STILL retains read-only access to her historical thread,
    # while write (even tail turn) is rejected because the workspace is deleted.
    ws_state["status"] = "deleted"
    t_after_delete = await require_thread_access(
        thread_id, "u-alice", need="read", channel="web"
    )
    assert t_after_delete == "u-alice"

    with pytest.raises(HTTPException) as exc_write_deleted:
        await require_thread_access(
            thread_id, "u-alice", need="write", channel="cos", is_tail_turn=True
        )
    assert exc_write_deleted.value.status_code == 403

    # Bob's OWN thread in Bob's deleted workspace is rejected with 404 (baseline behavior preserved)
    bob_own_thread_id = str(uuid4())
    with pytest.raises(HTTPException) as exc_bob_own_deleted:
        await require_thread_access(
            bob_own_thread_id,
            "u-bob",
            need="read",
            channel="web",
            owner_id="u-bob",
            workspace_owner_id="u-bob",
            workspace_id="ws-bob",
        )
    assert exc_bob_own_deleted.value.status_code == 404


@pytest.mark.asyncio
async def test_get_workspace_returns_thread_only_shell_when_reverted_to_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When workspace is reverted to hidden (or soft-deleted), a colleague with historical threads gets thread_only shell."""
    ws_id = str(uuid4())
    _setup_internal_team(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-bob": False},
        web_sharing_by_ws={ws_id: "hidden"},
    )

    now = datetime.now(UTC)
    ws_row = {
        "workspace_id": ws_id,
        "user_id": "u-bob",
        "name": "Bob Workspace",
        "description": "Secret description",
        "dir_name": "Bob_Workspace",
        "previous_dir_names": ["Old_Bob_Workspace"],
        "computer_root_dir": "/home/bob/langalpha",
        "previous_root_dirs": ["/Users/bob/langalpha"],
        "status": "running",
        "is_home": False,
        "created_at": now,
        "updated_at": now,
    }
    monkeypatch.setattr(
        workspaces_app,
        "db_get_workspace",
        create_autospec(workspaces_app.db_get_workspace, return_value=ws_row),
    )

    # Alice has threads in ws-bob -> gets minimal shell with effective_web_access="thread_only"
    has_threads_mock = create_autospec(
        conv_db.user_has_threads_in_workspace, return_value=True
    )
    monkeypatch.setattr(conv_db, "user_has_threads_in_workspace", has_threads_mock)
    shell = await workspaces_app.get_workspace(
        ws_row["workspace_id"], x_user_id="u-alice"
    )
    assert shell.effective_web_access == "thread_only"
    assert shell.name == "Bob Workspace"
    assert shell.description is None
    assert shell.dir_name is None
    assert shell.previous_dir_names == []
    assert shell.root_dir is None
    assert shell.previous_root_dirs == []

    # Carol has NO threads in ws-bob -> gets 404 (hidden workspace not found)
    has_threads_mock.return_value = False
    with pytest.raises(HTTPException) as exc_carol:
        await workspaces_app.get_workspace(ws_row["workspace_id"], x_user_id="u-carol")
    assert exc_carol.value.status_code in (403, 404)


@pytest.mark.asyncio
async def test_build_dispatched_by_meta_and_secretary_thread_owner_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_dispatched_by_meta populates dispatcher info; verify_thread_owner allows thread owner A to read."""
    _setup_internal_team(monkeypatch)

    meta = await build_dispatched_by_meta("u-alice", "u-bob", channel="cos")
    assert meta == {
        "user_id": "u-alice",
        "name": "Alice Chen",
        "email": "alice@dalton.test",
        "channel": "cos",
    }

    thread_id = str(uuid4())
    thread_row = {
        "conversation_thread_id": thread_id,
        "workspace_id": "ws-bob",
        "user_id": "u-alice",
        "owner_id": "u-alice",
        "thread_user_id": "u-alice",
        "workspace_owner_id": "u-bob",
    }
    monkeypatch.setattr(
        conv_threads_read,
        "get_thread_owner_id",
        create_autospec(conv_threads_read.get_thread_owner_id, return_value="u-alice"),
    )
    monkeypatch.setattr(
        conv_threads_read,
        "get_thread_auth_meta",
        create_autospec(conv_threads_read.get_thread_auth_meta, return_value=thread_row),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={
                "workspace_id": "ws-bob",
                "user_id": "u-bob",
                "name": "Bob Workspace",
                "is_home": False,
                "status": "running",
            },
        ),
    )

    # Alice (thread owner) can access and mutate her dispatched thread in Bob's workspace
    err_alice = await sec_commands.verify_thread_owner(
        thread_id, "u-alice", "tc-1", need="write", owner_only=True
    )
    assert err_alice is None

    # Bob (workspace owner, not thread owner) can read Alice's thread on CoS,
    # but is blocked from mutating Alice's thread when owner_only=True or need="write"
    err_bob_read = await sec_commands.verify_thread_owner(
        thread_id, "u-bob", "tc-2", need="read", owner_only=False
    )
    assert err_bob_read is None

    err_bob_mut = await sec_commands.verify_thread_owner(
        thread_id, "u-bob", "tc-3", need="write", owner_only=True
    )
    assert err_bob_mut is not None


@pytest.mark.asyncio
async def test_analyst_report_formats_workspace_owner_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """report_message includes workspace_owner attribute when reporting from a colleague's workspace."""
    monkeypatch.setattr(
        handoff_executor,
        "_workspace_name",
        create_autospec(
            handoff_executor._workspace_name,
            return_value="TSMC Deep Dive",
        ),
    )
    monkeypatch.setattr(
        handoff_executor,
        "_workspace_owner_name",
        create_autospec(
            handoff_executor._workspace_owner_name,
            return_value="Bob Wang",
        ),
    )
    monkeypatch.setattr(
        handoff_executor,
        "_reported_runs",
        create_autospec(handoff_executor._reported_runs, return_value=["run-1"]),
    )
    monkeypatch.setattr(
        handoff_executor,
        "_taken_hand_offs",
        create_autospec(handoff_executor._taken_hand_offs, return_value=[]),
    )
    owed = handoff_executor.OwedAnalystReport(
        origin={"ptc_workspace_id": "ws-bob", "user_id": "u-alice"},
        dispatcher_thread_id="th-cos",
        ptc_thread_id="th-123",
        final_status="completed",
        run_id="run-1",
    )
    msg, report = await handoff_executor.report_message(owed)
    assert report.workspace_name == "TSMC Deep Dive"
    assert report.workspace_owner == "Bob Wang"
    assert 'workspace_owner="Bob Wang"' in msg


@pytest.mark.asyncio
async def test_ensure_thread_writes_dispatched_by_with_workspace_owner_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ensure_thread writes metadata.dispatched_by when workspace_owner_id differs from user_id."""
    from src.server.handlers.chat import request_prep
    from src.server.models.chat import ChatRequest

    _setup_internal_team(monkeypatch)

    monkeypatch.setattr(
        request_prep,
        "_read_prior_thread",
        create_autospec(
            request_prep._read_prior_thread,
            return_value=request_prep.PriorThread(),
        ),
    )
    captured_kwargs: list[dict[str, Any]] = []

    async def _fake_ensure_thread_exists(**kwargs: Any) -> bool:
        captured_kwargs.append(kwargs)
        return False

    monkeypatch.setattr(
        request_prep.qr_db,
        "ensure_thread_exists",
        create_autospec(
            request_prep.qr_db.ensure_thread_exists,
            side_effect=_fake_ensure_thread_exists,
        ),
    )

    req = ChatRequest(messages=[{"role": "user", "content": "Analyze TSMC"}])
    await request_prep.ensure_thread(
        req,
        "th-colleague-1",
        "ws-bob",
        "u-alice",
        initial_query="Analyze TSMC",
        workspace_owner_id="u-bob",
        origin_channel="web",
    )

    assert len(captured_kwargs) == 1
    meta = captured_kwargs[0].get("metadata") or {}
    assert meta.get("dispatched_by") == {
        "user_id": "u-alice",
        "name": "Alice Chen",
        "email": "alice@dalton.test",
        "channel": "web",
    }


@pytest.mark.asyncio
async def test_soft_deleted_workspace_returns_thread_only_shell_and_lists_colleague_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When workspace is soft-deleted (db_get_workspace returns None unless include_deleted=True), colleague with threads gets shell and thread list."""
    from src.server.app.threads import crud as threads_crud

    ws_id = str(uuid4())
    thread_id = str(uuid4())
    _setup_internal_team(monkeypatch, dispatch_enabled=True)

    now = datetime.now(UTC)
    deleted_ws_row = {
        "workspace_id": ws_id,
        "user_id": "u-bob",
        "name": "Deleted Bob Workspace",
        "description": "Secret",
        "status": "deleted",
        "is_home": False,
        "created_at": now,
        "updated_at": now,
    }

    async def _fake_get_ws(
        wid: str, *, include_deleted: bool = False
    ) -> dict[str, Any] | None:
        if wid != ws_id:
            return None
        if not include_deleted:
            return None
        return deleted_ws_row

    monkeypatch.setattr(
        workspaces_app,
        "db_get_workspace",
        create_autospec(workspaces_app.db_get_workspace, side_effect=_fake_get_ws),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, side_effect=_fake_get_ws),
    )
    monkeypatch.setattr(
        conv_db,
        "user_has_threads_in_workspace",
        create_autospec(conv_db.user_has_threads_in_workspace, return_value=True),
    )

    # 1. GET /workspaces/{id} returns thread_only shell for soft-deleted workspace
    shell = await workspaces_app.get_workspace(ws_id, x_user_id="u-alice")
    assert shell.effective_web_access == "thread_only"
    assert shell.name == "Deleted Bob Workspace"
    assert shell.description is None

    # 2. GET /threads?workspace_id={id} returns Alice's historical threads with owner_user_id="u-alice"
    captured_ws_threads_kwargs: list[dict[str, Any]] = []

    async def _fake_get_ws_threads(**kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        captured_ws_threads_kwargs.append(kwargs)
        return (
            [
                {
                    "conversation_thread_id": thread_id,
                    "workspace_id": ws_id,
                    "user_id": "u-alice",
                    "thread_index": 1,
                    "current_status": "completed",
                    "msg_type": "ptc",
                    "title": "Historical Thread",
                    "first_query_content": "hello",
                    "platform": None,
                    "metadata": {},
                    "is_shared": False,
                    "is_pinned": False,
                    "archived_at": None,
                    "llm_model": "gpt-5",
                    "subagents_allowed": True,
                    "reasoning_effort": None,
                    "fast_mode": False,
                    "turn_count": 1,
                    "created_at": now,
                    "updated_at": now,
                }
            ],
            1,
        )

    monkeypatch.setattr(
        threads_crud,
        "get_workspace_threads",
        create_autospec(
            threads_crud.get_workspace_threads, side_effect=_fake_get_ws_threads
        ),
    )

    res = await threads_crud.list_threads(
        workspace_id=ws_id,
        limit=20,
        offset=0,
        sort_by="updated_at",
        sort_order="desc",
        platform_prefix=None,
        archived=False,
        x_user_id="u-alice",
    )
    assert res.total == 1
    assert res.threads[0].thread_id == thread_id
    assert captured_ws_threads_kwargs[0].get("owner_user_id") == "u-alice"


@pytest.mark.asyncio
async def test_get_threads_for_user_excludes_own_deleted_workspaces_and_keeps_colleague_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_threads_for_user filters out own deleted workspaces (w.user_id = %s AND w.status != 'deleted') while keeping colleague threads (t.user_id = %s AND w.user_id != %s)."""
    from contextlib import asynccontextmanager
    import psycopg

    executed_sql: list[tuple[str, Any]] = []
    cursor = create_autospec(psycopg.AsyncCursor, instance=True)

    async def _fake_execute(sql: str, params: Any = None) -> None:
        executed_sql.append((sql, params))

    async def _fake_fetchone() -> dict[str, Any]:
        return {"total": 0}

    async def _fake_fetchall() -> list[dict[str, Any]]:
        return []

    cursor.execute.side_effect = _fake_execute
    cursor.fetchone.side_effect = _fake_fetchone
    cursor.fetchall.side_effect = _fake_fetchall

    conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cursor

    conn.cursor.side_effect = _cursor_cm

    @asynccontextmanager
    async def _fake_get_db_connection(existing_conn: Any = None):
        yield existing_conn or conn

    monkeypatch.setattr(
        conv_threads_read.pool,
        "get_db_connection",
        create_autospec(
            conv_threads_read.pool.get_db_connection,
            side_effect=_fake_get_db_connection,
        ),
    )

    threads, total = await conv_threads_read.get_threads_for_user("u-alice")
    assert threads == []
    assert total == 0
    assert len(executed_sql) == 2
    for sql, params in executed_sql:
        assert "w.user_id = %s AND w.status != 'deleted'" in sql
        assert "t.user_id = %s AND w.user_id != %s" in sql
        assert list(params[:3]) == ["u-alice", "u-alice", "u-alice"]

