"""Unit tests for colleague workspace dispatch permissions, channel inheritance, tail turns, and error mapping."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

import aiohttp
import psycopg
import pytest
from fastapi import HTTPException
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore

from src.config import settings as app_settings
from src.dalton import sharing as dalton_sharing
from src.dalton.sharing import (
    COLLEAGUE_ACCESS_REVOKED_CODE,
    ColleagueAccessRevokedError,
    get_user_cos_sharing,
    require_workspace_access,
    resolve_turn_origin_channel,
    upsert_user_cos_sharing,
    verify_server_tail_turn,
)
from ptc_agent.agent.agent import PTCAgent
from ptc_agent.config.agent import AgentConfig, LLMConfig
from ptc_agent.config.core import (
    DaytonaConfig,
    FilesystemConfig,
    LoggingConfig,
    MCPConfig,
    SandboxConfig,
    SecurityConfig,
)
from ptc_agent.core.mcp_registry import MCPRegistry
from ptc_agent.core.paths import SandboxLayout, WorkspaceLayout
from ptc_agent.core.project_context import ProjectContext
from ptc_agent.core.sandbox import PTCSandbox
from src.server.app.threads import messaging as threads_messaging
from src.server.database import user as user_db
from src.server.database import workspace as ws_db
from src.server.handlers.chat import error_handling as chat_error_handling
from src.server.models.chat import ChatRequest
from src.server.services import turn_runtime
from src.server.services.report_back import notify_turn
from src.server.services.runs.admission import RunScope

USERS_BY_ID: dict[str, dict[str, Any]] = {
    "u-admin": {
        "user_id": "u-admin",
        "email": "chao.yu@dalton.test",
        "name": "Chao Yu",
    },
    "u-internal-a": {
        "user_id": "u-internal-a",
        "email": "richard.lu@dalton.test",
        "name": "Richard Lu",
    },
    "u-internal-b": {
        "user_id": "u-internal-b",
        "email": "rex.yuan@dalton.test",
        "name": "Rex Yuan",
    },
    "u-external": {
        "user_id": "u-external",
        "email": "motang@external.test",
        "name": "Motang",
    },
}


_ORIG_GET_USER = user_db.get_user
_ORIG_GET_USER_TIER = dalton_sharing.get_user_tier
_ORIG_CLASSIFY_OWNER_TIER = dalton_sharing.classify_owner_tier
_ORIG_IS_TEAM_MEMBER = dalton_sharing.is_team_member
_ORIG_GET_USER_COS_SHARING = dalton_sharing.get_user_cos_sharing
_ORIG_GET_WORKSPACE_SHARING = dalton_sharing.get_workspace_sharing


def _setup_roles(
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

    tier_by_uid = {
        "u-admin": "admin",
        "u-internal-a": "internal",
        "u-internal-b": "internal",
        "u-external": "external",
    }

    async def _fake_get_user_tier(uid: str) -> str:
        return tier_by_uid.get(str(uid), "external")

    async def _fake_classify_owner_tier(owner_id: str | None) -> str:
        return tier_by_uid.get(str(owner_id or ""), "external")

    async def _fake_is_team_member(email: str | None) -> bool:
        return str(email or "").lower() in {
            "chao.yu@dalton.test",
            "richard.lu@dalton.test",
            "rex.yuan@dalton.test",
            "motang@external.test",
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
        enabled = cos_map.get(owner_user_id, True)
        return {
            "user_id": owner_user_id,
            "cos_rw_enabled": enabled,
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
        lvl = web_map.get(workspace_id, "ro")
        return {
            "workspace_id": workspace_id,
            "cos_access": "ro",
            "web_access": lvl,
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
async def test_three_tier_role_matrix_across_channels_and_master_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify admin / internal / external matrix across cos & web channels and master switch."""
    ws_internal_b = {
        "workspace_id": "ws-b",
        "user_id": "u-internal-b",
        "name": "TSMC Workspace",
        "is_home": False,
        "status": "running",
    }
    ws_external = {
        "workspace_id": "ws-ext",
        "user_id": "u-external",
        "name": "External Workspace",
        "is_home": False,
        "status": "running",
    }

    # 1. Master switch disabled (default): write access to colleague workspace is blocked at entry point
    _setup_roles(
        monkeypatch,
        dispatch_enabled=False,
        cos_sharing_by_owner={"u-internal-b": True},
        web_sharing_by_ws={"ws-b": "rw"},
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, return_value=ws_internal_b),
    )
    with pytest.raises(HTTPException) as exc_off_cos:
        await turn_runtime.requested_workspace(
            "u-internal-a", "ws-b", channel="cos"
        )
    assert exc_off_cos.value.status_code == 403
    assert "尚未开放" in str(exc_off_cos.value.detail)

    with pytest.raises(HTTPException) as exc_off_web:
        await turn_runtime.requested_workspace(
            "u-internal-a", "ws-b", channel="web"
        )
    assert exc_off_web.value.status_code == 403
    assert "尚未开放" in str(exc_off_web.value.detail)

    # Read access still works when master switch is off
    res_read = await require_workspace_access(
        ws_internal_b, "u-internal-a", need="read", channel="cos"
    )
    assert res_read["workspace_id"] == "ws-b"

    # 2. Master switch enabled: internal colleague can read & write when owner enables sharing
    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": True},
        web_sharing_by_ws={"ws-b": "rw"},
    )
    await require_workspace_access(
        ws_internal_b, "u-internal-a", need="write", channel="cos"
    )
    await require_workspace_access(
        ws_internal_b, "u-internal-a", need="write", channel="web"
    )
    await require_workspace_access(
        ws_internal_b, "u-admin", need="write", channel="cos"
    )

    # 3. External member cannot read or write any colleague workspace (returns 404 to avoid leaking existence)
    for ch in ("cos", "web"):
        for need in ("read", "write"):
            with pytest.raises(HTTPException) as exc_ext_caller:
                await require_workspace_access(
                    ws_internal_b, "u-external", need=need, channel=ch  # type: ignore[arg-type]
                )
            assert exc_ext_caller.value.status_code == 404

    # 4. External member's workspace:
    #    - Default (web_access='hidden'): both internal colleague and admin get 404 on web & cos.
    #    - When external owner enables web_access='ro': internal colleague and admin can read on web,
    #      get 403 on web write, and still get 404 on cos.
    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": True},
        web_sharing_by_ws={"ws-b": "rw", "ws-ext": "hidden"},
    )
    for caller in ("u-internal-a", "u-admin"):
        for ch in ("cos", "web"):
            with pytest.raises(HTTPException) as exc_hidden_ext:
                await require_workspace_access(
                    ws_external, caller, need="read", channel=ch  # type: ignore[arg-type]
                )
            assert exc_hidden_ext.value.status_code == 404

    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": True},
        web_sharing_by_ws={"ws-b": "rw", "ws-ext": "ro"},
    )
    for caller in ("u-internal-a", "u-admin"):
        web_read = await require_workspace_access(
            ws_external, caller, need="read", channel="web"
        )
        assert web_read["workspace_id"] == "ws-ext"
        assert web_read["_caller_web_access"] == "ro"

        with pytest.raises(HTTPException) as exc_web_write_ext:
            await require_workspace_access(
                ws_external, caller, need="write", channel="web"
            )
        assert exc_web_write_ext.value.status_code == 403

        with pytest.raises(HTTPException) as exc_cos_ext:
            await require_workspace_access(
                ws_external, caller, need="read", channel="cos"
            )
        assert exc_cos_ext.value.status_code == 404


@pytest.mark.asyncio
async def test_user_cos_sharing_locks_workspaces_for_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """upsert_user_cos_sharing locks the user's workspaces FOR UPDATE and upserts inside an explicit transaction."""
    executed_queries: list[tuple[str, Any]] = []
    tx_entered: list[bool] = []

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)

    async def _fake_execute(sql: str, params: Any = None) -> None:
        executed_queries.append((sql, params))

    async def _fake_fetchone() -> dict[str, Any]:
        return {
            "user_id": "u-internal-b",
            "cos_rw_enabled": False,
            "updated_at": None,
        }

    cursor.execute.side_effect = _fake_execute
    cursor.fetchone.side_effect = _fake_fetchone

    conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cursor

    @asynccontextmanager
    async def _tx_cm(*args: Any, **kwargs: Any):
        tx_entered.append(True)
        yield

    conn.cursor.side_effect = _cursor_cm
    conn.transaction.side_effect = _tx_cm

    @asynccontextmanager
    async def _fake_get_db_connection(existing_conn: Any = None):
        yield existing_conn or conn

    monkeypatch.setattr(
        dalton_sharing,
        "get_db_connection",
        create_autospec(
            dalton_sharing.get_db_connection, side_effect=_fake_get_db_connection
        ),
    )

    saved = await upsert_user_cos_sharing(
        "u-internal-b", cos_rw_enabled=False
    )
    assert saved["cos_rw_enabled"] is False
    assert tx_entered == [True]
    assert len(executed_queries) == 2
    lock_sql, lock_params = executed_queries[0]
    upsert_sql, upsert_params = executed_queries[1]
    assert "FROM workspaces" in lock_sql
    assert "FOR UPDATE" in lock_sql
    assert lock_params == ("u-internal-b",)
    assert "INSERT INTO dalton_user_sharing" in upsert_sql
    assert upsert_params == ("u-internal-b", False)

    fetched = await get_user_cos_sharing("u-internal-b", conn=conn)
    assert fetched["cos_rw_enabled"] is False


