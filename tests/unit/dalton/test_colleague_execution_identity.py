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
from ptc_agent.agent.backends import StoreBackend
from ptc_agent.agent.backends.sandbox import SandboxBackend
from ptc_agent.agent.middleware.tool.leak_detection import LeakDetectionMiddleware
from ptc_agent.core.paths import MEMO_USER_DIR
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
    memo_routes = [
        r
        for r in getattr(composite, "_routes", [])
        if isinstance(r, StoreBackend) and r.root_prefix == f"/home/daytona/{MEMO_USER_DIR}/"
    ]
    assert len(memo_routes) == 1
    assert memo_routes[0]._namespace_factory() == ("u-owner-b", "memos")
    assert memo_routes[0]._read_only is True
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
    # computer_id is an instance attribute set in Session.__init__ (upstream
    # #652's AgentInputs.read reads it); autospec only sees class attributes,
    # so it is set by hand like session.sandbox below.
    session.computer_id = "c-colleague-1"
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

    from src.server.services.shared_workspaces.turn import TurnIdentity

    inputs = await thread_agent.AgentInputs.read(
        session,
        identity=TurnIdentity(sender="u-caller-a", owner="u-owner-b", recorded_as="u-caller-a"),
        workspace_id="ws-b",
        thread_id="th-1",
        role="analyst",
        user_profile={"name": "Caller A"},
        workspace_owner_id="u-owner-b",
    )
    assert inputs.identity.sender == "u-caller-a"
    assert inputs.identity.owner == "u-owner-b"
    assert inputs.computer_id == "c-colleague-1"
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
    assert built_kwargs["owner_id"] == "u-owner-b"
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
    session.computer_id = "c-colleague-1"  # Session.__init__ 实例属性，autospec 看不见
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


@pytest.mark.asyncio
async def test_phase1_c3_cross_user_automations_backend_read_only_and_unit_file_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C3 Option 2: cross-user shared workspaces disable AutomationsBackend direct writes
    and direct AutomationFile commits refuse managed tasks by non-lead users."""
    from unittest.mock import create_autospec
    from ptc_agent.agent.backends.automations import AutomationsBackend
    from ptc_agent.agent.backends.db_json_route import UserDataValidationError
    from ptc_agent.agent.backends.langgraph_store import ReadOnlyStoreError
    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.agent.filesystem_routes import IdentityGates, build_filesystem_backend
    from src.dalton import settings as dalton_settings
    from src.dalton.database import coverage_units as cu_db
    from src.server.models.automation import AutomationCreate
    from src.server.services.automations import file as auto_file

    sb = create_autospec(SandboxBackend, instance=True)
    sb.computer_root = "/ws"
    sb.normalize_path.side_effect = lambda p: p

    # Cross-user colleague session: AutomationsBackend keeps owner _user_id but refuses writes
    composite, _baseline = build_filesystem_backend(
        backend=sb,
        gates=IdentityGates(
            user_memory=False,
            workspace_memory=False,
            memo=False,
            user_data=True,
            workflow=False,
            workflow_fs=False,
            workflow_tool=False,
            channels=False,
        ),
        store=object(),
        user_id="u-caller-a",
        workspace_owner_id="u-owner-b",
        workspace_id="11111111-1111-1111-1111-111111111111",
    )
    auto_route = next(
        r for r in getattr(composite, "_routes", []) if isinstance(r, AutomationsBackend)
    )
    assert auto_route._user_id == "u-owner-b"
    assert auto_route.is_writable("/ws/.agents/user/automations/dalton-nvda-watch.json") is False
    assert auto_route.is_writable("/ws/.agents/user/automations") is False

    with pytest.raises(UserDataValidationError, match="dalton_schedule_automation"):
        await auto_route.awrite_text(
            "/ws/.agents/user/automations/dalton-nvda-watch.json",
            "{}",
        )

    with pytest.raises(ReadOnlyStoreError, match="dalton_schedule_automation"):
        await auto_route.adelete_versioned("/ws/.agents/user/automations/dalton-nvda-watch.json")
    with pytest.raises(ReadOnlyStoreError, match="dalton_schedule_automation"):
        await auto_route.arename_versioned(
            "/ws/.agents/user/automations/a.json",
            "/ws/.agents/user/automations/b.json",
        )
    with pytest.raises(UserDataValidationError, match="dalton_schedule_automation"):
        await auto_route.awrite_versioned(
            "/ws/.agents/user/automations/dalton-nvda-watch.json", "{}", None
        )

    # AutomationFile.commit blocks direct file writes for managed unit tasks for ALL users (owner & lead)
    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(
        cu_db,
        "get_active_unit_by_workspace",
        create_autospec(
            cu_db.get_active_unit_by_workspace,
            return_value={
                "unit_id": "uuuuuuuu-uuuu-uuuu-uuuu-uuuuuuuuuuuu",
                "workspace_id": "11111111-1111-1111-1111-111111111111",
                "owner_user_id": "u-owner-b",
                "lead_analyst_user_id": "u-lead-c",
                "status": "active",
                "kind": "company",
                "ticker": "NVDA",
            },
        ),
    )
    f = auto_file.AutomationFile("dalton-nvda-watch.json")
    managed_plan = auto_file.FilePlan(
        create=auto_file._Create(
            data=AutomationCreate(
                name="dalton-nvda-watch",
                trigger_type="cron",
                cron_expression="0 9 * * *",
                instruction="watch",
                workspace_id="11111111-1111-1111-1111-111111111111",
                metadata={"dalton_kind": "company_watch", "ticker": "NVDA"},
            ),
            pause=False,
        )
    )
    for caller_uid in ("u-owner-b", "u-lead-c"):
        with pytest.raises(ReadOnlyStoreError, match="dalton_schedule_automation"):
            await f.commit(caller_uid, managed_plan, conn=object())

    # Item 13: personal dalton-* task without unit_id/ticker metadata is NOT blocked on create
    personal_plan = auto_file.FilePlan(
        create=auto_file._Create(
            data=AutomationCreate(
                name="dalton-nvda-watch",
                trigger_type="cron",
                cron_expression="0 9 * * *",
                instruction="personal note",
                workspace_id="11111111-1111-1111-1111-111111111111",
            ),
            pause=False,
        )
    )
    await f._check_plan_not_managed_unit(personal_plan, conn=object())


@pytest.mark.asyncio
async def test_round4_item7_file_prelock_runs_before_file_lock_in_save_delete_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Item 7: _save, adelete_versioned, arename_versioned acquire unit FOR SHARE BEFORE file.lock."""
    from contextlib import asynccontextmanager
    from unittest.mock import create_autospec

    from ptc_agent.agent.backends.automations import AutomationsBackend
    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.core.sandbox.livefs_mount import CallContext
    from src.dalton import settings as dalton_settings
    from src.dalton.database import coverage_units as cu_db
    from src.server.database import automation as auto_db
    from src.server.services.automations import file as auto_file

    ws_old = "11111111-1111-1111-1111-111111111111"
    ws_new = "22222222-2222-2222-2222-222222222222"
    events: list[tuple[str, str | None]] = []

    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)

    async def _fake_get_unit(ws_id, conn=None, for_share=False):
        events.append(("UNIT_FOR_SHARE" if for_share else "UNIT_READ", str(ws_id)))
        return None

    async def _fake_lock_user(uid, conn=None):
        events.append(("USER_LOCK", str(uid)))

    existing_row = {
        "automation_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "user_id": "owner-a",
        "name": "brief",
        "file_name": "brief.json",
        "trigger_type": "cron",
        "cron_expression": "0 9 * * *",
        "timezone": "UTC",
        "instruction": "hello",
        "workspace_id": ws_old,
        "thread_strategy": "new",
        "status": "active",
        "max_failures": 3,
        "delivery_config": {"methods": []},
        "metadata": {},
    }

    managed_row = {
        **existing_row,
        "name": "dalton-nvda-watch",
        "file_name": "dalton-nvda-watch.json",
        "metadata": {"dalton_kind": "company_watch", "ticker": "NVDA", "unit_id": "u-nvda"},
    }

    async def _fake_fetch(uid, fn, conn=None, lock=False):
        if fn == "dalton-nvda-watch.json":
            return dict(managed_row)
        return dict(existing_row) if fn == "brief.json" else None

    async def _fake_delete(aid, uid, **_kw):
        return True

    async def _fake_rename(aid, uid, to, **_kw):
        return True

    class _TxConn:
        @asynccontextmanager
        async def transaction(self):
            yield self

    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", _fake_get_unit)
    monkeypatch.setattr(auto_db, "lock_user_automations", _fake_lock_user)
    monkeypatch.setattr(auto_db, "get_automation_file", _fake_fetch)
    monkeypatch.setattr(auto_db, "delete_automation", _fake_delete)
    monkeypatch.setattr(auto_db, "rename_automation_file", _fake_rename)

    @asynccontextmanager
    async def _backend_tx():
        yield _TxConn()

    sb = create_autospec(SandboxBackend, instance=True)
    sb.normalize_path.side_effect = lambda p: p
    backend = AutomationsBackend(
        user_id="owner-a",
        call=CallContext(workspace_id=ws_old),
        sandbox_backend=sb,
        root_prefix="/ws/.agents/user/automations",
    )
    monkeypatch.setattr(backend, "_transaction", _backend_tx)
    monkeypatch.setattr(auto_file, "get_home_workspace_id", lambda _uid: "99999999-9999-9999-9999-999999999999")

    # Ordinary file skips unit FOR SHARE queries during delete/rename; unit-managed file locks unit BEFORE USER_LOCK
    await backend.adelete_versioned("/ws/.agents/user/automations/brief.json")
    assert events == [("USER_LOCK", "owner-a")]

    events.clear()
    await backend.adelete_versioned("/ws/.agents/user/automations/dalton-nvda-watch.json")
    assert events[:2] == [("UNIT_FOR_SHARE", ws_old), ("USER_LOCK", "owner-a")]

    events.clear()
    await backend.arename_versioned(
        "/ws/.agents/user/automations/dalton-nvda-watch.json",
        "/ws/.agents/user/automations/dalton-nvda-watch-renamed.json",
    )
    assert events[:2] == [("UNIT_FOR_SHARE", ws_old), ("USER_LOCK", "owner-a")]

    events.clear()
    # Save changing workspace_id on a unit-managed file locks BOTH units in sorted order BEFORE USER_LOCK
    import json

    rendered_text, _ = auto_file._render(managed_row)
    doc_payload = json.loads(rendered_text)
    doc_payload["workspace_id"] = ws_new
    # Force an early return after file.lock by returning existing version mismatch so test checks lock order cleanly
    with pytest.raises(Exception):
        await backend._save(
            "dalton-nvda-watch.json",
            json.dumps(doc_payload),
            "stale-version",
            served=rendered_text,
            may_delete=True,
        )
    assert events[:3] == [
        ("UNIT_FOR_SHARE", ws_old),
        ("UNIT_FOR_SHARE", ws_new),
        ("USER_LOCK", "owner-a"),
    ]


