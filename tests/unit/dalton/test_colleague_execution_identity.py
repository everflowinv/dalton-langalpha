"""Unit tests for dual execution identity, direct MCP trading filter, knowledge 3-tuple identity, secret redaction, and d020 migration."""

from __future__ import annotations

from typing import Any
from unittest.mock import create_autospec

import pytest
from langgraph.store.base import BaseStore
from sqlalchemy.engine import Connection, Result

from src.dalton.knowledge import reconcile as knowledge_reconcile
from src.dalton.knowledge import service as knowledge_service
from src.dalton.migrations.versions import d020_colleague_dispatch_threads as d020
from ptc_agent.agent import filesystem_routes
from ptc_agent.agent.backends.sandbox import SandboxBackend
from ptc_agent.agent.middleware.tool.leak_detection import LeakDetectionMiddleware
from src.server.services.brokerage_capabilities import CapabilityGroup
from src.server.services.egress import direct_tools
from src.server.services.egress.relay_jwt import MintedJwt
from src.server.services.mcp_tool_split import DirectServerTools, DirectTool
from src.server.services.tool_binding import Resolved


def test_filesystem_backend_uses_workspace_owner_id_for_store_namespaces() -> None:
    """build_filesystem_backend routes Store namespaces and user-data backends to workspace_owner_id (B) instead of caller (A)."""
    fake_backend = create_autospec(SandboxBackend, instance=True)
    fake_backend.computer_root = "/home/daytona"
    store = create_autospec(BaseStore, instance=True)
    gates = filesystem_routes.IdentityGates(
        user_memory=True,
        workspace_memory=True,
        memo=True,
        user_data=True,
        workflow=True,
        workflow_fs=True,
        workflow_tool=True,
        channels=False,
    )

    composite, baseline = filesystem_routes.build_filesystem_backend(
        backend=fake_backend,
        gates=gates,
        store=store,
        user_id="u-caller-a",
        workspace_id="ws-owner-b",
        workspace_owner_id="u-owner-b",
    )
    assert baseline is not None
    assert baseline.memory["user"].namespace_factory() == ("u-owner-b", "memory")
    assert baseline.memory["workspace"].namespace_factory() == (
        "u-owner-b",
        "workspaces",
        "ws-owner-b",
        "memory",
    )
    assert baseline.memo is not None
    assert baseline.memo.namespace_factory() == ("u-owner-b", "memos")
    user_data_routes = [
        r for r in getattr(composite, "_routes", []) if hasattr(r, "_user_id")
    ]
    assert len(user_data_routes) >= 3
    for r in user_data_routes:
        assert r._user_id == "u-owner-b"