@pytest.mark.asyncio
async def test_resolve_turn_origin_channel_inherits_from_parent_run() -> None:
    """Derived turns inherit origin_channel from their parent run rather than thread metadata."""
    thread_id = str(uuid4())
    parent_run_id = str(uuid4())
    wakeup_id = str(uuid4())

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    last_sql: list[str] = []

    async def _fake_execute(sql: str, params: Any = None) -> None:
        last_sql.append(sql)

    async def _fake_fetchone() -> dict[str, Any] | None:
        sql = last_sql[-1]
        if "FROM command_runs" in sql:
            return {"origin_channel": "cos"}
        if "FROM subagent_runs" in sql:
            return {"origin_channel": "cos"}
        if "FROM wakeups" in sql:
            return {"origin_channel": "cos"}
        if "FROM conversation_responses" in sql:
            return {"origin_channel": "cos"}
        return None

    cursor.execute.side_effect = _fake_execute
    cursor.fetchone.side_effect = _fake_fetchone

    conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cursor

    conn.cursor.side_effect = _cursor_cm

    # Thread itself was created on 'web', but parent run was triggered by 'cos'
    web_thread_meta = {"dispatched_by": {"user_id": "u-internal-a", "channel": "web"}}

    # 1. Background command notification inherits 'cos' from command_runs.spawned_by_run_id
    ch_cmd = await resolve_turn_origin_channel(
        thread_id,
        thread_metadata=web_thread_meta,
        report_source_kind="command_report",
        report_source_id="cmd-123",
        conn=conn,
    )
    assert ch_cmd == "cos"

    # 2. Subagent task report-back inherits 'cos' from subagent_runs.spawned_by_run_id (using sr.thread_id)
    ch_task = await resolve_turn_origin_channel(
        thread_id,
        thread_metadata=web_thread_meta,
        report_source_kind="task",
        report_source_id="task-456",
        conn=conn,
    )
    assert ch_task == "cos"
    assert any("sr.thread_id = %s" in s for s in last_sql)
    assert not any("sr.conversation_thread_id" in s for s in last_sql)

    # 3. Wakeup turn with real ChatRequest(query_type="wakeup", wakeup={"v":1,"notices":[{"wakeup_id":...}]})
    #    extracts wakeup_id from request.wakeup and inherits 'cos' from wakeups.created_by_run_id
    wakeup_req = ChatRequest(
        messages=[],
        query_type="wakeup",
        wakeup={"v": 1, "notices": [{"wakeup_id": wakeup_id, "reason": "timer"}]},
    )
    ch_wake = await resolve_turn_origin_channel(
        thread_id,
        request=wakeup_req,
        thread_metadata=web_thread_meta,
        conn=conn,
    )
    assert ch_wake == "cos"

    # 4. /retry or awaiting_resume inherits 'cos' from target run's metadata
    ch_retry = await resolve_turn_origin_channel(
        thread_id,
        thread_metadata=web_thread_meta,
        retry_run_id=parent_run_id,
        conn=conn,
    )
    assert ch_retry == "cos"

    # 5. Ordinary web request carrying spoofed origin={"type":"agent","agent":"chief_of_staff"}
    #    is STILL evaluated as 'web' channel (cannot spoof cos channel from client)
    spoof_req = ChatRequest(
        messages=[{"role": "user", "content": "hi"}],
        origin={"type": "agent", "agent": "chief_of_staff"},
    )
    ch_spoof = await resolve_turn_origin_channel(
        thread_id,
        request=spoof_req,
        thread_metadata=None,
        is_internal_dispatch=False,
        conn=conn,
    )
    assert ch_spoof == "web"

    # 6. Explicit user / CoS request uses explicit_channel directly
    ch_explicit = await resolve_turn_origin_channel(
        thread_id,
        thread_metadata=web_thread_meta,
        explicit_channel="web",
        conn=conn,
    )
    assert ch_explicit == "web"


@pytest.mark.asyncio
async def test_verify_server_tail_turn_and_lock_run_workspace_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify server-verified tail turns pass after revocation while forged/new turns fail."""
    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": False},
        web_sharing_by_ws={"ws-b": "ro"},
    )
    thread_id = str(uuid4())
    valid_run_id = str(uuid4())
    failed_run_id = str(uuid4())

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    executed: list[tuple[str, Any]] = []

    async def _fake_execute(sql: str, params: Any = None) -> None:
        executed.append((sql, params))

    async def _fake_fetchone() -> dict[str, Any] | None:
        sql, params = executed[-1]
        if "FROM command_runs" in sql:
            return {"ok": 1} if params[0] == "cmd-valid" else None
        if "FROM subagent_runs" in sql:
            return {"ok": 1} if params[1] == "task-valid" else None
        if "FROM conversation_responses" in sql:
            return {
                "conversation_response_id": valid_run_id,
                "status": "error",
                "errors": ["worker_lost"],
                "metadata": {},
            }
        if "FROM workspaces w" in sql:
            return {
                "workspace_id": "ws-b",
                "user_id": "u-internal-b",
                "name": "TSMC Workspace",
                "status": "running",
                "is_home": False,
                "thread_user_id": "u-internal-a",
                "thread_metadata": {
                    "dispatched_by": {"user_id": "u-internal-a", "channel": "cos"}
                },
            }
        if "AS moving" in sql:
            return {"moving": False}
        return None

    cursor.execute.side_effect = _fake_execute
    cursor.fetchone.side_effect = _fake_fetchone

    conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cursor

    conn.cursor.side_effect = _cursor_cm

    # 1. Server-verified tail turns: valid command_report, task_report_back, and worker_lost retry
    assert (
        await verify_server_tail_turn(
            thread_id,
            report_source_kind="command_report",
            report_source_id="cmd-valid",
            conn=conn,
        )
        is True
    )
    assert (
        await verify_server_tail_turn(
            thread_id,
            report_source_kind="task",
            report_source_id="task-valid",
            conn=conn,
        )
        is True
    )
    assert any("sr.thread_id = %s" in sql for sql, _ in executed)
    assert not any("sr.conversation_thread_id" in sql for sql, _ in executed)
    assert (
        await verify_server_tail_turn(
            thread_id,
            retry_run_id=valid_run_id,
            conn=conn,
        )
        is True
    )
    assert any(
        "SELECT conversation_response_id, status, errors, metadata" in sql
        for sql, _ in executed
    )

    # 2. Forged client tail turn IDs or non-resumable retry are rejected
    assert (
        await verify_server_tail_turn(
            thread_id,
            report_source_kind="command_report",
            report_source_id="cmd-forged",
            conn=conn,
        )
        is False
    )
    assert (
        await verify_server_tail_turn(
            thread_id,
            retry_run_id=failed_run_id,
            conn=conn,
        )
        is False
    )

    # 3. lock_run_workspace raises ColleagueAccessRevokedError when access was revoked
    #    and the turn is NOT a verified tail turn
    with pytest.raises(ColleagueAccessRevokedError) as exc_revoked:
        await ws_db.lock_run_workspace(
            conn,
            thread_id,
            turn_user_id="u-internal-a",
            origin_channel="cos",
            is_tail_turn=False,
        )
    assert exc_revoked.value.code == COLLEAGUE_ACCESS_REVOKED_CODE

    # 4. lock_run_workspace allows a verified tail turn even after access revocation
    await ws_db.lock_run_workspace(
        conn,
        thread_id,
        turn_user_id="u-internal-a",
        origin_channel="cos",
        is_tail_turn=True,
    )


def test_tail_turn_no_new_work_trims_spawning_tools_and_preserves_readers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tail_turn_no_new_work=True keeps TaskOutput/ThreadStatus and disables Task/RunWorkflow/schedule_wakeup/hand_off."""
    import ptc_agent.agent.agent as agent_mod
    from ptc_agent.agent.middleware.compaction import compact as compact_mod

    llm = GenericFakeChatModel(messages=iter([]))
    monkeypatch.setattr(
        AgentConfig,
        "get_llm_client",
        create_autospec(AgentConfig.get_llm_client, return_value=llm),
    )
    monkeypatch.setattr(
        compact_mod,
        "create_llm",
        create_autospec(compact_mod.create_llm, return_value=llm),
    )

    captured: list[dict[str, Any]] = []

    def _fake_create_agent(_model: Any, **built: Any) -> Any:
        captured.append(built)
        graph = create_autospec(CompiledStateGraph, instance=True)
        graph.with_config.return_value = graph
        return graph

    monkeypatch.setattr(
        agent_mod,
        "create_agent",
        create_autospec(agent_mod.create_agent, side_effect=_fake_create_agent),
    )

    cfg = AgentConfig(
        llm=LLMConfig(name="gpt-5"),
        security=SecurityConfig(),
        logging=LoggingConfig(),
        sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test")),
        mcp=MCPConfig(),
        filesystem=FilesystemConfig(enable_path_validation=False),
    )
    ptc = PTCAgent(cfg)
    sandbox = create_autospec(PTCSandbox(cfg.to_core_config()), instance=True)
    sandbox.config = cfg.to_core_config()
    sandbox.working_dir = "/home/daytona"
    sandbox.layout = SandboxLayout("/home/daytona")
    sandbox.workspace.return_value = WorkspaceLayout("/home/daytona", "ws-b")
    sandbox.skills_manifest = None
    sandbox.livefs = None
    sandbox.transcripts = None
    mcp_registry = create_autospec(MCPRegistry(cfg.to_core_config()), instance=True)
    mcp_registry.connectors = {}
    mcp_registry.get_all_tools.return_value = {}
    store = create_autospec(BaseStore, instance=True)
    project = ProjectContext(workspace_id="ws-b", dir_name="ws-b")

    ptc.create_agent(
        sandbox=sandbox,
        mcp_registry=mcp_registry,
        llm=llm,
        thread_id="th-12345678",
        user_id="u-internal-a",
        store=store,
        project=project,
        tail_turn_no_new_work=False,
    )
    normal_built = captured[-1]
    normal_tools = {t.name for t in normal_built["tools"]}
    normal_mw = {type(m).__name__ for m in normal_built["middleware"]}
    assert "schedule_wakeup" in normal_tools
    assert "RunWorkflow" in normal_tools
    assert "TaskOutput" in normal_tools
    assert "ThreadStatus" in normal_tools
    assert "SubAgentMiddleware" in normal_mw

    ptc.create_agent(
        sandbox=sandbox,
        mcp_registry=mcp_registry,
        llm=llm,
        thread_id="th-12345678",
        user_id="u-internal-a",
        store=store,
        project=project,
        tail_turn_no_new_work=True,
    )
    tail_built = captured[-1]
    tail_tools = {t.name for t in tail_built["tools"]}
    tail_mw = {type(m).__name__ for m in tail_built["middleware"]}
    assert "schedule_wakeup" not in tail_tools
    assert "RunWorkflow" not in tail_tools
    assert "SubAgentMiddleware" not in tail_mw
    assert "TaskOutput" in tail_tools
    assert "ThreadStatus" in tail_tools