@pytest.mark.asyncio
async def test_round4_item8_item13_item14_execution_entry_and_preflight_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Items 8, 13, 14 on begin_execution_if_not_deleted and _conclude_preflight."""
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone
    from unittest.mock import create_autospec

    from src.dalton import settings as dalton_settings
    from src.dalton.autonomy import preflight as preflight_mod
    from src.dalton.database import watch as watch_db
    from src.server.database import automation_executions as exec_db

    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: True)

    # Item 14: ordinary non-managed task executes ONLY 1 SQL statement (no probe / no unit lock)
    ordinary_queries: list[str] = []

    class _OrdCur:
        async def execute(self, sql, params=None):
            ordinary_queries.append(" ".join(str(sql).split()))

        async def fetchone(self):
            return {
                "conversation_thread_id": None,
                "conversation_response_id": None,
                "scheduled_at": datetime.now(timezone.utc),
                "wait_reason": None,
                "awaits_computer": False,
                "checked_computer_id": None,
                "coverage_unit_id": None,
                "coverage_unit_version": None,
            }

    class _OrdConn:
        @asynccontextmanager
        async def transaction(self):
            yield self

        @asynccontextmanager
        async def cursor(self, **_kw):
            yield _OrdCur()

    @asynccontextmanager
    async def _ord_pool(c=None):
        yield _OrdConn()

    monkeypatch.setattr(exec_db, "get_db_connection", _ord_pool)
    ord_res = await exec_db.begin_execution_if_not_deleted(
        "exec-ordinary", started_at=datetime.now(timezone.utc)
    )
    assert ord_res is not None
    assert len(ordinary_queries) == 1
    assert "dalton_coverage_units" not in ordinary_queries[0]

    # Item 8a & 13: workspace_id drifts concurrently on first attempt -> retries & locks new unit;
    # also target CTE uses managed_unit_pred (no name-only fallback)
    attempt_state = {"probes": 0, "locked_ws": []}

    class _DriftCur:
        def __init__(self):
            self._r = None
            self._rows = []

        async def execute(self, sql, params=None):
            q = " ".join(str(sql).split())
            if "AND NOT (" in q and "UPDATE automation_executions e" in q:
                self._r = None
            elif "SELECT a.workspace_id" in q:
                attempt_state["probes"] += 1
                self._r = {
                    "workspace_id": "ws-old" if attempt_state["probes"] == 1 else "ws-new",
                    "is_managed_candidate": True,
                }
            elif "FROM dalton_coverage_units" in q and "FOR SHARE" in q:
                attempt_state["locked_ws"].append(params[0])
                self._rows = [{"unit_id": "u-new", "updated_at": datetime.now(timezone.utc)}]
                self._r = self._rows[0]
            elif "WITH target AS" in q:
                assert "(a.workspace_id IS DISTINCT FROM %s::uuid) AS ws_changed" in q
                if attempt_state["probes"] == 1:
                    self._r = {"ws_changed": True}
                else:
                    self._r = {
                        "conversation_thread_id": None,
                        "conversation_response_id": None,
                        "scheduled_at": datetime.now(timezone.utc),
                        "wait_reason": None,
                        "awaits_computer": False,
                        "checked_computer_id": None,
                        "coverage_unit_id": "u-new",
                        "coverage_unit_version": None,
                        "ws_changed": False,
                    }

        async def fetchone(self):
            return self._r

        async def fetchall(self):
            return self._rows

    class _DriftConn:
        @asynccontextmanager
        async def transaction(self):
            yield self

        @asynccontextmanager
        async def cursor(self, **_kw):
            yield _DriftCur()

    @asynccontextmanager
    async def _drift_pool(c=None):
        yield _DriftConn()

    monkeypatch.setattr(exec_db, "get_db_connection", _drift_pool)
    drift_res = await exec_db.begin_execution_if_not_deleted(
        "exec-drift", started_at=datetime.now(timezone.utc)
    )
    assert drift_res is not None
    assert attempt_state["locked_ws"] == ["ws-old", "ws-new"]
    assert drift_res["coverage_unit_id"] == "u-new"

    # Item 8b: preflight _conclude_preflight checks locked auto_row.workspace_id != workspace_id -> skips without claiming
    claim_mock = create_autospec(watch_db.claim_signals_woken, return_value=[])
    monkeypatch.setattr(watch_db, "claim_signals_woken", claim_mock)
    monkeypatch.setattr(
        watch_db, "list_unwoken_signals", create_autospec(watch_db.list_unwoken_signals, return_value=[])
    )
    monkeypatch.setattr(
        watch_db,
        "upsert_signal",
        create_autospec(watch_db.upsert_signal, return_value=({"signal_id": "sig-drift"}, True)),
    )
    monkeypatch.setattr(watch_db, "upsert_watch_state", lambda *_a, **_kw: _noop_async())

    async def _noop_async():
        return None

    class _PreDriftConn:
        @asynccontextmanager
        async def transaction(self):
            yield self

        @asynccontextmanager
        async def cursor(self, **_kw):
            class _PCur:
                async def execute(self_c, sql, _p=None):
                    s = " ".join(str(sql).split())
                    if "FROM dalton_coverage_units" in s and "FOR SHARE" in s:
                        self_c._r = None
                    elif "FROM automations" in s and "FOR SHARE" in s:
                        self_c._r = {
                            "automation_id": "11111111-1111-1111-1111-111111111111",
                            "user_id": "u-owner",
                            "workspace_id": "ws-moved",
                            "status": "executing",
                            "deleted_at": None,
                            "name": "dalton-nvda-move-up",
                            "metadata": {"dalton_kind": "price_move", "ticker": "NVDA"},
                        }

                async def fetchone(self_c):
                    return getattr(self_c, "_r", None)

            yield _PCur()

    @asynccontextmanager
    async def _pre_conn(c=None):
        yield _PreDriftConn()

    monkeypatch.setattr("src.server.database.pool.get_db_connection", _pre_conn)
    from src.dalton.database import coverage_units as cu_db

    monkeypatch.setattr(cu_db, "get_db_connection", _pre_conn)
    res_ws_drift = await preflight_mod._conclude_preflight(
        emitted=[preflight_mod.ProbeSignal(probe="sec", signal_key="k1", priority="P0", summary="s")],
        state={},
        persist=True,
        workspace_id="ws-orig",
        ticker="NVDA",
        automation_id="11111111-1111-1111-1111-111111111111",
        execution_id="exec-pre-drift",
        name="dalton-nvda-move-up",
        user_id="u-owner",
        agenda_doc_override={},
        watch_state_override=None,
        group_refresh_summary=None,
        load_saved_signals=False,
    )
    assert res_ws_drift.should_wake is False
    assert res_ws_drift.reason == "workspace_changed"
    claim_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_round4_item9_schedule_db_metadata_and_pm_target_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Item 9:
    1) Non-lead colleague omitting ticker cannot resume/modify a unit-managed automation carrying DB metadata.
    2) PM operating on bound unit workspace resolves target task owner to unit's lead_analyst_user_id.
    """
    from contextlib import asynccontextmanager
    from unittest.mock import create_autospec

    from src.dalton import settings as dalton_settings
    from src.dalton.autonomy import schedule as sched_mod
    from src.dalton.database import coverage_units as cu_db
    from src.server.database import automation as auto_db

    ws_id = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(
        sched_mod,
        "get_dalton_settings",
        lambda: type("S", (), {"pm_user_ids": ["pm-1"], "autonomy": type("A", (), {"timezone": "Asia/Shanghai"})()})(),
    )
    monkeypatch.setattr(
        "src.dalton.autonomy.system_jobs.is_pm_user",
        lambda uid: str(uid) == "pm-1",
    )
    monkeypatch.setattr(
        "src.dalton.access_tiers.classify_owner_tier",
        lambda _uid: _async_ret("internal"),
    )
    monkeypatch.setattr(
        sched_mod,
        "verify_workspace_access",
        create_autospec(
            sched_mod.verify_workspace_access,
            return_value={"workspace_id": ws_id, "user_id": "owner-b", "is_home": False},
        ),
    )
    monkeypatch.setattr(
        cu_db,
        "get_active_unit_by_workspace",
        create_autospec(
            cu_db.get_active_unit_by_workspace,
            return_value={
                "unit_id": "u-nvda",
                "workspace_id": ws_id,
                "owner_user_id": "owner-b",
                "lead_analyst_user_id": "lead-a",
                "status": "active",
                "kind": "company",
                "ticker": "NVDA",
            },
        ),
    )

    db_row = {
        "automation_id": "auto-nvda-1",
        "user_id": "colleague-c",
        "name": "dalton-nvda-watch",
        "workspace_id": ws_id,
        "status": "paused",
        "metadata": {"dalton_kind": "company_watch", "ticker": "NVDA"},
        "scope": "research_tracking",
    }
    lead_row = {**db_row, "user_id": "lead-a"}

    @asynccontextmanager
    async def _lock(_uid, conn=None):
        yield object()

    monkeypatch.setattr(sched_mod, "user_automations_lock", _lock)
    monkeypatch.setattr(
        sched_mod,
        "list_all_user_automations",
        create_autospec(
            sched_mod.list_all_user_automations,
            side_effect=lambda uid, **_kw: [lead_row] if uid == "lead-a" else [db_row],
        ),
    )

    # Non-lead colleague omitting ticker cannot resume DB-managed task
    denied = await sched_mod.schedule_dalton_automation_helper(
        "dalton-nvda-watch",
        {"action": "resume"},
        user_id="colleague-c",
        workspace_id=ws_id,
        role="chief_of_staff",
        action="resume",
    )
    assert denied["ok"] is False
    assert denied["error"] == "not_unit_lead_analyst"

    # PM omitting ticker resolves DB row under lead-a and updates as lead-a (actor = pm-1)
    updated_calls: list[tuple[str, str]] = []

    async def _fake_update(aid, uid, **_kw):
        updated_calls.append((str(aid), str(uid)))
        return {**lead_row, "status": "active"}

    monkeypatch.setattr(
        auto_db,
        "update_automation",
        create_autospec(auto_db.update_automation, side_effect=_fake_update),
    )
    res_pm = await sched_mod.schedule_dalton_automation_helper(
        "dalton-nvda-watch",
        {"action": "resume"},
        user_id="pm-1",
        workspace_id=ws_id,
        role="chief_of_staff",
        action="resume",
    )
    assert res_pm["ok"] is True
    assert updated_calls == [("auto-nvda-1", "lead-a")]