@pytest.mark.asyncio
async def test_resolve_direct_mcp_tools_uses_owner_and_strips_trading_on_colleague_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prepare_direct_mcp_tools mints relay JWT for owner B and strips trading capability tools on cross-user turns."""
    monkeypatch.setattr(direct_tools, "EGRESS_RELAY_SECRET", "test-relay-secret-key-1234567890")

    minted_users: list[str] = []

    def _fake_mint_relay_jwt(
        secret: str,
        *,
        user_id: str,
        workspace_id: str,
        sandbox_id: str | None = None,
        computer_id: str | None = None,
        caller: str = "host",
    ) -> Any:
        minted_users.append(user_id)
        jwt_mock = create_autospec(MintedJwt, instance=True)
        jwt_mock.token = "jwt-owner-b"
        return jwt_mock

    monkeypatch.setattr(
        direct_tools,
        "mint_relay_jwt",
        create_autospec(direct_tools.mint_relay_jwt, side_effect=_fake_mint_relay_jwt),
    )

    admitted_tool_names: list[str] = []

    def _fake_admit_within_budget(
        grantable: dict[str, DirectServerTools],
    ) -> tuple[dict[str, tuple[DirectTool, ...]], list[tuple[str, DirectTool]]]:
        for srv_entry in grantable.values():
            for t in srv_entry.tools:
                admitted_tool_names.append(t.name)
        return {}, []

    monkeypatch.setattr(
        direct_tools,
        "admit_within_budget",
        create_autospec(
            direct_tools.admit_within_budget, side_effect=_fake_admit_within_budget
        ),
    )

    trading_group = create_autospec(CapabilityGroup, instance=True)
    trading_group.key = "trading"

    monkeypatch.setattr(
        direct_tools,
        "group_for_tool",
        create_autospec(
            direct_tools.group_for_tool,
            side_effect=lambda vendor, tool_name: (
                trading_group if tool_name == "account_positions" else None
            ),
        ),
    )

    trading_resolved = Resolved(
        binding="direct",
        source="default",
    )
    data_resolved = Resolved(
        binding="direct",
        source="default",
    )

    by_server = {
        "ibkr": DirectServerTools(
            tools=(
                DirectTool(
                    schema={"name": "account_positions", "inputSchema": {"type": "object"}},
                    resolved=trading_resolved,
                ),
            ),
            vendor="ibkr",
        ),
        "market": DirectServerTools(
            tools=(
                DirectTool(
                    schema={"name": "fetch_quote", "inputSchema": {"type": "object"}},
                    resolved=data_resolved,
                ),
            ),
            vendor=None,
        ),
    }

    binding = await direct_tools.prepare_direct_mcp_tools(
        owner_user_id="u-owner-b",
        caller_user_id="u-caller-a",
        workspace_id="ws-b",
        grants={"ibkr": "grant-1", "market": "grant-2"},
        by_server=by_server,
    )
    assert binding.user_id == "u-owner-b"
    assert minted_users == ["u-owner-b"]
    assert admitted_tool_names == ["fetch_quote"]


def test_leak_detection_redacts_both_owner_and_caller_same_name_secrets() -> None:
    """LeakDetectionMiddleware redacts both owner B's secret and caller A's #caller secret to [REDACTED:NAME]."""
    merged_secrets = {
        "POLYGON_API_KEY": "owner-b-secret-value-12345",
        "POLYGON_API_KEY#caller": "caller-a-secret-value-67890",
    }
    text = (
        "Leaked owner key: owner-b-secret-value-12345 and "
        "caller key: caller-a-secret-value-67890"
    )
    mw = LeakDetectionMiddleware(vault_secrets=merged_secrets)
    redacted = mw.redact(text)
    assert redacted is not None
    assert "owner-b-secret-value-12345" not in redacted
    assert "caller-a-secret-value-67890" not in redacted
    assert "#caller" not in redacted
    assert redacted.count("[REDACTED:POLYGON_API_KEY]") == 2