@pytest.mark.asyncio
async def test_colleague_access_revoked_error_mapping_and_lasting_refusal() -> None:
    """ColleagueAccessRevokedError maps to 403 in _prime_dispatch, non-retryable SSE error, and lasting refusal."""
    err = ColleagueAccessRevokedError("工作区所有者已收回访问权限")
    assert err.code == COLLEAGUE_ACCESS_REVOKED_CODE

    # 1. _prime_dispatch raises HTTP 403 with code="colleague_access_revoked"
    async def _fail_gen():
        raise err
        yield ""  # pragma: no cover

    scope_prime = create_autospec(RunScope, instance=True)
    scope_prime.handed_off = False
    scope_prime.owned_run_handle = None
    request = ChatRequest(messages=[{"role": "user", "content": "hi"}])

    with pytest.raises(HTTPException) as http_exc:
        await threads_messaging._prime_dispatch(
            _fail_gen(),
            scope_prime,
            request,
            thread_id="th-1",
            run_id="run-1",
            user_id="u-internal-a",
            workspace_id="ws-b",
            model="gpt-5",
        )
    assert http_exc.value.status_code == 403
    assert isinstance(http_exc.value.detail, dict)
    assert http_exc.value.detail["code"] == COLLEAGUE_ACCESS_REVOKED_CODE

    # 2. handle_workflow_error yields terminal SSE error with code="colleague_access_revoked" and retryable=False
    scope = create_autospec(RunScope, instance=True)
    scope.handed_off = False
    scope.owned_run_handle = None

    frames: list[str] = []
    async for frame in chat_error_handling.handle_workflow_error(
        err,
        thread_id="th-1",
        user_id="u-internal-a",
        workspace_id="ws-b",
        handler=None,
        token_callback=None,
        scope=scope,
        start_time=0.0,
        request=request,
        is_byok=False,
    ):
        frames.append(frame)

    assert len(frames) == 1
    scope.release_slot.assert_awaited_once()
    assert frames[0].startswith("event: error\ndata: ")
    evt_data = json.loads(frames[0].split("data: ", 1)[1].strip())
    assert evt_data["code"] == COLLEAGUE_ACCESS_REVOKED_CODE
    assert evt_data["retryable"] is False
    assert evt_data["is_recoverable"] is False

    # 3. notify_turn._is_colleague_access_revoked identifies 403 colleague_access_revoked,
    #    while _lasting_refusal is False so wakeups terminate via end_wakeup instead of exhaust_alert
    resp = create_autospec(aiohttp.ClientResponse, instance=True)
    resp.status = 403
    resp.json.return_value = {
        "detail": {
            "code": COLLEAGUE_ACCESS_REVOKED_CODE,
            "message": "工作区所有者已收回访问权限",
        }
    }

    assert await notify_turn._is_colleague_access_revoked(resp) is True
    assert await notify_turn._lasting_refusal(resp) is False


@pytest.mark.asyncio
async def test_master_switch_disabled_preserves_legacy_cos_access_and_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When colleague_dispatch.enabled=false and B never set sharing, A's CoS write is rejected and list_colleague_cos_workspaces matches baseline."""
    _setup_roles(
        monkeypatch,
        dispatch_enabled=False,
        cos_sharing_by_owner={"u-internal-b": True},
        web_sharing_by_ws={"ws-b": "ro"},
    )
    ws_internal_b = {
        "workspace_id": "ws-b",
        "user_id": "u-internal-b",
        "name": "TSMC Workspace",
        "is_home": False,
        "status": "running",
    }

    # 1. With master switch disabled, B's default workspace sharing is cos_access='ro',
    #    so A's Chief of Staff write attempt is rejected with 403.
    with pytest.raises(HTTPException) as exc_cos_write:
        await require_workspace_access(
            ws_internal_b, "u-internal-a", need="write", channel="cos"
        )
    assert exc_cos_write.value.status_code == 403
    assert exc_cos_write.value.detail == "Forbidden"

    # 2. list_colleague_cos_workspaces queries dalton_workspace_sharing (not dalton_user_sharing)
    #    and returns cos_access='ro', read_only=True as in baseline.
    executed_sql: list[str] = []
    cursor = create_autospec(psycopg.AsyncCursor, instance=True)

    async def _fake_execute(sql: str, params: Any = None) -> None:
        executed_sql.append(sql)

    async def _fake_fetchall() -> list[dict[str, Any]]:
        return [
            {
                "workspace_id": "ws-b",
                "user_id": "u-internal-b",
                "name": "TSMC Workspace",
                "description": "Semis",
                "status": "running",
                "cos_access": "ro",
                "owner_name": "Rex Yuan",
                "owner_email": "rex.yuan@dalton.test",
            }
        ]

    cursor.execute.side_effect = _fake_execute
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
        dalton_sharing,
        "get_team_member_emails",
        create_autospec(
            dalton_sharing.get_team_member_emails,
            return_value=frozenset(
                {"chao.yu@dalton.test", "richard.lu@dalton.test", "rex.yuan@dalton.test"}
            ),
        ),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "_classify_owner_row_tier",
        create_autospec(
            dalton_sharing._classify_owner_row_tier,
            return_value="internal",
        ),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "get_db_connection",
        create_autospec(
            dalton_sharing.get_db_connection, side_effect=_fake_get_db_connection
        ),
    )

    items = await dalton_sharing.list_colleague_cos_workspaces("u-internal-a")
    assert len(items) == 1
    assert items[0]["workspace_id"] == "ws-b"
    assert items[0]["cos_access"] == "ro"
    assert items[0]["read_only"] is True
    assert any("dalton_workspace_sharing" in s for s in executed_sql)
    assert not any("dalton_user_sharing" in s for s in executed_sql)


@pytest.mark.asyncio
async def test_send_thread_message_rejects_mismatched_workspace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submitting a different workspace_id on an existing thread returns HTTP 400."""
    from fastapi import Request

    _setup_roles(monkeypatch, dispatch_enabled=True)
    thread_id = str(uuid4())
    ws_a = str(uuid4())
    ws_b = str(uuid4())

    monkeypatch.setattr(
        threads_messaging,
        "get_thread_auth_meta",
        create_autospec(
            threads_messaging.get_thread_auth_meta,
            return_value={
                "conversation_thread_id": thread_id,
                "workspace_id": ws_a,
                "user_id": "u-internal-a",
                "owner_id": "u-internal-a",
                "thread_user_id": "u-internal-a",
                "workspace_owner_id": "u-internal-a",
                "workspace_status": "running",
                "metadata": {},
            },
        ),
    )

    http_req = create_autospec(Request, instance=True)
    http_req.headers = {}
    auth = create_autospec(threads_messaging.ChatRateLimited, instance=True)
    auth.user_id = "u-internal-a"
    auth.is_byok = True
    auth.has_oauth = False
    auth.access_tier = 1
    auth.burst_slot_id = None
    chat_req = ChatRequest(
        messages=[{"role": "user", "content": "hi"}],
        workspace_id=ws_b,
    )

    with pytest.raises(HTTPException) as exc_mismatch:
        await threads_messaging._handle_send_message(
            chat_req,
            auth,
            thread_id,
            raw_request=http_req,
        )
    assert exc_mismatch.value.status_code == 400
    assert "workspace_id does not match" in str(exc_mismatch.value.detail)


@pytest.mark.asyncio
async def test_503_service_unavailable_is_not_misclassified_as_colleague_access_revoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When require_workspace_access raises HTTP 503, require_thread_access and lock_run_workspace re-raise 503 as-is."""
    from src.server.database import conversation as conv_db

    _setup_roles(monkeypatch, dispatch_enabled=True)
    thread_id = str(uuid4())

    async def _fail_503(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise HTTPException(status_code=503, detail="Team membership service unavailable")

    monkeypatch.setattr(
        dalton_sharing,
        "require_workspace_access",
        create_autospec(dalton_sharing.require_workspace_access, side_effect=_fail_503),
    )
    monkeypatch.setattr(
        conv_db,
        "get_thread_auth_meta",
        create_autospec(
            conv_db.get_thread_auth_meta,
            return_value={
                "conversation_thread_id": thread_id,
                "workspace_id": "ws-b",
                "user_id": "u-internal-a",
                "owner_id": "u-internal-a",
                "thread_user_id": "u-internal-a",
                "workspace_owner_id": "u-internal-b",
                "workspace_status": "running",
                "metadata": {},
            },
        ),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={
                "workspace_id": "ws-b",
                "user_id": "u-internal-b",
                "name": "TSMC Workspace",
                "is_home": False,
                "status": "running",
            },
        ),
    )
    monkeypatch.setattr(
        ws_db,
        "workspace_is_moving",
        create_autospec(ws_db.workspace_is_moving, return_value=False),
    )

    with pytest.raises(HTTPException) as exc_thread_503:
        await dalton_sharing.require_thread_access(
            thread_id, "u-internal-a", need="write", channel="cos"
        )
    assert exc_thread_503.value.status_code == 503
    assert exc_thread_503.value.detail == "Team membership service unavailable"

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)

    async def _fake_fetchone() -> dict[str, Any]:
        return {
            "workspace_id": "ws-b",
            "user_id": "u-internal-b",
            "name": "TSMC Workspace",
            "status": "running",
            "is_home": False,
            "thread_user_id": "u-internal-a",
            "thread_metadata": {},
        }

    cursor.fetchone.side_effect = _fake_fetchone
    conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cursor

    conn.cursor.side_effect = _cursor_cm

    with pytest.raises(HTTPException) as exc_lock_503:
        await ws_db.lock_run_workspace(
            conn,
            thread_id,
            turn_user_id="u-internal-a",
            origin_channel="cos",
            is_tail_turn=False,
        )
    assert exc_lock_503.value.status_code == 503