@pytest.mark.asyncio
async def test_round5_pm_resume_recomputes_next_run_and_refuses_concurrent_unbind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5 Item 6:
    1) PM resume writes recalculated next_run_at and strips stale handoff_paused_reason.
    2) PM resume when locked re-read finds unit is None (unbound concurrently) refuses without writing.
    """
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone
    from unittest.mock import create_autospec
    from src.dalton import access_tiers as access_tiers_mod
    from src.dalton import invites as invites_mod
    from src.dalton import settings as dalton_settings
    from src.dalton.autonomy import schedule as sched_mod
    from src.dalton.database import coverage_units as cu_db
    from src.server.database import automation as auto_db

    ws_id = "22222222-2222-2222-2222-222222222222"
    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(
        sched_mod,
        "get_dalton_settings",
        lambda: type("S", (), {"pm_user_ids": ["pm-1"], "autonomy": type("A", (), {"timezone": "Asia/Shanghai"})()})(),
    )
    monkeypatch.setattr("src.dalton.autonomy.system_jobs.is_pm_user", lambda uid: uid == "pm-1")
    monkeypatch.setattr(invites_mod, "is_invite_pm", lambda uid: uid == "pm-1")
    monkeypatch.setattr(
        access_tiers_mod,
        "classify_owner_tier",
        create_autospec(access_tiers_mod.classify_owner_tier, return_value="internal"),
    )

    unit_row = {
        "unit_id": "u-nvda",
        "workspace_id": ws_id,
        "owner_user_id": "owner-b",
        "lead_analyst_user_id": "lead-a",
        "status": "active",
        "kind": "company",
        "ticker": "NVDA",
    }
    lead_cron_row = {
        "automation_id": "auto-nvda-cron",
        "user_id": "lead-a",
        "name": "dalton-nvda-watch",
        "workspace_id": ws_id,
        "status": "paused",
        "trigger_type": "cron",
        "cron_expression": "0 9 * * *",
        "timezone": "UTC",
        "next_run_at": None,
        "metadata": {
            "dalton_kind": "company_watch",
            "ticker": "NVDA",
            "handoff_paused_reason": "unit_unbound",
        },
        "scope": "research_tracking",
    }
    monkeypatch.setattr(
        sched_mod,
        "verify_workspace_access",
        create_autospec(
            sched_mod.verify_workspace_access,
            return_value={"workspace_id": ws_id, "user_id": "owner-b"},
        ),
    )
    orig_get_active_unit = cu_db.get_active_unit_by_workspace
    monkeypatch.setattr(
        cu_db,
        "get_active_unit_by_workspace",
        create_autospec(orig_get_active_unit, return_value=unit_row),
    )
    monkeypatch.setattr(
        sched_mod,
        "list_all_user_automations",
        create_autospec(
            sched_mod.list_all_user_automations,
            side_effect=lambda uid, **_kw: [lead_cron_row] if uid == "lead-a" else [],
        ),
    )

    @asynccontextmanager
    async def _lock(_uid, conn=None):
        yield object()

    monkeypatch.setattr(sched_mod, "user_automations_lock", _lock)
    update_spy = create_autospec(
        auto_db.update_automation,
        side_effect=lambda aid, uid, **kw: {**lead_cron_row, **kw},
    )
    monkeypatch.setattr(auto_db, "update_automation", update_spy)

    res = await sched_mod.schedule_dalton_automation_helper(
        "dalton-nvda-watch",
        {"action": "resume"},
        user_id="pm-1",
        workspace_id=ws_id,
        role="chief_of_staff",
        action="resume",
    )
    assert res["ok"] is True
    assert update_spy.await_count == 1
    write_kw = update_spy.await_args.kwargs
    assert write_kw["status"] == "active"
    assert isinstance(write_kw["next_run_at"], datetime)
    assert write_kw["next_run_at"] > datetime.now(timezone.utc)
    assert write_kw["failure_count"] == 0
    assert write_kw["disable_reason"] is None
    assert "handoff_paused_reason" not in write_kw["metadata"]

    # Concurrent unbind between pre-lock probe and locked re-read returns unit_unbound and skips write
    update_spy.reset_mock()
    monkeypatch.setattr(
        cu_db,
        "get_active_unit_by_workspace",
        create_autospec(
            orig_get_active_unit,
            side_effect=[unit_row, None],
        ),
    )
    refused = await sched_mod.schedule_dalton_automation_helper(
        "dalton-nvda-watch",
        {"action": "resume"},
        user_id="pm-1",
        workspace_id=ws_id,
        role="chief_of_staff",
        action="resume",
    )
    assert refused["ok"] is False
    assert refused["error"] == "unit_unbound"
    update_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_round4_item10_report_authorize_blocks_post_handoff_and_group_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Item 10: authorize_saved_report_target and _authorize_failure_alert_row skip after handoff/group."""
    from unittest.mock import create_autospec

    from src.dalton import settings as dalton_settings
    from src.dalton.database import automation_reports as ar_db
    from src.dalton.notify import dispatch as notify_dispatch

    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)

    exec_id = "11111111-1111-1111-1111-111111111111"
    auto_id = "22222222-2222-2222-2222-222222222222"
    ws_id = "33333333-3333-3333-3333-333333333333"
    report_payload = {
        "source": "automation_report",
        "version": 1,
        "profile": "market-digest-am",
        "report_run_id": "run-1",
        "content_kind": "text",
        "content_sha256": "abc123",
        "text": "report body",
        "first_execution_id": exec_id,
        "owner_user_id": "old-lead",
        "automation_id": auto_id,
        "target_snapshot": ["feishu:user:ou_1"],
        "failure_target_snapshot": ["discord:user:123456789"],
        "expected_item_count": 1,
        "coverage_unit_id": "unit-1",
        "coverage_unit_version": "2026-11-20T12:00:00+00:00",
    }
    report_row = {
        "delivery_id": "d-1",
        "execution_id": exec_id,
        "automation_id": auto_id,
        "user_id": "old-lead",
        "channel": "feishu",
        "target": "feishu:user:ou_1",
        "status": "pending",
        "error_code": None,
        "attempt_count": 0,
        "payload_json": report_payload,
    }
    alert_row = {
        "delivery_id": "d-2",
        "execution_id": exec_id,
        "automation_id": auto_id,
        "user_id": "old-lead",
        "channel": "discord",
        "target": "discord:user:123456789",
        "status": "pending",
        "error_code": None,
        "attempt_count": 0,
        "payload_json": {
            "source": "automation_report_failure_alert",
            "version": 1,
            "profile": "market-digest-am",
            "content_kind": "failure_text",
            "content_sha256": "def456",
            "failure_text": "alert body",
            "first_execution_id": exec_id,
            "original_execution_id": exec_id,
            "original_content_sha256": "abc123",
            "original_report_run_id": "run-1",
            "report_run_id": "run-1",
            "owner_user_id": "old-lead",
            "automation_id": auto_id,
            "target_snapshot": ["discord:user:123456789"],
            "expected_item_count": 1,
        },
    }

    monkeypatch.setattr(
        ar_db,
        "_lock_after_reconcile_advisories",
        create_autospec(ar_db._lock_after_reconcile_advisories, return_value=[report_row]),
    )
    monkeypatch.setattr(
        notify_dispatch,
        "_resolve_unit_managed_notify_gate",
        create_autospec(
            notify_dispatch._resolve_unit_managed_notify_gate,
            return_value=(False, "not_unit_lead_analyst", "unit-1", False),
        ),
    )

    class _Cur:
        async def execute(self, sql, _p=None):
            s = " ".join(str(sql).split())
            if "FROM automations" in s:
                self._r = {
                    "automation_id": auto_id,
                    "user_id": "old-lead",
                    "workspace_id": ws_id,
                    "name": "dalton-nvda-watch",
                    "metadata": {
                        "dalton_kind": "company_watch",
                        "ticker": "NVDA",
                        "report_profile": {
                            "profile": "market-digest-am",
                            "report_targets": ["feishu:user:ou_1"],
                            "failure_targets": ["discord:user:123456789"],
                        },
                    },
                }

        async def fetchone(self):
            return getattr(self, "_r", None)

    res_report = await ar_db.authorize_saved_report_target(
        _Cur(),
        object(),
        execution_id=exec_id,
        target="feishu:user:ou_1",
    )
    assert res_report["allowed"] is False
    assert res_report["deny_status"] == "skipped"
    assert res_report["deny_code"] == "not_unit_lead_analyst"

    res_alert = await ar_db._authorize_failure_alert_row(
        _Cur(),
        object(),
        execution_id=exec_id,
        target="discord:user:123456789",
        row=alert_row,
        report_rows=[report_row],
    )
    assert res_alert["allowed"] is False
    assert res_alert["deny_status"] == "skipped"
    assert res_alert["deny_code"] == "not_unit_lead_analyst"