@pytest.mark.asyncio
async def test_knowledge_three_tuple_identity_and_admitted_workspace_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Knowledge detect_direct_edits uses storage_owner_id=B for file resolution and user_id=A as author."""
    verify_calls: list[tuple[str, str]] = []

    async def _fake_verify(
        workspace_id: str,
        user_id: str | None,
        *,
        need: str = "read",
        channel: str = "web",
    ) -> dict[str, Any]:
        verify_calls.append((workspace_id, str(user_id)))
        return {"workspace_id": workspace_id}

    monkeypatch.setattr(
        knowledge_service,
        "verify_workspace_access",
        create_autospec(knowledge_service.verify_workspace_access, side_effect=_fake_verify),
    )

    # 1. When skip_access_check=True (admitted workspace), apply_update skips inner verify_workspace_access
    res_skip = await knowledge_service.apply_update(
        user_id="u-caller-a",
        workspace_id="ws-admitted",
        doc_key="invalid_doc_for_fast_exit",
        ops=[],
        reason="test",
        base_version=None,
        skip_access_check=True,
    )
    assert res_skip["error"] == "invalid_doc_key"
    assert verify_calls == []

    # 2. When skip_access_check=False (cross-workspace), apply_update calls verify_workspace_access
    await knowledge_service.apply_update(
        user_id="u-caller-a",
        workspace_id="ws-other",
        doc_key="invalid_doc_for_fast_exit",
        ops=[],
        reason="test",
        base_version=None,
        skip_access_check=False,
    )
    assert verify_calls == [("ws-other", "u-caller-a")]

    # 3. detect_direct_edits passes storage_owner_id=B to resolve_file_text_or_none and user_id=A to _insert_reconcile_version
    resolved_users: list[str | None] = []
    inserted_authors: list[str | None] = []

    monkeypatch.setattr(
        knowledge_reconcile,
        "get_file_metadata_for_sync",
        create_autospec(
            knowledge_reconcile.get_file_metadata_for_sync,
            return_value={
                "data/company.json": {
                    "kind": "file",
                    "content_hash": "new-sha-123",
                }
            },
        ),
    )
    monkeypatch.setattr(
        knowledge_reconcile.db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(
            knowledge_reconcile.db_knowledge.list_all_latest_for_workspace,
            return_value={},
        ),
    )
    monkeypatch.setattr(
        knowledge_reconcile.db_publications,
        "list_publication_files",
        create_autospec(
            knowledge_reconcile.db_publications.list_publication_files,
            return_value=[],
        ),
    )
    monkeypatch.setattr(
        knowledge_reconcile,
        "db_get_file",
        create_autospec(
            knowledge_reconcile.db_get_file,
            return_value={"file_path": "data/company.json"},
        ),
    )

    async def _fake_resolve_file(
        file_row: dict[str, Any] | None,
        *,
        user_id: str | None = None,
        context: str = "",
    ) -> str | None:
        resolved_users.append(user_id)
        return '{"ticker": "TSM", "name": "TSMC"}'

    monkeypatch.setattr(
        knowledge_reconcile,
        "resolve_file_text_or_none",
        create_autospec(
            knowledge_reconcile.resolve_file_text_or_none, side_effect=_fake_resolve_file
        ),
    )

    async def _fake_insert_reconcile_version(
        *,
        user_id: str,
        workspace_id: str,
        doc_key: str,
        base_version_no: int | None,
        content: dict[str, Any],
        content_sha256: str,
        reason: str,
        warnings: list[dict[str, Any]],
        schema_valid: bool,
        edited_path: str,
    ) -> dict[str, Any] | None:
        inserted_authors.append(user_id)
        return {"version_no": 1, "doc_key": doc_key}

    monkeypatch.setattr(
        knowledge_reconcile,
        "_insert_reconcile_version",
        create_autospec(
            knowledge_reconcile._insert_reconcile_version,
            side_effect=_fake_insert_reconcile_version,
        ),
    )

    reconciled = await knowledge_reconcile.detect_direct_edits(
        "ws-admitted",
        "u-caller-a",
        storage_owner_id="u-owner-b",
        skip_access_check=True,
    )
    assert len(reconciled["recorded_versions"]) == 1
    assert resolved_users == ["u-owner-b"]
    assert inserted_authors == ["u-caller-a"]


def test_d020_migration_uses_sql_literal_for_comment_and_never_drops_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify d020 _ensure_nullable_column uses SQL literal for COMMENT ON COLUMN and downgrade never drops columns."""
    executed_sql: list[tuple[str, Any]] = []

    res = create_autospec(Result, instance=True)
    res.fetchone.return_value = None

    conn = create_autospec(Connection, instance=True)

    def _fake_execute(stmt: Any, params: Any = None) -> Result:
        executed_sql.append((str(stmt), params))
        return res

    conn.execute.side_effect = _fake_execute
    d020._ensure_nullable_column(
        conn,
        table="conversation_threads",
        column="user_id",
        col_ddl="VARCHAR(255)",
        expected_types=("character varying", "text"),
        comment="dalton:d020:thread_owner_user_id",
    )

    comment_stmts = [s for s, _ in executed_sql if "COMMENT ON COLUMN" in s]
    assert len(comment_stmts) == 1
    assert ":comment" not in comment_stmts[0]
    assert "'dalton:d020:thread_owner_user_id'" in comment_stmts[0]

    # Inspect upgrade and downgrade source to verify Home check, invalid index cleanup, lock_timeout retry, and non-destructive downgrade
    import inspect

    upgrade_src = inspect.getsource(d020.upgrade)
    assert "w.is_home" not in upgrade_src
    assert "SET lock_timeout = '5s'" in upgrade_src
    assert "_ensure_concurrent_index" in upgrade_src

    executed_idx_sql: list[str] = []
    inv_res = create_autospec(Result, instance=True)
    inv_res.fetchone.return_value = (False,)
    idx_conn = create_autospec(Connection, instance=True)

    def _fake_idx_execute(stmt: Any, params: Any = None) -> Result:
        executed_idx_sql.append(str(stmt))
        return inv_res

    idx_conn.execute.side_effect = _fake_idx_execute
    d020._ensure_concurrent_index(
        idx_conn,
        index_name="idx_conversation_threads_user_updated",
        create_sql="CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_conversation_threads_user_updated ON conversation_threads (user_id, updated_at DESC)",
    )
    assert any("indisvalid" in s for s in executed_idx_sql)
    assert any(
        "DROP INDEX CONCURRENTLY IF EXISTS idx_conversation_threads_user_updated" in s
        for s in executed_idx_sql
    )
    assert any(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_conversation_threads_user_updated"
        in s
        for s in executed_idx_sql
    )

    downgrade_src = inspect.getsource(d020.downgrade)
    assert "DROP COLUMN" not in downgrade_src.upper()
    assert "DROP INDEX CONCURRENTLY IF EXISTS" in downgrade_src

    # Verify _migrate_user_cos_sharing uses runtime is_home_row (retains UUIDv5 research workspaces, excludes Home and Home alias)
    import uuid
    import src.dalton.settings as home_alias_mod
    from src.server.database.home_workspace import get_home_workspace_id

    meta_v5_ws_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, "dalton.workspace.META"))
    real_home_u1 = get_home_workspace_id("u-v5-research")
    aliased_home_u2 = str(uuid.uuid4())
    research_ws_u2 = str(uuid.uuid5(uuid.NAMESPACE_DNS, "dalton.workspace.AMZN"))

    monkeypatch.setattr(
        home_alias_mod,
        "get_home_workspace_alias",
        create_autospec(
            home_alias_mod.get_home_workspace_alias,
            side_effect=lambda uid: aliased_home_u2 if uid == "u-aliased" else None,
        ),
    )

    mig_rows = [
        # u-v5-research: UUIDv5 research workspace (hidden) + real Home (rw) -> should migrate to FALSE
        ("u-v5-research", meta_v5_ws_id, "running", "hidden"),
        ("u-v5-research", real_home_u1, "running", "rw"),
        # u-aliased: aliased Home (rw) + unbound Home (rw) + UUIDv5 research workspace (hidden) -> should migrate to FALSE
        ("u-aliased", aliased_home_u2, "running", "rw"),
        ("u-aliased", str(uuid.uuid4()), "unbound", "rw"),
        ("u-aliased", research_ws_u2, "stopped", "hidden"),
        # u-open: research workspace (rw) + research workspace (hidden) -> should NOT migrate to FALSE
        ("u-open", str(uuid.uuid4()), "running", "rw"),
        ("u-open", str(uuid.uuid4()), "running", "hidden"),
    ]
    mig_res = create_autospec(Result, instance=True)
    mig_res.fetchall.return_value = mig_rows
    mig_conn = create_autospec(Connection, instance=True)
    inserted_batches: list[Any] = []

    def _fake_mig_execute(stmt: Any, params: Any = None) -> Result:
        if "INSERT INTO dalton_user_sharing" in str(stmt):
            inserted_batches.append(params)
        return mig_res

    mig_conn.execute.side_effect = _fake_mig_execute
    d020._migrate_user_cos_sharing(mig_conn)
    assert inserted_batches == [
        [{"user_id": "u-v5-research"}, {"user_id": "u-aliased"}]
    ]