@pytest.mark.asyncio
async def test_start_task_run_skips_colleague_access_recheck_while_new_main_turn_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-admitted parent run can still spawn subagents via start_task_run after B revokes sharing, while a new main turn is rejected."""
    from src.server.database.runs import subagent_runs

    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": False},
        web_sharing_by_ws={"ws-b": "hidden"},
    )
    monkeypatch.setattr(
        ws_db,
        "workspace_is_moving",
        create_autospec(ws_db.workspace_is_moving, return_value=False),
    )

    thread_id = str(uuid4())
    parent_run_id = str(uuid4())
    task_run_id = str(uuid4())

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    executed_sql: list[str] = []

    async def _fake_execute(sql: str, params: Any = None) -> None:
        executed_sql.append(sql)

    async def _fake_fetchone() -> dict[str, Any] | None:
        sql = executed_sql[-1]
        if "FROM workspaces w" in sql:
            return {
                "workspace_id": "ws-b",
                "user_id": "u-internal-b",
                "name": "TSMC Workspace",
                "status": "running",
                "is_home": False,
                "thread_user_id": "u-internal-a",
                "thread_metadata": {
                    "dispatched_by": {"user_id": "u-internal-a", "channel": "web"}
                },
            }
        if "INSERT INTO subagent_runs" in sql:
            return {
                "task_run_id": task_run_id,
                "thread_id": thread_id,
                "task_id": "k1",
                "parent_run_id": parent_run_id,
                "status": "in_progress",
            }
        return None

    cursor.execute.side_effect = _fake_execute
    cursor.fetchone.side_effect = _fake_fetchone

    conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cursor

    @asynccontextmanager
    async def _tx_cm(*args: Any, **kwargs: Any):
        yield None

    conn.cursor.side_effect = _cursor_cm
    conn.transaction.side_effect = _tx_cm

    monkeypatch.setattr(
        subagent_runs,
        "find_run_by_launch_call",
        create_autospec(subagent_runs.find_run_by_launch_call, return_value=None),
    )

    # Subagent launch inside an already-admitted parent run succeeds even after B revoked both cos and web
    started = await subagent_runs.start_task_run(
        task_run_id=task_run_id,
        thread_id=thread_id,
        task_id="k1",
        cause="initial",
        parent_run_id=parent_run_id,
        launch_tool_call_id="tc-1",
        conn=conn,
    )
    assert started["task_run_id"] == task_run_id

    # A new main turn on the same thread is rejected with ColleagueAccessRevokedError
    with pytest.raises(ColleagueAccessRevokedError):
        await ws_db.lock_run_workspace(
            conn,
            thread_id,
            turn_user_id="u-internal-a",
            origin_channel="cos",
            is_tail_turn=False,
        )


@pytest.mark.asyncio
async def test_merged_wakeup_notices_filter_revoked_channel_and_keep_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a wakeup turn merges notices from cos and web and cos is revoked, only the web notice executes; if both are revoked, the turn is rejected."""
    from src.tools.wakeups.wording import render_wake_block

    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": False},
        web_sharing_by_ws={"ws-b": "rw"},
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={
                "workspace_id": "ws-b",
                "user_id": "u-internal-b",
                "name": "TSMC Workspace",
                "is_home": False,
                "status": "running",
            },
        ),
    )

    thread_id = str(uuid4())
    w_cos_id = str(uuid4())
    w_web_id = str(uuid4())

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    last_params: list[Any] = []

    async def _fake_execute(_sql: str, params: Any = None) -> None:
        last_params.append(params)

    async def _fake_fetchone() -> dict[str, Any] | None:
        wid = last_params[-1][0] if last_params and last_params[-1] else None
        if wid == w_cos_id:
            return {"origin_channel": "cos", "thread_channel": "web"}
        if wid == w_web_id:
            return {"origin_channel": "web", "thread_channel": "web"}
        return None

    cursor.execute.side_effect = _fake_execute
    cursor.fetchone.side_effect = _fake_fetchone
    conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cursor

    conn.cursor.side_effect = _cursor_cm

    wake_text = render_wake_block(
        ["cos timer fired", "web price trigger hit"],
        started_turn=True,
    )
    req = ChatRequest(
        messages=[{"role": "user", "content": wake_text}],
        query_type="wakeup",
        wakeup={
            "v": 1,
            "notices": [
                {"wakeup_id": w_cos_id, "reason": "cos timer"},
                {"wakeup_id": w_web_id, "reason": "web price"},
            ],
        },
    )

    from src.server.services.wakeups import cancel as wakeup_cancel_mod

    mock_end_wakeup = create_autospec(wakeup_cancel_mod.end_wakeup, return_value=None)
    monkeypatch.setattr(wakeup_cancel_mod, "end_wakeup", mock_end_wakeup)

    surviving_channel = await dalton_sharing.filter_wakeup_notices_by_access(
        thread_id,
        "u-internal-a",
        "ws-b",
        req,
        conn=conn,
    )
    assert surviving_channel == "web"
    assert req.wakeup["notices"] == [{"wakeup_id": w_web_id, "reason": "web price"}]
    assert "cos timer fired" not in req.messages[-1].content
    assert "web price trigger hit" in req.messages[-1].content
    mock_end_wakeup.assert_awaited_once_with(thread_id, w_cos_id, "disabled")

    # When web is also revoked ('ro'), all notices are revoked -> ColleagueAccessRevokedError
    mock_end_wakeup.reset_mock()
    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": False},
        web_sharing_by_ws={"ws-b": "ro"},
    )
    req_all_revoked = ChatRequest(
        messages=[{"role": "user", "content": wake_text}],
        query_type="wakeup",
        wakeup={
            "v": 1,
            "notices": [
                {"wakeup_id": w_cos_id, "reason": "cos timer"},
                {"wakeup_id": w_web_id, "reason": "web price"},
            ],
        },
    )
    with pytest.raises(ColleagueAccessRevokedError):
        await dalton_sharing.filter_wakeup_notices_by_access(
            thread_id,
            "u-internal-a",
            "ws-b",
            req_all_revoked,
            conn=conn,
        )
    assert mock_end_wakeup.await_count == 2


@pytest.mark.asyncio
async def test_revoked_cos_wakeup_terminated_in_delivery_and_excluded_from_subsequent_web_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When B disables CoS while keeping web='rw', A's CoS wakeup is terminated on 403 and never injected into A's subsequent web turn."""
    from datetime import UTC, datetime

    from src.server.database import conversation as conv_db
    from src.server.database.runs import lifecycle as tl_db
    from src.server.database.runs import outbox as outbox_db
    from src.server.database.wakeups import fire as fire_db
    from src.server.database.wakeups import read as read_db
    from src.server.database.wakeups import riders as riders_db
    from src.server.services import features
    from src.server.services.report_back import notify_turn
    from src.server.services.wakeups import cancel as wakeup_cancel_mod
    from src.server.services.wakeups import delivery, inline
    from src.server.services.wakeups import message as wakeup_msg_mod

    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": False},
        web_sharing_by_ws={"ws-b": "rw"},
    )

    thread_id = str(uuid4())
    w_cos_id = str(uuid4())
    w_web_id = str(uuid4())
    ended_wakeups: set[str] = set()

    async def _fake_end_wakeup(tid: str, wid: str, by: str) -> None:
        assert tid == thread_id
        assert by == "disabled"
        ended_wakeups.add(wid)

    mock_end_wakeup = create_autospec(
        wakeup_cancel_mod.end_wakeup,
        side_effect=_fake_end_wakeup,
    )
    monkeypatch.setattr(wakeup_cancel_mod, "end_wakeup", mock_end_wakeup)

    mock_exhaust_alert = create_autospec(outbox_db.exhaust_alert, return_value=True)
    monkeypatch.setattr(outbox_db, "exhaust_alert", mock_exhaust_alert)
    monkeypatch.setattr(
        outbox_db,
        "extend_job_lease",
        create_autospec(outbox_db.extend_job_lease, return_value=True),
    )
    monkeypatch.setattr(
        tl_db,
        "get_latest_attempt",
        create_autospec(tl_db.get_latest_attempt, return_value=None),
    )
    monkeypatch.setattr(
        delivery,
        "showing_run",
        create_autospec(delivery.showing_run, return_value=None),
    )
    monkeypatch.setattr(
        conv_db,
        "get_thread_by_id",
        create_autospec(
            conv_db.get_thread_by_id,
            return_value={
                "conversation_thread_id": thread_id,
                "workspace_id": "ws-b",
                "user_id": "u-internal-a",
                "archived_at": None,
                "metadata": {
                    "dispatched_by": {"user_id": "u-internal-a", "channel": "web"}
                },
            },
        ),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={
                "workspace_id": "ws-b",
                "user_id": "u-internal-b",
                "name": "TSMC Workspace",
                "is_home": False,
                "status": "running",
            },
        ),
    )
    monkeypatch.setattr(
        features,
        "user_feature_enabled",
        create_autospec(features.user_feature_enabled, return_value=True),
    )
    monkeypatch.setattr(
        notify_turn,
        "park_while_thread_busy",
        create_autospec(notify_turn.park_while_thread_busy, return_value=False),
    )
    monkeypatch.setattr(
        riders_db,
        "gather_wakeup_riders",
        create_autospec(riders_db.gather_wakeup_riders, return_value=[]),
    )
    monkeypatch.setattr(
        delivery,
        "render_wake",
        create_autospec(
            delivery.render_wake,
            return_value=("<wakeup>\ncos timer\n</wakeup>", [{"wakeup_id": w_cos_id}]),
        ),
    )
    monkeypatch.setattr(
        notify_turn,
        "post_notification_turn",
        create_autospec(
            notify_turn.post_notification_turn,
            return_value=("revoked", None),
        ),
    )

    cos_job = {
        "hook_outbox_id": str(uuid4()),
        "hook_type": "wakeup",
        "attempts": 1,
        "created_at": datetime.now(UTC),
        "payload": {
            "wakeup_id": w_cos_id,
            "notice": "fire",
            "seq": 1,
            "user_id": "u-internal-a",
            "thread_id": thread_id,
        },
    }
    web_job = {
        "hook_outbox_id": str(uuid4()),
        "hook_type": "wakeup",
        "attempts": 1,
        "created_at": datetime.now(UTC),
        "payload": {
            "wakeup_id": w_web_id,
            "notice": "fire",
            "seq": 1,
            "user_id": "u-internal-a",
            "thread_id": thread_id,
        },
    }

    async def _fake_job_owed(job_id: str) -> bool:
        if job_id == cos_job["hook_outbox_id"]:
            return w_cos_id not in ended_wakeups
        return w_web_id not in ended_wakeups

    monkeypatch.setattr(
        fire_db,
        "job_owed",
        create_autospec(fire_db.job_owed, side_effect=_fake_job_owed),
    )

    # 1. Delivery of the revoked CoS wakeup calls end_wakeup and returns 'release' without calling exhaust_alert
    wake = delivery._Wake(
        job=cos_job,
        thread_id=thread_id,
        wakeup_id=w_cos_id,
        subject=f"fire 1 of wakeup {w_cos_id}",
    )
    exit_outcome = await delivery._deliver(wake)
    assert exit_outcome == "release"
    assert w_cos_id in ended_wakeups
    mock_exhaust_alert.assert_not_awaited()

    # 2. When A sends a subsequent web message, inline wakeup_offer will not offer the revoked CoS wakeup
    offer_after_delivery = await inline.wakeup_offer(cos_job)
    assert isinstance(offer_after_delivery, notify_turn.NotOwed)

    # 3. Even if a CoS wakeup fired while A's web turn was already running (not yet ended by delivery),
    #    wakeup_offer checks origin_channel, terminates the CoS wakeup, and only offers the web wakeup.
    ended_wakeups.clear()

    async def _fake_resolve_channel(*args: Any, **kwargs: Any) -> str:
        return "cos" if kwargs.get("wakeup_id") == w_cos_id else "web"

    monkeypatch.setattr(
        dalton_sharing,
        "resolve_turn_origin_channel",
        create_autospec(
            dalton_sharing.resolve_turn_origin_channel,
            side_effect=_fake_resolve_channel,
        ),
    )
    monkeypatch.setattr(
        read_db,
        "get_wakeup",
        create_autospec(
            read_db.get_wakeup,
            side_effect=lambda wid: {
                "wakeup_id": wid,
                "conversation_thread_id": thread_id,
                "user_id": "u-internal-a",
                "kind": "at",
                "status": "fired",
            },
        ),
    )
    monkeypatch.setattr(
        wakeup_msg_mod,
        "render_notice",
        create_autospec(
            wakeup_msg_mod.render_notice,
            return_value=("web wakeup line", {"wakeup_id": w_web_id}),
        ),
    )

    cos_inline = await inline.wakeup_offer(cos_job)
    assert isinstance(cos_inline, notify_turn.NotOwed)
    assert w_cos_id in ended_wakeups

    web_inline = await inline.wakeup_offer(web_job)
    assert getattr(web_inline, "kind", None) == "wakeup"
    assert getattr(web_inline, "content", None) == "web wakeup line"
    assert w_web_id not in ended_wakeups


@pytest.mark.asyncio
async def test_merged_wakeups_rechecked_under_start_run_lock_after_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If B revokes CoS after HTTP entry filtering and before start_run acquires FOR SHARE OF w, lock_run_workspace terminates the CoS wakeup and rejects the turn."""
    from src.server.services.wakeups import cancel as wakeup_cancel_mod

    # Step 1: At HTTP entry, both CoS and web are open
    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": True},
        web_sharing_by_ws={"ws-b": "rw"},
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={
                "workspace_id": "ws-b",
                "user_id": "u-internal-b",
                "name": "TSMC Workspace",
                "is_home": False,
                "status": "running",
            },
        ),
    )
    monkeypatch.setattr(
        ws_db,
        "workspace_is_moving",
        create_autospec(ws_db.workspace_is_moving, return_value=False),
    )

    thread_id = str(uuid4())
    w_web_id = str(uuid4())
    w_cos_id = str(uuid4())

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    executed: list[tuple[str, Any]] = []

    async def _fake_execute(sql: str, params: Any = None) -> None:
        executed.append((sql, params))

    async def _fake_fetchone() -> dict[str, Any] | None:
        sql, params = executed[-1]
        if "FROM workspaces w" in sql:
            return {
                "workspace_id": "ws-b",
                "user_id": "u-internal-b",
                "name": "TSMC Workspace",
                "status": "running",
                "is_home": False,
                "thread_user_id": "u-internal-a",
                "thread_metadata": {
                    "dispatched_by": {"user_id": "u-internal-a", "channel": "web"}
                },
            }
        wid = params[0] if params else None
        if wid == w_web_id:
            return {"origin_channel": "web", "thread_channel": "web"}
        if wid == w_cos_id:
            return {"origin_channel": "cos", "thread_channel": "web"}
        return None

    cursor.execute.side_effect = _fake_execute
    cursor.fetchone.side_effect = _fake_fetchone
    conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cursor

    conn.cursor.side_effect = _cursor_cm

    mock_end_wakeup = create_autospec(wakeup_cancel_mod.end_wakeup, return_value=None)
    monkeypatch.setattr(wakeup_cancel_mod, "end_wakeup", mock_end_wakeup)

    req = ChatRequest(
        messages=[{"role": "user", "content": "<wakeup>\nweb\ncos\n</wakeup>"}],
        query_type="wakeup",
        wakeup={
            "v": 1,
            "notices": [
                {"wakeup_id": w_web_id, "reason": "web price"},
                {"wakeup_id": w_cos_id, "reason": "cos timer"},
            ],
        },
    )

    first_channel = await dalton_sharing.filter_wakeup_notices_by_access(
        thread_id,
        "u-internal-a",
        "ws-b",
        req,
        conn=conn,
    )
    assert first_channel == "web"
    assert len(req.wakeup["notices"]) == 2
    mock_end_wakeup.assert_not_awaited()

    # Step 2: Before start_run (lock_run_workspace) acquires FOR SHARE OF w, B revokes CoS
    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": False},
        web_sharing_by_ws={"ws-b": "rw"},
    )
    notice_ids = dalton_sharing.extract_wakeup_notice_ids(req)
    assert notice_ids == [w_web_id, w_cos_id]

    # Even though first notice's channel is 'web' (still open), lock_run_workspace checks every notice,
    # terminates the revoked CoS wakeup w_cos_id, and raises ColleagueAccessRevokedError.
    with pytest.raises(ColleagueAccessRevokedError):
        await ws_db.lock_run_workspace(
            conn,
            thread_id,
            turn_user_id="u-internal-a",
            origin_channel=first_channel,
            is_tail_turn=False,
            wakeup_notice_ids=notice_ids,
        )
    mock_end_wakeup.assert_awaited_once_with(thread_id, w_cos_id, "disabled")