@pytest.mark.asyncio
async def test_round6_item1_item2_schedule_helper_pause_resume_lifecycle_and_replaced_by_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 6 Items 1 & 2: schedule helper reuses lifecycle pause/resume rules and refuses replaced_by_tracker."""
    from contextlib import asynccontextmanager
    from datetime import datetime, timedelta, timezone
    from unittest.mock import create_autospec

    from src.dalton import settings as dalton_settings
    from src.dalton.autonomy import schedule as sched_mod
    from src.dalton.database import coverage_units as cu_db
    from src.server.database import automation as auto_db

    ws_id = "22222222-2222-2222-2222-222222222222"
    future_once = datetime(2035, 6, 1, 9, 0, tzinfo=timezone.utc)
    past_once = datetime.now(timezone.utc) - timedelta(hours=2)

    rows_by_id: dict[str, dict[str, Any]] = {
        "auto-once-future": {
            "automation_id": "auto-once-future",
            "user_id": "owner-a",
            "name": "dalton-nvda-once",
            "workspace_id": ws_id,
            "status": "active",
            "trigger_type": "once",
            "cron_expression": None,
            "timezone": "UTC",
            "next_run_at": future_once,
            "metadata": {"dalton_kind": "earnings_t0", "handoff_paused_reason": "unit_unbound"},
        },
        "auto-once-expired": {
            "automation_id": "auto-once-expired",
            "user_id": "owner-a",
            "name": "dalton-nvda-once-expired",
            "workspace_id": ws_id,
            "status": "paused",
            "trigger_type": "once",
            "cron_expression": None,
            "timezone": "UTC",
            "next_run_at": past_once,
            "metadata": {"dalton_kind": "earnings_t0"},
        },
        "auto-once-none": {
            "automation_id": "auto-once-none",
            "user_id": "owner-a",
            "name": "dalton-nvda-once-none",
            "workspace_id": ws_id,
            "status": "paused",
            "trigger_type": "once",
            "cron_expression": None,
            "timezone": "UTC",
            "next_run_at": None,
            "metadata": {"dalton_kind": "earnings_t0"},
        },
        "auto-completed": {
            "automation_id": "auto-completed",
            "user_id": "owner-a",
            "name": "dalton-nvda-done",
            "workspace_id": ws_id,
            "status": "completed",
            "trigger_type": "once",
            "cron_expression": None,
            "timezone": "UTC",
            "next_run_at": past_once,
            "metadata": {"dalton_kind": "earnings_t0"},
        },
        "auto-replaced": {
            "automation_id": "auto-replaced",
            "user_id": "owner-a",
            "name": "dalton-nvda-delta",
            "workspace_id": ws_id,
            "status": "paused",
            "trigger_type": "cron",
            "cron_expression": "0 9 * * *",
            "timezone": "UTC",
            "next_run_at": None,
            "metadata": {"dalton_kind": "company_watch", "replaced_by_tracker": True},
        },
    }

    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: False)
    monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: False)
    monkeypatch.setattr(
        sched_mod,
        "verify_workspace_access",
        create_autospec(sched_mod.verify_workspace_access, return_value={"workspace_id": ws_id, "user_id": "owner-a"}),
    )
    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", create_autospec(cu_db.get_active_unit_by_workspace, return_value=None))
    monkeypatch.setattr(
        sched_mod,
        "list_all_user_automations",
        create_autospec(sched_mod.list_all_user_automations, side_effect=lambda _uid, **_kw: list(rows_by_id.values())),
    )

    @asynccontextmanager
    async def _lock(_uid, conn=None):
        yield object()

    monkeypatch.setattr(sched_mod, "user_automations_lock", _lock)

    async def _fake_update(aid, uid, **kw):
        from src.dalton.services.company_automations import get_current_history_context

        row = dict(rows_by_id[aid])
        eff = dict(kw)
        hist_ctx = get_current_history_context()
        if hist_ctx is not None and hist_ctx.locked_row_reconciler is not None:
            await hist_ctx.locked_row_reconciler(row, eff, kw.get("conn"))
        row.update({k: v for k, v in eff.items() if k in ("status", "next_run_at", "failure_count", "disable_reason", "metadata")})
        rows_by_id[aid] = row
        return row

    update_spy = create_autospec(auto_db.update_automation, side_effect=_fake_update)
    monkeypatch.setattr(auto_db, "update_automation", update_spy)
    from src.dalton.database import coverage as cov_db_mod
    from src.dalton.watchlist import policies as wl_policies_mod

    monkeypatch.setattr(
        cov_db_mod,
        "get_coverage_row",
        create_autospec(
            cov_db_mod.get_coverage_row,
            return_value={"ticker": "NVDA", "tier": "core", "is_private": False},
        ),
    )
    monkeypatch.setattr(
        wl_policies_mod,
        "get_user_tier_policy",
        create_autospec(
            wl_policies_mod.get_user_tier_policy,
            return_value=wl_policies_mod.DEFAULT_TIER_POLICIES["core"],
        ),
    )

    # 1. r5: Pause Dalton-managed once clears next_run_at while preserving once_schedule_at in metadata
    paused = await sched_mod.schedule_dalton_automation_helper(
        "dalton-nvda-once",
        {"action": "pause"},
        user_id="owner-a",
        workspace_id=ws_id,
        action="pause",
    )
    assert paused["ok"] is True
    assert update_spy.await_args.kwargs["status"] == "paused"
    assert update_spy.await_args.kwargs["next_run_at"] is None
    assert rows_by_id["auto-once-future"]["next_run_at"] is None
    assert rows_by_id["auto-once-future"]["metadata"]["once_schedule_at"] == future_once.isoformat()

    # 2. Resume paused once keeps original future_once unchanged and cleans handoff_paused_reason
    update_spy.reset_mock()
    resumed = await sched_mod.schedule_dalton_automation_helper(
        "dalton-nvda-once",
        {"action": "resume"},
        user_id="owner-a",
        workspace_id=ws_id,
        action="resume",
    )
    assert resumed["ok"] is True
    assert update_spy.await_args.kwargs["status"] == "active"
    assert update_spy.await_args.kwargs["next_run_at"] == future_once
    assert "handoff_paused_reason" not in update_spy.await_args.kwargs["metadata"]

    # 3. Resume expired once & next_run_at=None once refused without writing DB
    for name in ("dalton-nvda-once-expired", "dalton-nvda-once-none"):
        update_spy.reset_mock()
        err = await sched_mod.schedule_dalton_automation_helper(
            name,
            {"action": "resume"},
            user_id="owner-a",
            workspace_id=ws_id,
            action="resume",
        )
        assert err["ok"] is False
        assert err["error"]
        update_spy.assert_not_awaited()

    # 4. Resume completed refused via generic lifecycle path without writing DB
    update_spy.reset_mock()
    err_done = await sched_mod.schedule_dalton_automation_helper(
        "dalton-nvda-done",
        {"action": "resume"},
        user_id="owner-a",
        workspace_id=ws_id,
        action="resume",
    )
    assert err_done["ok"] is False
    update_spy.assert_not_awaited()

    # 5. Resume replaced_by_tracker refused with generic wording and without writing DB
    update_spy.reset_mock()
    err_rep = await sched_mod.schedule_dalton_automation_helper(
        "dalton-nvda-delta",
        {"action": "resume"},
        user_id="owner-a",
        workspace_id=ws_id,
        action="resume",
    )
    assert err_rep["ok"] is False
    assert "多空跟踪替代" in err_rep["error"]
    update_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_round7_item1_item2_window_execution_entry_and_notify_reject_stale_unit_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 7 Items 1 & 2: between Stage 1 commit and Stage 2 pause, execution entry and
    notify gate reject managed automations whose metadata.unit_id points to an unbound/archived/rebound unit."""
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone
    from unittest.mock import create_autospec

    from src.dalton import settings as dalton_settings
    from src.dalton.database import coverage_units as cu_db
    from src.dalton.notify import dispatch as dispatch_mod
    from src.server.database import automation_executions as exec_db

    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: True)

    unit_id = "11111111-1111-4111-8111-111111111111"
    ws_id = "22222222-2222-4222-8222-222222222222"
    queries: list[tuple[str, Any]] = []

    class _WindowCur:
        def __init__(self):
            self._r = None
            self._rows = []

        async def execute(self, sql, params=None):
            q = " ".join(str(sql).split())
            queries.append((q, params))
            if "AND NOT (" in q and "UPDATE automation_executions e" in q:
                self._r = None
            elif "SELECT a.workspace_id" in q:
                self._r = {
                    "workspace_id": ws_id,
                    "meta_unit_id": unit_id,
                    "is_managed_candidate": True,
                }
            elif "FROM dalton_coverage_units" in q and "FOR SHARE" in q:
                self._rows = [{"unit_id": unit_id, "updated_at": datetime.now(timezone.utc)}]
                self._r = self._rows[0]
            elif "WITH target AS" in q:
                assert "u_ref.unit_id::text" in q
                assert "u_ref.workspace_id IS DISTINCT FROM a.workspace_id" in q
                self._r = None

        async def fetchone(self):
            return self._r

        async def fetchall(self):
            return self._rows

    class _WindowConn:
        @asynccontextmanager
        async def transaction(self):
            yield self

        @asynccontextmanager
        async def cursor(self, **_kw):
            yield _WindowCur()

    @asynccontextmanager
    async def _window_pool(c=None):
        yield _WindowConn()

    monkeypatch.setattr(exec_db, "get_db_connection", _window_pool)
    res = await exec_db.begin_execution_if_not_deleted(
        "exec-window-1", started_at=datetime.now(timezone.utc)
    )
    assert res is None
    assert any("FOR SHARE" in q and p == (ws_id, unit_id) for q, p in queries)

    # Notify gate without stamped_unit_id also blocks Stage 1 -> Stage 2 window via metadata.unit_id
    monkeypatch.setattr(
        cu_db,
        "get_active_unit_by_workspace",
        create_autospec(cu_db.get_active_unit_by_workspace, return_value=None),
    )
    monkeypatch.setattr(
        cu_db,
        "get_coverage_unit",
        create_autospec(
            cu_db.get_coverage_unit,
            return_value={"unit_id": unit_id, "workspace_id": None, "status": "active"},
        ),
    )
    gate_ok, gate_reason, _, _ = await dispatch_mod._resolve_unit_managed_notify_gate(
        "lead-a",
        ws_id,
        {
            "automation_id": "auto-1",
            "user_id": "lead-a",
            "name": "dalton-nvda-watch",
            "workspace_id": ws_id,
            "metadata": {"dalton_kind": "company_watch", "ticker": "NVDA", "unit_id": unit_id},
        },
    )
    assert (gate_ok, gate_reason) == (False, "unit_unbound_or_archived")