@pytest.mark.asyncio
async def test_agent_inputs_and_budget_attribution_keep_caller_for_billing_and_owner_for_infra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentInputs.read uses workspace_owner_id=B for direct MCP while keeping user_id=A for caller/budget."""
    from ptc_agent.core.project_context import ProjectContext
    from ptc_agent.core.session import Session
    from src.server.services import thread_agent, workspace_layout
    from src.server.services.computer_manager._types import WorkspaceToolView
    from src.server.services.egress.direct_tools import DirectMCPBinding
    from src.server.services.workspace_manager import WorkspaceManager

    session = create_autospec(Session, instance=True)
    wm = create_autospec(WorkspaceManager, instance=True)
    view = create_autospec(WorkspaceToolView, instance=True)
    view.trading_rule = None
    wm.tool_view.return_value = view
    monkeypatch.setattr(
        WorkspaceManager,
        "get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=wm),
    )
    monkeypatch.setattr(
        workspace_layout,
        "resolve_project_context",
        create_autospec(
            workspace_layout.resolve_project_context,
            return_value=ProjectContext(workspace_id="ws-b", dir_name="ws-b"),
        ),
    )

    direct_calls: list[dict[str, Any]] = []

    async def _fake_direct_tools_for_turn(
        _sess: Any,
        *,
        user_id: str | None = None,
        owner_user_id: str | None = None,
        caller_user_id: str | None = None,
        workspace_id: str,
        view: Any = None,
    ) -> Any:
        direct_calls.append(
            {
                "user_id": user_id,
                "owner_user_id": owner_user_id,
                "caller_user_id": caller_user_id,
                "workspace_id": workspace_id,
            }
        )
        return create_autospec(DirectMCPBinding, instance=True)

    monkeypatch.setattr(
        direct_tools,
        "direct_tools_for_turn",
        create_autospec(
            direct_tools.direct_tools_for_turn, side_effect=_fake_direct_tools_for_turn
        ),
    )

    inputs = await thread_agent.AgentInputs.read(
        session,
        user_id="u-caller-a",
        workspace_id="ws-b",
        thread_id="th-1",
        role="analyst",
        user_profile={"name": "Caller A"},
        workspace_owner_id="u-owner-b",
    )
    assert inputs.user_id == "u-caller-a"
    assert inputs.workspace_owner_id == "u-owner-b"
    assert direct_calls == [
        {
            "user_id": "u-owner-b",
            "owner_user_id": "u-owner-b",
            "caller_user_id": "u-caller-a",
            "workspace_id": "ws-b",
        }
    ]
    built_kwargs = inputs.build_kwargs()
    assert built_kwargs["user_id"] == "u-caller-a"
    assert built_kwargs["workspace_owner_id"] == "u-owner-b"


@pytest.mark.asyncio
async def test_thread_maintenance_summarize_uses_workspace_owner_for_session_and_store_and_caller_for_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When colleague A calls /summarize on A's thread in B's workspace, session/store/Direct MCP use B while LLM/budget use A."""
    from contextlib import asynccontextmanager
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.pregel import Pregel
    from langgraph.types import StateSnapshot
    from ptc_agent.agent.graph import build_ptc_graph_with_session
    from ptc_agent.config.agent import AgentConfig
    from ptc_agent.core.project_context import ProjectContext
    from ptc_agent.core.session import Session
    from src.server.app import setup as app_setup
    from src.server.database import conversation as qr_db, user as user_db
    from src.server.handlers import thread_maintenance
    from src.server.services import workspace_layout
    from src.server.services.computer_manager._types import WorkspaceToolView
    from src.server.services.egress.direct_tools import DirectMCPBinding
    from src.server.services.llm import thread_model
    from src.server.services.turn_runtime import TurnRoute
    from src.server.services.workspace_manager import WorkspaceManager

    thread_id = "11111111-1111-4111-8111-111111111111"
    ws_id = "22222222-2222-4222-8222-222222222222"

    monkeypatch.setattr(
        qr_db,
        "get_thread_with_summary",
        create_autospec(
            qr_db.get_thread_with_summary,
            return_value={
                "conversation_thread_id": thread_id,
                "workspace_id": ws_id,
                "workspace_owner_id": "u-owner-b",
                "current_status": "completed",
            },
        ),
    )

    session = create_autospec(Session, instance=True)
    session.sandbox = None
    wm = create_autospec(WorkspaceManager, instance=True)
    session_calls: list[tuple[str, str | None]] = []

    async def _fake_get_session(
        w_id: str, user_id: str | None = None, **kwargs: Any
    ) -> Session:
        session_calls.append((w_id, user_id))
        return session

    wm.get_session_for_workspace.side_effect = _fake_get_session
    view = create_autospec(WorkspaceToolView, instance=True)
    view.trading_rule = None
    wm.tool_view.return_value = view
    monkeypatch.setattr(
        WorkspaceManager,
        "get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=wm),
    )
    monkeypatch.setattr(
        workspace_layout,
        "resolve_project_context",
        create_autospec(
            workspace_layout.resolve_project_context,
            return_value=ProjectContext(workspace_id=ws_id, dir_name="ws-b"),
        ),
    )
    monkeypatch.setattr(
        user_db,
        "get_user_profile_for_prompt",
        create_autospec(
            user_db.get_user_profile_for_prompt,
            return_value={"name": "Caller A"},
        ),
    )

    direct_calls: list[dict[str, Any]] = []

    async def _fake_direct_tools(
        _sess: Any,
        *,
        user_id: str | None = None,
        owner_user_id: str | None = None,
        caller_user_id: str | None = None,
        workspace_id: str,
        view: Any = None,
    ) -> Any:
        direct_calls.append(
            {
                "user_id": user_id,
                "owner_user_id": owner_user_id,
                "caller_user_id": caller_user_id,
                "workspace_id": workspace_id,
            }
        )
        return create_autospec(DirectMCPBinding, instance=True)

    monkeypatch.setattr(
        direct_tools,
        "direct_tools_for_turn",
        create_autospec(
            direct_tools.direct_tools_for_turn, side_effect=_fake_direct_tools
        ),
    )

    graph_kwargs_captured: list[dict[str, Any]] = []
    fake_graph = create_autospec(Pregel, instance=True)
    fake_state = StateSnapshot(
        values={
            "messages": [
                HumanMessage(content="Question", id="m1"),
                AIMessage(content="Answer", id="m2"),
            ]
        },
        next=(),
        config={"configurable": {"checkpoint_id": "cp-1"}},
        metadata=None,
        created_at=None,
        parent_config=None,
        tasks=(),
        interrupts=(),
    )
    fake_graph.aget_state.return_value = fake_state

    async def _fake_build_graph(**kwargs: Any) -> Any:
        graph_kwargs_captured.append(kwargs)
        return fake_graph

    monkeypatch.setattr(
        "ptc_agent.agent.graph.build_ptc_graph_with_session",
        create_autospec(build_ptc_graph_with_session, side_effect=_fake_build_graph),
    )
    monkeypatch.setattr(
        thread_maintenance,
        "turn_base_checkpoint_id",
        create_autospec(
            thread_maintenance.turn_base_checkpoint_id, return_value="cp-1"
        ),
    )

    model_user_calls: list[str] = []
    fake_cfg = create_autospec(AgentConfig, instance=True)
    monkeypatch.setattr(app_setup, "agent_config", fake_cfg, raising=False)
    monkeypatch.setattr(
        thread_maintenance,
        "thread_route",
        create_autospec(
            thread_maintenance.thread_route,
            return_value=(
                {"workspace_id": ws_id, "llm_model": "gpt-5"},
                TurnRoute(workspace_id=ws_id, role="analyst"),
            ),
        ),
    )
    monkeypatch.setattr(
        thread_maintenance,
        "_resolver",
        create_autospec(thread_maintenance._resolver, return_value=None),
    )

    async def _fake_resolve_turn_config(
        u_id: str, t_id: str, **kwargs: Any
    ) -> AgentConfig:
        model_user_calls.append(u_id)
        return fake_cfg

    monkeypatch.setattr(
        thread_model,
        "resolve_turn_config",
        create_autospec(
            thread_model.resolve_turn_config, side_effect=_fake_resolve_turn_config
        ),
    )

    saver = create_autospec(BaseCheckpointSaver, instance=True)

    @asynccontextmanager
    async def _fake_hold(_tid: str, _verb: str):
        class _Mut:
            pass

        m = _Mut()
        m.saver = saver  # type: ignore[attr-defined]
        yield m

    monkeypatch.setattr(thread_maintenance, "_hold_thread_mutation", _fake_hold)

    async def _fake_ask(_self: Any, _ask: Any) -> Any:
        class _Summary:
            text = "Compacted summary"
            source = "model"

        class _Compaction:
            original_count = 2
            messages = [HumanMessage(content="Compacted summary", id="s1")]
            summary = _Summary()
            event = {"summary_message": HumanMessage(content="Compacted summary", id="s1")}

            @staticmethod
            def update(_vals: dict[str, Any]) -> dict[str, Any]:
                return {}

        return _Compaction()

    monkeypatch.setattr(thread_maintenance.ThreadAgent, "ask", _fake_ask)
    monkeypatch.setattr(
        thread_maintenance,
        "_summary_since_the_turn",
        create_autospec(thread_maintenance._summary_since_the_turn, return_value=None),
    )
    monkeypatch.setattr(
        thread_maintenance,
        "_update_graph_state",
        create_autospec(thread_maintenance._update_graph_state, return_value=None),
    )

    res = await thread_maintenance.trigger_compaction(
        thread_id, user_id="u-caller-a"
    )
    assert res["success"] is True
    # Model & budget resolved under caller A
    assert model_user_calls == ["u-caller-a"]
    # Session acquired under workspace owner B
    assert session_calls == [(ws_id, "u-owner-b")]
    # Direct MCP and graph store/memory scoped to workspace owner B, caller kept as A
    assert direct_calls == [
        {
            "user_id": "u-owner-b",
            "owner_user_id": "u-owner-b",
            "caller_user_id": "u-caller-a",
            "workspace_id": ws_id,
        }
    ]
    assert len(graph_kwargs_captured) == 1
    assert graph_kwargs_captured[0]["user_id"] == "u-caller-a"
    assert graph_kwargs_captured[0]["workspace_owner_id"] == "u-owner-b"