@pytest.mark.asyncio
async def test_subagent_report_back_keeps_full_tools_when_unrevoked_and_trims_after_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When sharing is open, a subagent completion report-back turn is admitted with is_tail_turn=False (full tools); after revocation, it is admitted with is_tail_turn=True (spawning tools trimmed)."""
    from src.server.services import turn_runtime

    thread_id = str(uuid4())
    thread_meta = {
        "dispatched_by": {"user_id": "u-internal-a", "channel": "cos"},
    }
    report_req = ChatRequest(
        messages=[{"role": "user", "content": "Subagent k1 completed"}],
        query_type="system",
        report_source={"kind": "task", "task_id": "task-valid", "display_id": "k1"},
    )

    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={
                "workspace_id": "ws-b",
                "user_id": "u-internal-b",
                "name": "TSMC Workspace",
                "is_home": False,
                "status": "running",
            },
        ),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "resolve_turn_origin_channel",
        create_autospec(
            dalton_sharing.resolve_turn_origin_channel,
            return_value="cos",
        ),
    )
    mock_verify_tail = create_autospec(
        dalton_sharing.verify_server_tail_turn,
        return_value=True,
    )
    monkeypatch.setattr(
        dalton_sharing,
        "verify_server_tail_turn",
        mock_verify_tail,
    )

    # 1. While B's CoS sharing is open (cos_rw_enabled=True), normal write access succeeds -> is_tail_turn is False (full tools)
    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": True},
        web_sharing_by_ws={"ws-b": "ro"},
    )
    ws_open, ch_open, is_tail_open = await dalton_sharing.resolve_colleague_turn_admission(
        user_id="u-internal-a",
        workspace_id="ws-b",
        thread_id=thread_id,
        request=report_req,
        thread_meta=thread_meta,
        is_existing_colleague_thread=True,
        is_internal=True,
        is_dispatch=True,
        requested_workspace_fn=turn_runtime.requested_workspace,
    )
    assert ws_open is not None
    assert ch_open == "cos"
    assert is_tail_open is False
    mock_verify_tail.assert_not_awaited()

    # 2. After B revokes CoS sharing (cos_rw_enabled=False), normal write fails with ColleagueAccessRevokedError ->
    #    verify_server_tail_turn runs and admits the turn with is_tail_turn=True (tail_turn_no_new_work=True)
    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": False},
        web_sharing_by_ws={"ws-b": "ro"},
    )
    ws_revoked, ch_revoked, is_tail_revoked = (
        await dalton_sharing.resolve_colleague_turn_admission(
            user_id="u-internal-a",
            workspace_id="ws-b",
            thread_id=thread_id,
            request=report_req,
            thread_meta=thread_meta,
            is_existing_colleague_thread=True,
            is_internal=True,
            is_dispatch=True,
            requested_workspace_fn=turn_runtime.requested_workspace,
        )
    )
    assert ws_revoked is not None
    assert ch_revoked == "cos"
    assert is_tail_revoked is True
    mock_verify_tail.assert_awaited_once()


@pytest.mark.asyncio
async def test_inline_wakeup_offer_defers_on_permission_query_failure_without_terminating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When get_thread_by_id fails during wakeup_offer, the notice is not handed to the model (_decide returns None) and end_wakeup is not called."""
    from datetime import UTC, datetime

    from src.server.database import conversation as conv_db
    from src.server.database.wakeups import fire as fire_db
    from src.server.database.wakeups import read as read_db
    from src.server.services import features
    from src.server.services.report_back import steer as steer_mod
    from src.server.services.wakeups import cancel as wakeup_cancel_mod
    from src.server.services.wakeups import delivery, inline
    from src.server.services.wakeups import message as wakeup_msg_mod

    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": False},
        web_sharing_by_ws={"ws-b": "ro"},
    )

    thread_id = str(uuid4())
    wakeup_id = str(uuid4())

    mock_end_wakeup = create_autospec(wakeup_cancel_mod.end_wakeup, return_value=None)
    monkeypatch.setattr(wakeup_cancel_mod, "end_wakeup", mock_end_wakeup)
    monkeypatch.setattr(
        fire_db,
        "job_owed",
        create_autospec(fire_db.job_owed, return_value=True),
    )
    monkeypatch.setattr(
        read_db,
        "get_wakeup",
        create_autospec(
            read_db.get_wakeup,
            return_value={
                "wakeup_id": wakeup_id,
                "conversation_thread_id": thread_id,
                "user_id": "u-internal-a",
                "kind": "at",
                "status": "fired",
            },
        ),
    )
    monkeypatch.setattr(
        features,
        "user_feature_enabled",
        create_autospec(features.user_feature_enabled, return_value=True),
    )
    monkeypatch.setattr(
        delivery,
        "showing_run",
        create_autospec(delivery.showing_run, return_value=None),
    )
    mock_render = create_autospec(
        wakeup_msg_mod.render_notice,
        return_value=("should not be offered", {"wakeup_id": wakeup_id}),
    )
    monkeypatch.setattr(wakeup_msg_mod, "render_notice", mock_render)

    # Simulate DB connection / timeout error during get_thread_by_id
    monkeypatch.setattr(
        conv_db,
        "get_thread_by_id",
        create_autospec(
            conv_db.get_thread_by_id,
            side_effect=psycopg.OperationalError("connection lost during query"),
        ),
    )

    job = {
        "hook_outbox_id": str(uuid4()),
        "hook_type": "wakeup",
        "attempts": 1,
        "created_at": datetime.now(UTC),
        "payload": {
            "wakeup_id": wakeup_id,
            "notice": "fire",
            "seq": 1,
            "user_id": "u-internal-a",
            "thread_id": thread_id,
        },
    }

    # 1. wakeup_offer propagates the DB error rather than treating it as unrevoked
    with pytest.raises(psycopg.OperationalError):
        await inline.wakeup_offer(job)

    # 2. steer._decide catches the error and returns None (deferring the notice, not handing to model and not closing as NotOwed)
    decision = await steer_mod._decide(job, thread_id)
    assert decision is None
    mock_end_wakeup.assert_not_awaited()
    mock_render.assert_not_awaited()