@pytest.mark.asyncio
async def test_round7_item3_item4_upsert_blocks_replaced_delta_and_schedule_rechecks_under_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 7 Items 3 & 4: upsert refuses replaced_by_tracker deltas identically to resume,
    and pause/resume re-verify locked_row via apply_locked_{pause,resume}_schedule."""
    from contextlib import asynccontextmanager
    from datetime import datetime, timedelta, timezone
    from unittest.mock import create_autospec

    from src.dalton import settings as dalton_settings
    from src.dalton.app import router as dalton_router
    from src.dalton.autonomy import schedule as sched_mod
    from src.dalton.database import coverage_units as cu_db
    from src.dalton.services.company_automations import get_current_history_context
    from src.server.database import automation as auto_db

    ws_id = "22222222-2222-2222-2222-222222222222"
    future_once = datetime(2035, 6, 1, 9, 0, tzinfo=timezone.utc)
    past_once = datetime.now(timezone.utc) - timedelta(minutes=5)

    rows_by_name: dict[str, dict[str, Any]] = {
        "dalton-nvda-delta": {
            "automation_id": "auto-replaced",
            "user_id": "owner-a",
            "name": "dalton-nvda-delta",
            "workspace_id": ws_id,
            "status": "paused",
            "trigger_type": "cron",
            "cron_expression": "0 9 * * *",
            "timezone": "UTC",
            "next_run_at": None,
            "metadata": {"dalton_kind": "weekly_delta", "ticker": "NVDA", "replaced_by_tracker": "tracker-1"},
        },
        "dalton-nvda-earn-q1-t0": {
            "automation_id": "auto-once",
            "user_id": "owner-a",
            "name": "dalton-nvda-earn-q1-t0",
            "workspace_id": ws_id,
            "status": "active",
            "trigger_type": "once",
            "cron_expression": None,
            "timezone": "UTC",
            "next_run_at": future_once,
            "metadata": {"dalton_kind": "earnings_t0", "ticker": "NVDA"},
        },
    }

    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: False)
    monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: False)
    monkeypatch.setattr(
        sched_mod,
        "verify_workspace_access",
        create_autospec(sched_mod.verify_workspace_access, return_value={"workspace_id": ws_id, "user_id": "owner-a"}),
    )
    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", create_autospec(cu_db.get_active_unit_by_workspace, return_value=None))
    monkeypatch.setattr(
        sched_mod,
        "list_all_user_automations",
        create_autospec(sched_mod.list_all_user_automations, side_effect=lambda _uid, **_kw: list(rows_by_name.values())),
    )

    @asynccontextmanager
    async def _lock(_uid, conn=None):
        yield object()

    monkeypatch.setattr(sched_mod, "user_automations_lock", _lock)

    locked_override: dict[str, Any] | None = None

    async def _fake_update(aid, uid, conn=None, **kw):
        target = dict(locked_override) if locked_override is not None else dict(next(r for r in rows_by_name.values() if r["automation_id"] == aid))
        ctx = get_current_history_context()
        eff = dict(kw)
        if ctx is not None and ctx.locked_row_reconciler is not None:
            await ctx.locked_row_reconciler(target, eff, conn)
        target.update(eff)
        return target

    update_spy = create_autospec(auto_db.update_automation, side_effect=_fake_update)
    monkeypatch.setattr(auto_db, "update_automation", update_spy)
    from src.dalton.database import coverage as cov_db_mod
    from src.dalton.watchlist import policies as wl_policies_mod

    monkeypatch.setattr(
        cov_db_mod,
        "get_coverage_row",
        create_autospec(
            cov_db_mod.get_coverage_row,
            return_value={"ticker": "NVDA", "tier": "core", "is_private": False},
        ),
    )
    monkeypatch.setattr(
        wl_policies_mod,
        "get_user_tier_policy",
        create_autospec(
            wl_policies_mod.get_user_tier_policy,
            return_value=wl_policies_mod.DEFAULT_TIER_POLICIES["core"],
        ),
    )

    # Item 3: upsert (schedule helper & REST PUT) on replaced_by_tracker delta returns resume refusal without writing
    upsert_err = await sched_mod.schedule_dalton_automation_helper(
        "dalton-nvda-delta",
        {"status": "active", "cron_expression": "0 10 * * 1-5", "instruction": "revive"},
        user_id="owner-a",
        workspace_id=ws_id,
        action="upsert",
    )
    assert upsert_err["ok"] is False
    assert "多空跟踪替代" in upsert_err["error"]
    update_spy.assert_not_awaited()

    monkeypatch.setattr(
        dalton_router,
        "_verify_workspace_access",
        create_autospec(dalton_router._verify_workspace_access, return_value={"workspace_id": ws_id, "user_id": "owner-a"}),
    )
    monkeypatch.setattr(dalton_settings, "is_dalton_enabled", lambda: True)
    rest_err = await dalton_router.put_dalton_automation_endpoint(
        name="dalton-nvda-delta",
        body=dalton_router.DaltonAutomationWriteRequest(
            workspace_id=ws_id,
            spec={"status": "active", "cron_expression": "0 10 * * 1-5"},
        ),
        user_id="owner-a",
    )
    assert rest_err["ok"] is False
    assert rest_err["error"] == upsert_err["error"]
    update_spy.assert_not_awaited()

    # Item 4a: pre-read sees active, but locked_row became completed -> locked reconciler refuses pause
    locked_override = {**rows_by_name["dalton-nvda-earn-q1-t0"], "status": "completed", "next_run_at": None}
    pause_raced = await sched_mod.schedule_dalton_automation_helper(
        "dalton-nvda-earn-q1-t0",
        {"action": "pause"},
        user_id="owner-a",
        workspace_id=ws_id,
        action="pause",
    )
    assert pause_raced["ok"] is False
    assert "completed" in pause_raced["error"]

    # Item 4b: pre-read sees paused with future once, but locked_row time expired while waiting -> resume refuses
    rows_by_name["dalton-nvda-earn-q1-t0"]["status"] = "paused"
    locked_override = {**rows_by_name["dalton-nvda-earn-q1-t0"], "next_run_at": past_once}
    resume_raced = await sched_mod.schedule_dalton_automation_helper(
        "dalton-nvda-earn-q1-t0",
        {"action": "resume"},
        user_id="owner-a",
        workspace_id=ws_id,
        action="resume",
    )
    assert resume_raced["ok"] is False
    assert "passed" in resume_raced["error"]


@pytest.mark.asyncio
async def test_round9_item3_kept_guards_and_replaced_by_tracker_protection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 9 Item 3 kept protections (without two-phase stage cleanup window):
    - new company watch specs stamp unit_id
    - create_execution_for_live_parent acquires FOR SHARE on unit lookups and refuses mismatched/archived unit_id
    - AutomationFile / lifecycle / schedule refuse non-lead or direct edits on active unit automations
    - schedule upsert & PUT protect replaced_by_tracker (add/mutate/None preservation) via lifecycle helper
    """
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone
    from unittest.mock import create_autospec

    from fastapi import HTTPException
    from ptc_agent.agent.backends.db_json_route import ReadOnlyStoreError
    from src.dalton import settings as dalton_settings
    from src.dalton.app import router as dalton_router
    from src.dalton.autonomy import schedule as sched_mod
    from src.dalton.coverage import units as units_mod
    from src.dalton.database import coverage_units as cu_db
    from src.server.database import automation as auto_db
    from src.server.database import automation_executions as exec_db
    from src.server.services.automations import file as auto_file

    ws_old = "11111111-1111-4111-8111-111111111111"
    unit_id = "22222222-2222-4222-8222-222222222222"

    # 1. build_company_watch_specs stamps unit_id on all 4 standard specs when provided
    stamped_specs = sched_mod.build_company_watch_specs(
        ticker="NVDA", workspace_id=ws_old, unit_id=unit_id
    )
    assert len(stamped_specs) == 4
    assert all(sp["metadata"].get("unit_id") == unit_id for sp in stamped_specs)

    # 3. Active bound unit guards (file write, lifecycle write, create_execution_for_live_parent FOR SHARE)
    archived_unit = {
        "unit_id": unit_id,
        "kind": "company",
        "owner_user_id": "lead-a",
        "lead_analyst_user_id": "lead-a",
        "workspace_id": None,
        "ticker": "NVDA",
        "ticker_key": "NVDA",
        "status": "archived",
    }
    active_unit = {
        **archived_unit,
        "workspace_id": ws_old,
        "status": "active",
    }
    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(
        cu_db,
        "get_active_unit_by_workspace",
        create_autospec(cu_db.get_active_unit_by_workspace, return_value=active_unit),
    )
    monkeypatch.setattr(
        cu_db,
        "get_coverage_unit",
        create_autospec(cu_db.get_coverage_unit, return_value=archived_unit),
    )

    f = auto_file.AutomationFile("dalton-nvda-move-up.json")
    with pytest.raises(ReadOnlyStoreError, match="managed coverage unit"):
        await f._assert_not_managed_unit_file_edit(
            workspace_id=ws_old,
            name="dalton-nvda-move-up",
            metadata={"dalton_kind": "price_move", "ticker": "NVDA"},
            conn=object(),
        )

    with pytest.raises(HTTPException) as exc_lc:
        await units_mod.lock_and_assert_managed_unit_write_allowed(
            owner_user_id="non-lead",
            workspace_id=ws_old,
            name="dalton-nvda-move-up",
            metadata={"dalton_kind": "price_move", "ticker": "NVDA"},
            conn=object(),
        )
    assert exc_lc.value.status_code == 403

    # create_execution_for_live_parent acquires FOR SHARE on unit lookups and refuses stale/archived meta_unit_id
    stamped_row = {
        "automation_id": "auto-stamped-nvda",
        "user_id": "lead-a",
        "name": "dalton-nvda-move-up",
        "trigger_type": "price",
        "workspace_id": ws_old,
        "status": "active",
        "metadata": {"dalton_kind": "price_move", "ticker": "NVDA", "unit_id": unit_id},
        "scope": "research_tracking",
    }
    cu_db.get_active_unit_by_workspace.return_value = None

    class _LiveCur:
        def __init__(self):
            self._r = None

        async def execute(self, sql, params=None):
            q = " ".join(str(sql).split())
            if "SELECT workspace_id," in q:
                self._r = {"workspace_id": ws_old, "meta_unit_id": unit_id}
            elif "FOR UPDATE" in q:
                self._r = {**stamped_row, "deleted_at": None}

        async def fetchone(self):
            return self._r

    class _LiveConn:
        @asynccontextmanager
        async def transaction(self):
            yield self

        @asynccontextmanager
        async def cursor(self, **_kw):
            yield _LiveCur()

    @asynccontextmanager
    async def _live_pool(_c=None):
        yield _LiveConn()

    monkeypatch.setattr(exec_db, "get_db_connection", _live_pool)
    cu_db.get_coverage_unit.reset_mock()
    with pytest.raises(HTTPException) as exc_live:
        await exec_db.create_execution_for_live_parent(
            "auto-stamped-nvda",
            "lead-a",
            datetime.now(timezone.utc),
            "srv-1",
        )
    assert exc_live.value.status_code == 403
    cu_db.get_coverage_unit.assert_awaited()
    assert cu_db.get_coverage_unit.await_args.kwargs.get("for_share") is True

    # 4. schedule upsert & REST PUT protect replaced_by_tracker (add, mutate, and preserve None)
    @asynccontextmanager
    async def _ordered_lock(_uid, conn=None):
        yield object()

    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: False)
    monkeypatch.setattr(
        sched_mod,
        "verify_workspace_access",
        create_autospec(
            sched_mod.verify_workspace_access,
            return_value={"workspace_id": ws_old, "user_id": "owner-a", "is_home": False},
        ),
    )
    monkeypatch.setattr(
        "src.server.database.home_workspace.get_home_workspace_id",
        lambda _uid: "99999999-9999-4999-8999-999999999999",
    )
    from src.dalton.database import company_automations as comp_auto_db

    monkeypatch.setattr(
        comp_auto_db,
        "resolve_automation_scope_for_write",
        create_autospec(comp_auto_db.resolve_automation_scope_for_write, return_value="research_tracking"),
    )
    monkeypatch.setattr(sched_mod, "user_automations_lock", _ordered_lock)
    plain_row = {
        "automation_id": "auto-plain-1",
        "user_id": "owner-a",
        "name": "dalton-custom-delta",
        "workspace_id": ws_old,
        "status": "active",
        "trigger_type": "cron",
        "cron_expression": "0 9 * * *",
        "timezone": "UTC",
        "instruction": "v1",
        "metadata": {"note": "v1"},
        "scope": "personal",
    }
    null_marked_row = {
        **plain_row,
        "automation_id": "auto-null-rbt",
        "name": "dalton-custom-null-rbt",
        "metadata": {"note": "v1", "replaced_by_tracker": None},
    }
    written_metas: list[Any] = []

    async def _cap_update(aid, uid, **kw):
        written_metas.append(kw.get("metadata"))
        return {**null_marked_row, "metadata": kw.get("metadata") or null_marked_row["metadata"]}

    monkeypatch.setattr(
        sched_mod,
        "list_all_user_automations",
        create_autospec(sched_mod.list_all_user_automations, return_value=[plain_row, null_marked_row]),
    )
    monkeypatch.setattr(
        auto_db,
        "update_automation",
        create_autospec(auto_db.update_automation, side_effect=_cap_update),
    )
    create_spy = create_autospec(auto_db.create_automation, return_value={"automation_id": "new-1"})
    monkeypatch.setattr(auto_db, "create_automation", create_spy)

    # 4a. Adding replaced_by_tracker on create or ordinary update refused (helper & REST 403)
    res_add_create = await sched_mod.schedule_dalton_automation_helper(
        "dalton-new-task",
        {"cron": "0 9 * * *", "instruction": "hi", "metadata": {"replaced_by_tracker": "t1"}},
        user_id="owner-a",
        workspace_id=ws_old,
        action="upsert",
    )
    assert res_add_create["ok"] is False and res_add_create["error"] == "forbidden_replaced_by_tracker"
    res_add_update = await sched_mod.schedule_dalton_automation_helper(
        "dalton-custom-delta",
        {"instruction": "v2", "metadata": {"replaced_by_tracker": None}},
        user_id="owner-a",
        workspace_id=ws_old,
        action="upsert",
    )
    assert res_add_update["ok"] is False and res_add_update["error"] == "forbidden_replaced_by_tracker"

    # 4b. Updating existing row carrying replaced_by_tracker=None preserves the key with None value
    res_ok_none = await sched_mod.schedule_dalton_automation_helper(
        "dalton-custom-null-rbt",
        {"instruction": "v2", "metadata": {"note": "v2"}},
        user_id="owner-a",
        workspace_id=ws_old,
        action="upsert",
    )
    assert res_ok_none["ok"] is True
    assert written_metas[-1]["note"] == "v2"
    assert "replaced_by_tracker" in written_metas[-1] and written_metas[-1]["replaced_by_tracker"] is None

    # 4c. Mutating replaced_by_tracker=None -> "t1" refused via REST PUT with 403
    monkeypatch.setattr(
        dalton_router,
        "_verify_workspace_access",
        create_autospec(
            dalton_router._verify_workspace_access,
            return_value={"workspace_id": ws_old, "user_id": "owner-a"},
        ),
    )
    monkeypatch.setattr(dalton_settings, "is_dalton_enabled", lambda: True)
    with pytest.raises(HTTPException) as exc_put_rbt:
        await dalton_router.put_dalton_automation_endpoint(
            name="dalton-custom-null-rbt",
            body=dalton_router.DaltonAutomationWriteRequest(
                workspace_id=ws_old,
                spec={"instruction": "v3", "metadata": {"replaced_by_tracker": "t1"}},
            ),
            user_id="owner-a",
        )
    assert exc_put_rbt.value.status_code == 403
    create_spy.assert_not_awaited()


async def _async_ret(val):
    return val