@pytest.mark.asyncio
async def test_revocation_between_entry_and_start_lock_rechecks_tail_turn_and_trims_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When B revokes access after HTTP entry check and before START lock_run_workspace, a subagent completion report-back is admitted as a tail turn with tools trimmed, while an ordinary new message is rejected."""
    import ptc_agent.agent.agent as agent_mod
    from ptc_agent.agent.middleware.compaction import compact as compact_mod
    from src.server.services import turn_runtime

    thread_id = str(uuid4())
    thread_meta = {
        "dispatched_by": {"user_id": "u-internal-a", "channel": "cos"},
    }
    report_req = ChatRequest(
        messages=[{"role": "user", "content": "Subagent k1 completed"}],
        query_type="system",
        report_source={"kind": "task", "task_id": "task-valid", "display_id": "k1"},
    )
    normal_req = ChatRequest(
        messages=[{"role": "user", "content": "Start a new analysis task"}],
    )

    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={
                "workspace_id": "ws-b",
                "user_id": "u-internal-b",
                "name": "TSMC Workspace",
                "is_home": False,
                "status": "running",
            },
        ),
    )
    monkeypatch.setattr(
        ws_db,
        "workspace_is_moving",
        create_autospec(ws_db.workspace_is_moving, return_value=False),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "resolve_turn_origin_channel",
        create_autospec(
            dalton_sharing.resolve_turn_origin_channel,
            return_value="cos",
        ),
    )

    # Step 1: At HTTP entry, B's CoS sharing is still open -> both turns pass entry with is_tail_turn=False
    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": True},
        web_sharing_by_ws={"ws-b": "ro"},
    )
    _, ch_report, is_tail_entry = await dalton_sharing.resolve_colleague_turn_admission(
        user_id="u-internal-a",
        workspace_id="ws-b",
        thread_id=thread_id,
        request=report_req,
        thread_meta=thread_meta,
        is_existing_colleague_thread=True,
        is_internal=True,
        is_dispatch=True,
        requested_workspace_fn=turn_runtime.requested_workspace,
    )
    assert ch_report == "cos"
    assert is_tail_entry is False

    # Step 2: Before START (lock_run_workspace) acquires FOR SHARE OF w, B revokes CoS sharing
    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": False},
        web_sharing_by_ws={"ws-b": "ro"},
    )

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    executed: list[tuple[str, Any]] = []

    async def _fake_execute(sql: str, params: Any = None) -> None:
        executed.append((sql, params))

    async def _fake_fetchone() -> dict[str, Any] | None:
        sql, params = executed[-1]
        if "FROM workspaces w" in sql:
            return {
                "workspace_id": "ws-b",
                "user_id": "u-internal-b",
                "name": "TSMC Workspace",
                "status": "running",
                "is_home": False,
                "thread_user_id": "u-internal-a",
                "thread_metadata": thread_meta,
            }
        if "FROM subagent_runs sr" in sql and params and params[1] == "task-valid":
            return {"ok": 1}
        return None

    cursor.execute.side_effect = _fake_execute
    cursor.fetchone.side_effect = _fake_fetchone
    conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cursor

    conn.cursor.side_effect = _cursor_cm

    # Subagent completion report-back re-evaluates verify_server_tail_turn under lock, succeeds as a tail turn,
    # and sets tail_turn_no_new_work=True so spawning tools are trimmed.
    run_meta: dict[str, Any] = {"origin_channel": "cos"}
    locked_tail = await ws_db.lock_run_workspace(
        conn,
        thread_id,
        turn_user_id="u-internal-a",
        origin_channel="cos",
        is_tail_turn=is_tail_entry,
        query_type=report_req.query_type,
        report_source=report_req.report_source,
        is_internal=True,
        is_dispatch=True,
        run_metadata=run_meta,
    )
    assert locked_tail is True
    assert run_meta["tail_turn_no_new_work"] is True

    # Verify tool trimming when building the agent with the promoted tail_turn_no_new_work flag
    llm = GenericFakeChatModel(messages=iter([]))
    monkeypatch.setattr(
        AgentConfig,
        "get_llm_client",
        create_autospec(AgentConfig.get_llm_client, return_value=llm),
    )
    monkeypatch.setattr(
        compact_mod,
        "create_llm",
        create_autospec(compact_mod.create_llm, return_value=llm),
    )
    captured: list[dict[str, Any]] = []

    def _fake_create_agent(_model: Any, **built: Any) -> Any:
        captured.append(built)
        graph = create_autospec(CompiledStateGraph, instance=True)
        graph.with_config.return_value = graph
        return graph

    monkeypatch.setattr(
        agent_mod,
        "create_agent",
        create_autospec(agent_mod.create_agent, side_effect=_fake_create_agent),
    )
    cfg = AgentConfig(
        llm=LLMConfig(name="gpt-5"),
        security=SecurityConfig(),
        logging=LoggingConfig(),
        sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test")),
        mcp=MCPConfig(),
        filesystem=FilesystemConfig(enable_path_validation=False),
    )
    ptc = PTCAgent(cfg)
    sandbox = create_autospec(PTCSandbox(cfg.to_core_config()), instance=True)
    sandbox.config = cfg.to_core_config()
    sandbox.working_dir = "/home/daytona"
    sandbox.layout = SandboxLayout("/home/daytona")
    sandbox.workspace.return_value = WorkspaceLayout("/home/daytona", "ws-b")
    sandbox.skills_manifest = None
    sandbox.livefs = None
    sandbox.transcripts = None
    mcp_registry = create_autospec(MCPRegistry(cfg.to_core_config()), instance=True)
    mcp_registry.connectors = {}
    mcp_registry.get_all_tools.return_value = {}
    store = create_autospec(BaseStore, instance=True)
    project = ProjectContext(workspace_id="ws-b", dir_name="ws-b")

    ptc.create_agent(
        sandbox=sandbox,
        mcp_registry=mcp_registry,
        llm=llm,
        thread_id="th-12345678",
        user_id="u-internal-a",
        store=store,
        project=project,
        tail_turn_no_new_work=bool(run_meta.get("tail_turn_no_new_work")),
    )
    tail_tools = {t.name for t in captured[-1]["tools"]}
    tail_mw = {type(m).__name__ for m in captured[-1]["middleware"]}
    assert "schedule_wakeup" not in tail_tools
    assert "RunWorkflow" not in tail_tools
    assert "SubAgentMiddleware" not in tail_mw
    assert "TaskOutput" in tail_tools

    # Ordinary new message under the same race (entry passed with is_tail_turn=False, B revoked before START) is rejected
    normal_meta: dict[str, Any] = {"origin_channel": "cos"}
    with pytest.raises(ColleagueAccessRevokedError):
        await ws_db.lock_run_workspace(
            conn,
            thread_id,
            turn_user_id="u-internal-a",
            origin_channel="cos",
            is_tail_turn=False,
            query_type=normal_req.query_type,
            report_source=normal_req.report_source,
            run_metadata=normal_meta,
        )
    assert "tail_turn_no_new_work" not in normal_meta


@pytest.mark.asyncio
async def test_retry_of_interrupted_wakeup_turn_admitted_as_tail_turn_after_revocation_without_ending_wakeup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When an already-admitted wakeup turn fails with worker_lost and B revokes access, A's /retry is admitted as a tail turn without terminating the wakeup, whereas a new wakeup turn is rejected and terminated."""
    from src.server.services import turn_runtime
    from src.server.services.wakeups import cancel as wakeup_cancel_mod

    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": False},
        web_sharing_by_ws={"ws-b": "ro"},
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={
                "workspace_id": "ws-b",
                "user_id": "u-internal-b",
                "name": "TSMC Workspace",
                "is_home": False,
                "status": "running",
            },
        ),
    )

    thread_id = str(uuid4())
    w_cos_id = str(uuid4())
    failed_wake_run_id = str(uuid4())
    thread_meta = {
        "dispatched_by": {"user_id": "u-internal-a", "channel": "cos"},
    }

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    executed: list[tuple[str, Any]] = []

    async def _fake_execute(sql: str, params: Any = None) -> None:
        executed.append((sql, params))

    async def _fake_fetchone() -> dict[str, Any] | None:
        sql, _params = executed[-1]
        if "SELECT conversation_response_id, status, errors, metadata" in sql:
            return {
                "conversation_response_id": failed_wake_run_id,
                "status": "error",
                "errors": [{"code": "worker_lost", "message": "worker crashed"}],
                "metadata": {"origin_channel": "cos"},
            }
        if "WHERE r.conversation_response_id = %s" in sql:
            return {"origin_channel": "cos", "thread_channel": "cos"}
        if "WHERE w.wakeup_id = %s" in sql:
            return {"origin_channel": "cos", "thread_channel": "cos"}
        return None

    cursor.execute.side_effect = _fake_execute
    cursor.fetchone.side_effect = _fake_fetchone
    conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cursor

    @asynccontextmanager
    async def _conn_cm(*args: Any, **kwargs: Any):
        yield conn

    conn.cursor.side_effect = _cursor_cm
    monkeypatch.setattr(
        dalton_sharing,
        "get_db_connection",
        create_autospec(dalton_sharing.get_db_connection, side_effect=_conn_cm),
    )

    mock_end_wakeup = create_autospec(wakeup_cancel_mod.end_wakeup, return_value=None)
    monkeypatch.setattr(wakeup_cancel_mod, "end_wakeup", mock_end_wakeup)

    # 1. /retry of the worker_lost wakeup turn reconstructs query_type="wakeup" and wakeup={"v": 1, "notices": [...]}
    #    with retry_of_run_id=failed_wake_run_id -> admitted as a tail turn (is_tail_turn=True) and wakeup is NOT terminated
    retry_wake_req = ChatRequest(
        messages=[{"role": "user", "content": "<wakeup>\ncos timer fired\n</wakeup>"}],
        query_type="wakeup",
        wakeup={
            "v": 1,
            "notices": [{"wakeup_id": w_cos_id, "reason": "cos timer"}],
        },
        retry_of_run_id=failed_wake_run_id,
    )
    ws_retry, ch_retry, is_tail_retry = (
        await dalton_sharing.resolve_colleague_turn_admission(
            user_id="u-internal-a",
            workspace_id="ws-b",
            thread_id=thread_id,
            request=retry_wake_req,
            thread_meta=thread_meta,
            is_existing_colleague_thread=True,
            is_internal=False,
            is_dispatch=False,
            requested_workspace_fn=turn_runtime.requested_workspace,
        )
    )
    assert ws_retry is not None
    assert ch_retry == "cos"
    assert is_tail_retry is True
    assert len(retry_wake_req.wakeup["notices"]) == 1
    mock_end_wakeup.assert_not_awaited()

    # 2. A newly due wakeup turn (without retry_of_run_id) under the same revocation is rejected and its wakeup is terminated
    new_wake_req = ChatRequest(
        messages=[{"role": "user", "content": "<wakeup>\ncos timer fired\n</wakeup>"}],
        query_type="wakeup",
        wakeup={
            "v": 1,
            "notices": [{"wakeup_id": w_cos_id, "reason": "cos timer"}],
        },
    )
    with pytest.raises(ColleagueAccessRevokedError):
        await dalton_sharing.resolve_colleague_turn_admission(
            user_id="u-internal-a",
            workspace_id="ws-b",
            thread_id=thread_id,
            request=new_wake_req,
            thread_meta=thread_meta,
            is_existing_colleague_thread=True,
            is_internal=True,
            is_dispatch=True,
            requested_workspace_fn=turn_runtime.requested_workspace,
        )
    mock_end_wakeup.assert_awaited_once_with(thread_id, w_cos_id, "disabled")





@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("errors", "checks_each_notice"),
    [(["timeout"], True), (["worker_lost"], False)],
)
async def test_start_lock_skips_per_notice_check_only_for_a_resumable_retry(
    monkeypatch: pytest.MonkeyPatch, errors: list[str], checks_each_notice: bool
) -> None:
    """An ordinary retry of a merged web+cos wakeup is new work: inside the
    START lock every notice's channel is re-checked, so cos revoked between the
    entry check and START stops it even though web stays writable. Only a
    ledger-verified resumable retry skips that and is re-admitted as the run it
    continues."""
    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": False},
        web_sharing_by_ws={"ws-b": "rw"},
    )
    thread_id = str(uuid4())
    retried_run_id = str(uuid4())

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    executed: list[tuple[str, Any]] = []

    async def _fake_execute(sql: str, params: Any = None) -> None:
        executed.append((sql, params))

    async def _fake_fetchone() -> dict[str, Any] | None:
        sql, _params = executed[-1]
        if "FROM conversation_responses" in sql:
            return {
                "conversation_response_id": retried_run_id,
                "status": "error",
                "errors": errors,
                "metadata": {"origin_channel": "web"},
            }
        if "FROM workspaces w" in sql:
            return {
                "workspace_id": "ws-b",
                "user_id": "u-internal-b",
                "name": "TSMC Workspace",
                "status": "running",
                "is_home": False,
                "thread_user_id": "u-internal-a",
                "thread_metadata": {
                    "dispatched_by": {"user_id": "u-internal-a", "channel": "web"}
                },
            }
        if "AS moving" in sql:
            return {"moving": False}
        return None

    cursor.execute.side_effect = _fake_execute
    cursor.fetchone.side_effect = _fake_fetchone
    conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cursor

    conn.cursor.side_effect = _cursor_cm

    per_notice = create_autospec(
        dalton_sharing.verify_locked_wakeup_notices,
        side_effect=ColleagueAccessRevokedError(),
    )
    monkeypatch.setattr(dalton_sharing, "verify_locked_wakeup_notices", per_notice)

    call = ws_db.lock_run_workspace(
        conn,
        thread_id,
        turn_user_id="u-internal-a",
        origin_channel="web",
        is_tail_turn=False,
        wakeup_notice_ids=["w-web", "w-cos"],
        query_type="wakeup",
        retry_of_run_id=retried_run_id,
    )
    if checks_each_notice:
        with pytest.raises(ColleagueAccessRevokedError):
            await call
        per_notice.assert_awaited_once()
    else:
        await call
        per_notice.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_thread_by_id_projects_ownership_and_dispatched_by(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 3: get_thread_by_id joins workspaces and selects thread_user_id, workspace_owner_id, and dispatched_by."""
    from src.server.database import pool as db_pool
    from src.server.database.conversation import threads_read

    thread_id = str(uuid4())
    executed_sql: list[str] = []

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)

    async def _fake_execute(sql: str, params: Any = None) -> None:
        executed_sql.append(sql)

    cursor.execute.side_effect = _fake_execute
    cursor.fetchone.return_value = {
        "conversation_thread_id": thread_id,
        "workspace_id": "ws-b",
        "user_id": "u-internal-a",
        "thread_user_id": "u-internal-a",
        "workspace_owner_id": "u-internal-b",
        "dispatched_by": {
            "user_id": "u-internal-a",
            "name": "Chao Yu",
            "channel": "cos",
        },
    }
    conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cursor

    @asynccontextmanager
    async def _db_cm(*args: Any, **kwargs: Any):
        yield conn

    conn.cursor.side_effect = _cursor_cm
    monkeypatch.setattr(
        db_pool,
        "get_db_connection",
        create_autospec(db_pool.get_db_connection, side_effect=_db_cm),
    )

    row = await threads_read.get_thread_by_id(thread_id)
    assert row is not None
    assert row["thread_user_id"] == "u-internal-a"
    assert row["workspace_owner_id"] == "u-internal-b"
    assert row["dispatched_by"]["user_id"] == "u-internal-a"
    assert any("LEFT JOIN workspaces w" in s for s in executed_sql)
    assert any("AS thread_user_id" in s for s in executed_sql)
    assert any("AS workspace_owner_id" in s for s in executed_sql)
    assert any("AS dispatched_by" in s for s in executed_sql)


@pytest.mark.asyncio
async def test_thread_commands_feedback_and_seen_endpoints_for_non_creator_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 4: authorized non-creator reader (e.g. workspace owner B viewing A's thread)
    gets 200 on GET /commands, GET /feedback, and POST /seen (no-op without stamping DB),
    while write endpoints (stop command, submit/delete feedback) remain 403."""
    from src.server.app.threads import commands as threads_commands
    from src.server.app.threads import crud as threads_crud
    from src.server.app.threads import feedback as threads_feedback
    from src.server.database.runs import command_runs as cr_db
    from src.server.models.conversation import FeedbackRequest

    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": True},
        web_sharing_by_ws={"ws-b": "hidden"},
    )
    thread_id = str(uuid4())
    run_id = str(uuid4())

    from src.server.database.conversation import threads_read

    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "workspace_id": "ws-b",
            "user_id": "u-internal-a",
            "thread_user_id": "u-internal-a",
            "workspace_owner_id": "u-internal-b",
        },
    )
    monkeypatch.setattr(
        dalton_sharing.conv_db,
        "get_thread_auth_meta",
        meta_mock,
    )
    monkeypatch.setattr(
        threads_read,
        "get_thread_auth_meta",
        meta_mock,
    )
    monkeypatch.setattr(
        dalton_sharing.ws_db,
        "get_workspace",
        create_autospec(
            dalton_sharing.ws_db.get_workspace,
            return_value={
                "workspace_id": "ws-b",
                "user_id": "u-internal-b",
                "name": "TSMC Workspace",
                "status": "running",
                "is_home": False,
            },
        ),
    )
    monkeypatch.setattr(
        cr_db,
        "list_for_thread",
        create_autospec(cr_db.list_for_thread, return_value=[]),
    )
    monkeypatch.setattr(
        threads_feedback,
        "get_feedback_for_thread",
        create_autospec(threads_feedback.get_feedback_for_thread, return_value=[]),
    )
    stamp_mock = create_autospec(
        threads_crud.stamp_thread_seen,
        return_value={"last_seen_run_seq": 3, "latest_run_seq": 3},
    )
    monkeypatch.setattr(threads_crud, "stamp_thread_seen", stamp_mock)

    # 1. GET /commands succeeds for workspace owner B viewing A's thread
    cmd_res = await threads_commands.list_commands(
        thread_id, x_user_id="u-internal-b", status=None, limit=50
    )
    assert cmd_res.thread_id == thread_id
    assert cmd_res.commands == []

    # 2. GET /feedback succeeds for workspace owner B viewing A's thread
    fb_res = await threads_feedback.get_feedback(
        thread_id, x_user_id="u-internal-b"
    )
    assert fb_res == []

    # 3. POST /seen is a 200 no-op for non-creator B (does NOT call stamp_thread_seen)
    seen_res_b = await threads_crud.mark_thread_seen(
        thread_id,
        threads_crud.ThreadSeenRequest(run_id=run_id),
        x_user_id="u-internal-b",
    )
    assert seen_res_b.last_seen_run_seq == 0
    assert stamp_mock.await_count == 0

    # Thread creator A calling POST /seen DOES call stamp_thread_seen
    seen_res_a = await threads_crud.mark_thread_seen(
        thread_id,
        threads_crud.ThreadSeenRequest(run_id=run_id),
        x_user_id="u-internal-a",
    )
    assert seen_res_a.last_seen_run_seq == 3
    assert stamp_mock.await_count == 1

    # 4. Write endpoints still enforce thread owner (B gets 403 on A's thread)
    with pytest.raises(HTTPException) as exc_stop:
        await threads_commands.stop_thread_command(
            thread_id, "cmd-1", x_user_id="u-internal-b"
        )
    assert exc_stop.value.status_code == 403

    with pytest.raises(HTTPException) as exc_fb_post:
        await threads_feedback.submit_feedback(
            thread_id,
            FeedbackRequest(turn_index=0, rating="thumbs_up"),
            x_user_id="u-internal-b",
        )
    assert exc_fb_post.value.status_code == 403


def test_lifecycle_fields_clamps_last_seen_run_seq_for_non_creator() -> None:
    """Item 5: _lifecycle_fields sets unseen=False, is_read=True, and
    last_seen_run_seq >= latest_run_seq for non-creator viewers so the frontend
    store never shows an unread dot on a colleague's thread."""
    from datetime import UTC, datetime

    from src.server.app.threads.crud import _lifecycle_fields

    row = {
        "conversation_thread_id": "th-colleague",
        "workspace_id": "ws-b",
        "user_id": "u-internal-a",
        "thread_user_id": "u-internal-a",
        "workspace_owner_id": "u-internal-b",
        "current_status": "completed",
        "last_seen_run_seq": 0,
        "latest_run_id": "run-1",
        "latest_run_status": "completed",
        "latest_cancel_requested_at": None,
        "latest_interrupt_reason": None,
        "latest_run_seq": 4,
        "latest_run_started_at": datetime.now(UTC),
    }

    # Viewer B (not creator A): unseen=False, is_read=True, last_seen_run_seq=4
    lc_b = _lifecycle_fields(row, current_user_id="u-internal-b")
    assert lc_b["unseen"] is False
    assert lc_b["is_read"] is True
    assert lc_b["last_seen_run_seq"] == 4

    # Creator A: unseen=True, is_read=False, last_seen_run_seq=0
    lc_a = _lifecycle_fields(row, current_user_id="u-internal-a")
    assert lc_a["unseen"] is True
    assert lc_a["is_read"] is False
    assert lc_a["last_seen_run_seq"] == 0


@pytest.mark.asyncio
async def test_delegate_to_analyst_cleans_up_precreated_colleague_thread_and_surfaces_oauth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 6: When dispatching a new thread to a colleague workspace fails with 400
    (e.g. oauth_required), the pre-created thread row is deleted and the error detail
    is returned to Chief of Staff."""
    from src.server.database import conversation as conv_pkg
    from src.tools.secretary import dispatch as dispatch_mod
    from src.tools.secretary.chief_of_staff import delegate_to_analyst
    from tests.unit.server.handlers.chat.redis_fakes import FakeCache
    from tests.unit.tools.secretary.conftest import FakeResp, FakeSession

    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": True},
        web_sharing_by_ws={"ws-b": "hidden"},
    )
    from src.config import settings as app_settings

    monkeypatch.setattr(
        app_settings,
        "background_dispatch_requires_token",
        create_autospec(
            app_settings.background_dispatch_requires_token, return_value=False
        ),
    )
    fake_cache = FakeCache()
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client", lambda: fake_cache
    )
    ws_b_id = "22222222-2222-4222-8222-222222222222"
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={
                "workspace_id": ws_b_id,
                "user_id": "u-internal-b",
                "name": "ORCL Research",
                "status": "running",
                "is_home": False,
            },
        ),
    )
    create_mock = create_autospec(conv_pkg.create_thread, return_value={})
    delete_mock = create_autospec(conv_pkg.delete_thread, return_value=True)
    monkeypatch.setattr(conv_pkg, "create_thread", create_mock)
    monkeypatch.setattr(conv_pkg, "delete_thread", delete_mock)
    monkeypatch.setattr(
        dispatch_mod,
        "hitl_confirm",
        create_autospec(dispatch_mod.hitl_confirm, return_value=(True, {})),
    )

    oauth_resp = FakeResp(
        status=400,
        body={
            "detail": {
                "type": "oauth_required",
                "message": "Model 'claude-opus-4-6' requires a connected Claude account.",
                "provider": "claude-oauth",
            }
        },
    )
    monkeypatch.setattr(
        "aiohttp.ClientSession", lambda: FakeSession(oauth_resp)
    )

    cmd = await delegate_to_analyst.ainvoke(
        {
            "name": "delegate_to_analyst",
            "id": "call_oauth_fail",
            "type": "tool_call",
            "args": {
                "question": "Check ORCL capex",
                "workspace_id": ws_b_id,
                "state": {},
            },
        },
        config={
            "configurable": {
                "user_id": "u-internal-a",
                "thread_id": "cos-home-thread",
            }
        },
    )
    payload = json.loads(cmd.update["messages"][0].content)
    assert payload["success"] is False
    assert "oauth_required" in payload["error"]
    assert "requires a connected Claude account" in payload["error"]
    assert create_mock.await_count == 1
    created_tid = create_mock.await_args.kwargs["conversation_thread_id"]
    delete_mock.assert_awaited_once_with(created_tid)


@pytest.mark.asyncio
async def test_context_menu_backend_guards_across_four_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify backend guards for sidebar context menu actions (rename, archive, delete, share,
    workspace delete guard) across owner, dispatcher, bystander colleague, and external member."""
    from src.dalton.database import publications as dalton_pubs
    from src.server.app import workspaces as workspaces_app
    from src.server.app.threads import crud as threads_crud
    from src.server.app.threads import sharing as threads_sharing
    from src.server.database import conversation as conv_pkg
    from src.server.models.conversation import ThreadShareRequest, ThreadUpdateRequest

    _setup_roles(
        monkeypatch,
        dispatch_enabled=True,
        cos_sharing_by_owner={"u-internal-b": True},
        web_sharing_by_ws={"ws-b": "rw"},
    )

    own_thread_id = "11111111-1111-4111-8111-111111111111"
    dispatched_thread_id = "22222222-2222-4222-8222-222222222222"

    meta_by_tid = {
        own_thread_id: {
            "workspace_id": "ws-b",
            "user_id": "u-internal-b",
            "owner_id": "u-internal-b",
            "thread_user_id": "u-internal-b",
            "workspace_owner_id": "u-internal-b",
        },
        dispatched_thread_id: {
            "workspace_id": "ws-b",
            "user_id": "u-internal-a",
            "owner_id": "u-internal-a",
            "thread_user_id": "u-internal-a",
            "workspace_owner_id": "u-internal-b",
        },
    }
    row_by_tid = {
        own_thread_id: {
            "conversation_thread_id": own_thread_id,
            "workspace_id": "ws-b",
            "user_id": "u-internal-b",
            "thread_user_id": "u-internal-b",
            "workspace_owner_id": "u-internal-b",
            "thread_index": 0,
            "current_status": "completed",
            "title": "Owner Thread",
            "is_shared": False,
            "is_pinned": False,
            "archived_at": None,
            "subagents_allowed": True,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "share_token": "tok-own",
            "share_permissions": {},
        },
        dispatched_thread_id: {
            "conversation_thread_id": dispatched_thread_id,
            "workspace_id": "ws-b",
            "user_id": "u-internal-a",
            "thread_user_id": "u-internal-a",
            "workspace_owner_id": "u-internal-b",
            "thread_index": 1,
            "current_status": "completed",
            "title": "Dispatched Thread",
            "is_shared": False,
            "is_pinned": False,
            "archived_at": None,
            "subagents_allowed": True,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "share_token": None,
            "share_permissions": {},
        },
    }

    async def _fake_owner_id(tid: str, *, conn: Any = None) -> str | None:
        m = meta_by_tid.get(tid)
        return m["user_id"] if m else None

    async def _fake_auth_meta(tid: str, *, conn: Any = None) -> dict[str, Any] | None:
        return meta_by_tid.get(tid)

    async def _fake_thread_by_id(tid: str) -> dict[str, Any] | None:
        return row_by_tid.get(tid)

    monkeypatch.setattr(
        conv_pkg,
        "get_thread_owner_id",
        create_autospec(conv_pkg.get_thread_owner_id, side_effect=_fake_owner_id),
    )
    monkeypatch.setattr(
        conv_pkg,
        "get_thread_auth_meta",
        create_autospec(conv_pkg.get_thread_auth_meta, side_effect=_fake_auth_meta),
    )
    monkeypatch.setattr(
        threads_sharing,
        "get_thread_by_id",
        create_autospec(threads_sharing.get_thread_by_id, side_effect=_fake_thread_by_id),
    )
    monkeypatch.setattr(
        threads_sharing,
        "update_thread_sharing",
        create_autospec(
            threads_sharing.update_thread_sharing,
            return_value={
                "is_shared": True,
                "share_token": "tok-own",
                "share_permissions": {},
            },
        ),
    )
    monkeypatch.setattr(
        threads_crud,
        "update_thread_fields",
        create_autospec(
            threads_crud.update_thread_fields,
            side_effect=lambda tid, **kw: {**row_by_tid[tid], **kw},
        ),
    )
    monkeypatch.setattr(
        threads_crud,
        "publish_thread_title",
        create_autospec(threads_crud.publish_thread_title),
    )

    # 1. Owner (u-internal-b) on own thread: rename & share succeed; on colleague-dispatched thread: 403
    renamed_own = await threads_crud.update_thread_endpoint(
        own_thread_id,
        ThreadUpdateRequest(title="Renamed by owner"),
        x_user_id="u-internal-b",
    )
    assert renamed_own.title == "Renamed by owner"

    shared_own = await threads_sharing.update_thread_share(
        own_thread_id,
        ThreadShareRequest(is_shared=True),
        x_user_id="u-internal-b",
    )
    assert shared_own.is_shared is True

    for fn in (
        lambda: threads_crud.update_thread_endpoint(
            dispatched_thread_id,
            ThreadUpdateRequest(title="Owner hijack"),
            x_user_id="u-internal-b",
        ),
        lambda: threads_crud.delete_thread_endpoint(
            dispatched_thread_id, x_user_id="u-internal-b"
        ),
        lambda: threads_sharing.update_thread_share(
            dispatched_thread_id,
            ThreadShareRequest(is_shared=True),
            x_user_id="u-internal-b",
        ),
        lambda: threads_sharing.get_thread_share(
            dispatched_thread_id, x_user_id="u-internal-b"
        ),
    ):
        with pytest.raises(HTTPException) as exc_owner_on_dispatched:
            await fn()
        assert exc_owner_on_dispatched.value.status_code == 403

    # 2. Dispatcher (u-internal-a) on own dispatched thread: rename succeeds, but public share is 403
    renamed_disp = await threads_crud.update_thread_endpoint(
        dispatched_thread_id,
        ThreadUpdateRequest(title="Renamed by dispatcher"),
        x_user_id="u-internal-a",
    )
    assert renamed_disp.title == "Renamed by dispatcher"

    with pytest.raises(HTTPException) as exc_disp_share:
        await threads_sharing.update_thread_share(
            dispatched_thread_id,
            ThreadShareRequest(is_shared=True),
            x_user_id="u-internal-a",
        )
    assert exc_disp_share.value.status_code == 403

    with pytest.raises(HTTPException) as exc_disp_get_share:
        await threads_sharing.get_thread_share(
            dispatched_thread_id, x_user_id="u-internal-a"
        )
    assert exc_disp_get_share.value.status_code == 403

    # 3. Bystander colleague (u-admin) on either thread: rename, delete, share all 403
    for tid in (own_thread_id, dispatched_thread_id):
        with pytest.raises(HTTPException) as exc_bystander_upd:
            await threads_crud.update_thread_endpoint(
                tid, ThreadUpdateRequest(title="Bystander"), x_user_id="u-admin"
            )
        assert exc_bystander_upd.value.status_code == 403

        with pytest.raises(HTTPException) as exc_bystander_del:
            await threads_crud.delete_thread_endpoint(tid, x_user_id="u-admin")
        assert exc_bystander_del.value.status_code == 403

        with pytest.raises(HTTPException) as exc_bystander_share:
            await threads_sharing.update_thread_share(
                tid, ThreadShareRequest(is_shared=True), x_user_id="u-admin"
            )
        assert exc_bystander_share.value.status_code == 403

    # 4. External member (u-external) on any foreign thread: strict 404 isolation
    for tid in (own_thread_id, dispatched_thread_id):
        with pytest.raises(HTTPException) as exc_ext_upd:
            await threads_crud.update_thread_endpoint(
                tid, ThreadUpdateRequest(title="External"), x_user_id="u-external"
            )
        assert exc_ext_upd.value.status_code == 404

        with pytest.raises(HTTPException) as exc_ext_del:
            await threads_crud.delete_thread_endpoint(tid, x_user_id="u-external")
        assert exc_ext_del.value.status_code == 404

        with pytest.raises(HTTPException) as exc_ext_share:
            await threads_sharing.update_thread_share(
                tid, ThreadShareRequest(is_shared=True), x_user_id="u-external"
            )
        assert exc_ext_share.value.status_code == 404

    # 5. Published report workspace delete protection (409) preserved for owner
    monkeypatch.setattr(
        workspaces_app,
        "db_get_workspace",
        create_autospec(
            workspaces_app.db_get_workspace,
            return_value={
                "workspace_id": "ws-b",
                "user_id": "u-internal-b",
                "name": "TSMC Workspace",
                "is_home": False,
                "status": "running",
            },
        ),
    )
    monkeypatch.setattr(
        dalton_pubs,
        "has_published_reports",
        create_autospec(dalton_pubs.has_published_reports, return_value=True),
    )
    with pytest.raises(HTTPException) as exc_pub_guard:
        await workspaces_app.delete_workspace("ws-b", x_user_id="u-internal-b")
    assert exc_pub_guard.value.status_code == 409
    assert "已发布研报" in str(exc_pub_guard.value.detail)
