"""Real PostgreSQL integration tests for Bundle 7 (run by lead outside sandbox):
- E2 + Bound Home: Concurrent web workspace creation under real pg_advisory_xact_lock
  with bound Home rows present (no false conflict, ordinary create succeeds first try).
- E2 Alias + Same Ticker: Two concurrent alias requests with same ticker commit company
  identity (dalton_coverage_units) in the same transaction as workspace insert so locked
  recheck catches duplicates deterministically even when LLM returns empty matches.
- BD4: Real dalton_knowledge_versions company doc ticker read inside locked recheck.
- O1/Q1: Real list_executions stripping hidden colleague links AND preserving initiator's
  readable thread link when colleague workspace is soft-deleted with colleague dispatch on.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timezone
import hashlib
import importlib
import json
import pkgutil
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec, patch
import uuid

from fastapi import Request
from httpx import ASGITransport, AsyncClient
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import pytest
import pytest_asyncio

from src.dalton import access_tiers as access_tiers_mod
from src.dalton import settings as dalton_settings_mod
from src.dalton import sharing as sharing_mod
from src.dalton.coverage import dedup as dedup_mod
from src.dalton.coverage import locked_recheck as locked_recheck_mod
from src.dalton.coverage import units as units_mod
from src.dalton.coverage import workspace_create as ws_create_mod
from src.dalton.coverage.dedup import DedupLlmResult, DedupMatchItem
from src.dalton.database import coverage_units as cu_db
from src.dalton.services import execution_history_access as history_access_mod
import src.server.database as _database_pkg
from src.server.database import automation_executions as exec_db
from src.server.database import computer as computer_db
from src.server.database import conversation as conv_db
from src.server.database import pool as pool_mod
from src.server.database import user as user_db
from src.server.database import workspace as ws_db
from src.server.database import workspace_folders as ws_folders_db
from src.server.database.computer import create_computer
from src.server.database.session_lock import close_shared_lock_session
from src.server.database.user import create_user
from src.server.services.workspace_manager import WorkspaceManager
from tests.conftest import create_test_app

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_REAL_IS_COV_ENABLED = dedup_mod.is_coverage_units_enabled
_REAL_IS_SUPA_AUTH = dedup_mod.is_dalton_supabase_auth_enabled
_REAL_IS_LOCAL_AUTH = dedup_mod.dalton_settings_mod.is_dalton_local_auth_enabled
_REAL_IS_COLLEAGUE_DISPATCH = sharing_mod.is_colleague_dispatch_enabled
_REAL_SHARING_AUTH_ENABLED = sharing_mod._sharing_auth_enabled
_REAL_MULTIUSER_AUTH_ENABLED = sharing_mod._multiuser_auth_enabled
_REAL_ALLOWED_EMAILS = sharing_mod.get_dalton_allowed_emails
_REAL_IS_INVITE_PM = sharing_mod.is_invite_pm
_REAL_IS_COMPANY_USER = sharing_mod.is_company_system_user
_REAL_GET_DALTON_SETTINGS = dalton_settings_mod.get_dalton_settings


def _enable_everflow_env(
    monkeypatch: pytest.MonkeyPatch,
    allowed_emails: tuple[str, ...],
) -> None:
    import src.config.settings as app_settings
    import src.dalton.settings as dalton_settings_mod

    monkeypatch.setenv("DALTON_PROFILE", "everflow")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "true")
    monkeypatch.setenv("DALTON_ALLOWED_EMAILS", ",".join(allowed_emails))
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setattr(dalton_settings_mod, "HOST_MODE", "oss")


@pytest_asyncio.fixture
async def bundle7_live_pool(
    dalton_schema,
    cleanup_dalton_tables,
    test_db_uri: str,
):
    """Open an AsyncConnectionPool on the test's live event loop using the
    exact Dalton integration test fixture and pool injection pattern."""
    del dalton_schema, cleanup_dalton_tables
    for _info in pkgutil.walk_packages(
        _database_pkg.__path__, prefix="src.server.database."
    ):
        importlib.import_module(_info.name)

    local_pool = AsyncConnectionPool(
        conninfo=test_db_uri,
        min_size=2,
        max_size=8,
        configure=pool_mod._configure_postgres_connection,
        check=AsyncConnectionPool.check_connection,
        kwargs=dict(pool_mod._CONNECTION_KWARGS),
        open=False,
    )
    await local_pool.open()
    await local_pool.wait()

    @asynccontextmanager
    async def _live_get_db_connection(conn=None):
        if conn is not None:
            yield conn
            return
        async with local_pool.connection(timeout=10) as owned:
            yield owned

    async def _live_session_connection():
        conn = await psycopg.AsyncConnection.connect(
            test_db_uri, **pool_mod._CONNECTION_KWARGS
        )
        await pool_mod._configure_postgres_connection(conn)
        return conn

    orig_gdc = pool_mod.get_db_connection
    holders = {
        pool_mod,
        computer_db,
        user_db,
        ws_db,
        ws_folders_db,
        exec_db,
        conv_db,
        dedup_mod,
        locked_recheck_mod,
        ws_create_mod,
        units_mod,
        cu_db,
        history_access_mod,
        *(
            module
            for name, module in list(sys.modules.items())
            if name.startswith(("src.", "ptc_agent."))
            and getattr(module, "get_db_connection", None) is orig_gdc
        ),
    }

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                pool_mod,
                "get_db_connection_string",
                create_autospec(pool_mod.get_db_connection_string, return_value=test_db_uri),
            )
        )
        stack.enter_context(
            patch.object(
                pool_mod,
                "get_or_create_pool",
                create_autospec(pool_mod.get_or_create_pool, return_value=local_pool),
            )
        )
        stack.enter_context(
            patch.object(
                pool_mod,
                "open_session_connection",
                create_autospec(
                    pool_mod.open_session_connection, side_effect=_live_session_connection
                ),
            )
        )
        gdc_spec = create_autospec(orig_gdc, side_effect=_live_get_db_connection)
        for module in holders:
            if hasattr(module, "get_db_connection"):
                stack.enter_context(
                    patch.object(module, "get_db_connection", gdc_spec)
                )
        await close_shared_lock_session()
        try:
            yield local_pool
        finally:
            try:
                await close_shared_lock_session()
                async with local_pool.connection() as cleanup_conn:
                    async with cleanup_conn.cursor() as cur:
                        await cur.execute(
                            """
                            DELETE FROM automation_executions
                            WHERE automation_id IN (
                                SELECT automation_id FROM automations WHERE user_id LIKE 'b7-%'
                            )
                            """
                        )
                        await cur.execute(
                            "DELETE FROM automations WHERE user_id LIKE 'b7-%'"
                        )
                        await cur.execute(
                            """
                            DELETE FROM conversation_threads
                            WHERE user_id LIKE 'b7-%'
                               OR workspace_id IN (
                                   SELECT workspace_id FROM workspaces WHERE user_id LIKE 'b7-%'
                               )
                            """
                        )
                        await cur.execute(
                            """
                            DELETE FROM dalton_knowledge_versions
                            WHERE user_id LIKE 'b7-%'
                               OR workspace_id IN (
                                   SELECT workspace_id FROM workspaces WHERE user_id LIKE 'b7-%'
                               )
                            """
                        )
                        await cur.execute(
                            """
                            DELETE FROM dalton_workspace_sharing
                            WHERE workspace_id IN (
                                SELECT workspace_id FROM workspaces WHERE user_id LIKE 'b7-%'
                            )
                            """
                        )
                        await cur.execute(
                            """
                            DELETE FROM dalton_coverage_units
                            WHERE owner_user_id LIKE 'b7-%'
                               OR workspace_id IN (
                                   SELECT workspace_id FROM workspaces WHERE user_id LIKE 'b7-%'
                               )
                            """
                        )
                        await cur.execute(
                            "DELETE FROM dalton_coverage WHERE user_id LIKE 'b7-%'"
                        )
                        await cur.execute(
                            "DELETE FROM workspaces WHERE user_id LIKE 'b7-%'"
                        )
                        await cur.execute(
                            "DELETE FROM computers WHERE user_id LIKE 'b7-%'"
                        )
                        await cur.execute(
                            "DELETE FROM users WHERE user_id LIKE 'b7-%'"
                        )
            finally:
                await local_pool.close()


def _wrap_collect_barrier(orig_collect: Any, barrier_limit: int = 2) -> Any:
    barrier_count = 0
    both_entered = asyncio.Event()

    async def _synced_collect(
        uid: str,
        raw_name: str,
        *,
        caller_tier: str,
        include_colleagues: bool | None = None,
        conn: Any = None,
        stats: dict[str, int] | None = None,
    ):
        nonlocal barrier_count
        res = await orig_collect(
            uid,
            raw_name,
            caller_tier=caller_tier,
            include_colleagues=include_colleagues,
            conn=conn,
            stats=stats,
        )
        barrier_count += 1
        if barrier_count <= barrier_limit:
            if barrier_count == barrier_limit:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=5.0)
        return res

    return create_autospec(orig_collect, side_effect=_synced_collect)


def _make_llm_stub(match_tokens: tuple[str, ...] = ()) -> Any:
    async def _fake_complete(*, user_prompt: str, **_unused: Any):
        matches = []
        if match_tokens:
            for line in user_prompt.splitlines():
                if line.startswith("c") and any(tok in line for tok in match_tokens):
                    cid = line.split("|", 1)[0].strip()
                    matches.append(
                        DedupMatchItem(id=cid, confidence="high", reason="same company")
                    )
        return SimpleNamespace(ok=True, data=DedupLlmResult(matches=matches))

    return create_autospec(dedup_mod.dalton_complete, side_effect=_fake_complete)


async def _bind_home_workspace(
    pool: AsyncConnectionPool,
    user_id: str,
    computer_id: str,
) -> str:
    """Create and bind a user's Home workspace to their computer (status='stopped')."""
    home = await ws_db.get_or_create_home_workspace(user_id)
    home_id = str(home["workspace_id"])
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE workspaces
                SET computer_id = %s::uuid, status = 'stopped', dir_name = 'Home'
                WHERE workspace_id = %s::uuid
                """,
                (computer_id, home_id),
            )
    return home_id


async def test_e2_web_concurrent_create_real_postgres_with_bound_home_creates_only_one(
    bundle7_live_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2 + Item 1 (real PG): With bound Home workspaces for both caller and
    teammate present, ordinary web create succeeds first try, and two concurrent
    creates for '700.HK' and '0700.HK' serialize under pg_advisory_xact_lock so
    only one workspace is created and the second returns 409 company_workspace_exists."""
    from src.server.app.workspaces import router as ws_router
    from src.server.dependencies.principal import Principal, get_principal

    uid = f"b7-e2a-{uuid.uuid4().hex[:8]}"
    uid_col = f"b7-e2b-{uuid.uuid4().hex[:8]}"
    email = f"{uid}@team.local"
    email_col = f"{uid_col}@team.local"
    await create_user(user_id=uid, email=email, name="E2 User")
    await create_user(user_id=uid_col, email=email_col, name="E2 Colleague")
    comp = await create_computer(
        uid,
        kind="docker",
        name="E2 Box",
        is_primary=True,
        status="stopped",
        provider_ref=f"b7-e2-{uuid.uuid4()}",
    )
    comp_col = await create_computer(
        uid_col,
        kind="docker",
        name="Col Box",
        is_primary=True,
        status="stopped",
        provider_ref=f"b7-e2c-{uuid.uuid4()}",
    )
    comp_id = str(comp["computer_id"])
    await _bind_home_workspace(bundle7_live_pool, uid, comp_id)
    await _bind_home_workspace(bundle7_live_pool, uid_col, str(comp_col["computer_id"]))

    ws_mgr = object.__new__(WorkspaceManager)

    async def _ensure_primary(_self, _uid: str, *, default_name=None, resource_tier=None):
        del default_name, resource_tier
        return dict(comp)

    async def _resolve_binding(_self, _wid: str, *, workspace=None):
        del workspace
        return None

    monkeypatch.setattr(
        WorkspaceManager,
        "ensure_primary_computer",
        create_autospec(WorkspaceManager.ensure_primary_computer, side_effect=_ensure_primary),
    )
    monkeypatch.setattr(
        WorkspaceManager,
        "resolve_binding",
        create_autospec(WorkspaceManager.resolve_binding, side_effect=_resolve_binding),
    )
    monkeypatch.setattr(
        "src.server.app.workspaces.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=ws_mgr),
    )
    _enable_everflow_env(monkeypatch, (email, email_col))
    monkeypatch.setattr(
        dedup_mod,
        "is_coverage_units_enabled",
        create_autospec(_REAL_IS_COV_ENABLED, return_value=True),
    )
    monkeypatch.setattr(
        dedup_mod,
        "is_dalton_supabase_auth_enabled",
        create_autospec(_REAL_IS_SUPA_AUTH, return_value=True),
    )
    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(dedup_mod.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        "src.dalton.settings.is_dalton_local_auth_enabled",
        create_autospec(_REAL_IS_LOCAL_AUTH, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_dalton_local_auth_enabled",
        create_autospec(_REAL_IS_LOCAL_AUTH, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "get_dalton_allowed_emails",
        create_autospec(_REAL_ALLOWED_EMAILS, return_value=frozenset({email, email_col})),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_invite_pm",
        create_autospec(_REAL_IS_INVITE_PM, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_company_system_user",
        create_autospec(_REAL_IS_COMPANY_USER, return_value=False),
    )
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_model",
        create_autospec(dedup_mod.resolve_assist_model, return_value="gpt-4o-mini"),
    )
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_fallback_models",
        create_autospec(dedup_mod.resolve_assist_fallback_models, return_value=[]),
    )
    monkeypatch.setattr(
        dedup_mod,
        "model_requires_user_oauth",
        create_autospec(dedup_mod.model_requires_user_oauth, return_value=False),
    )
    monkeypatch.setattr(dedup_mod, "dalton_complete", _make_llm_stub(("700.HK", "0700.HK")))

    app = create_test_app(ws_router)
    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=uid, auth="session", computer_id=None
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Ordinary create succeeds on first try even with bound Homes present.
        ordinary = await client.post(
            "/api/v1/workspaces", json={"name": "Nvidia", "computer_id": comp_id}
        )
        assert ordinary.status_code == 201, ordinary.text

        # 2. Concurrent ticker variants serialize and create only one workspace.
        monkeypatch.setattr(
            dedup_mod,
            "_collect_candidates",
            _wrap_collect_barrier(dedup_mod._collect_candidates),
        )
        resp_a, resp_b = await asyncio.gather(
            client.post("/api/v1/workspaces", json={"name": "700.HK", "computer_id": comp_id}),
            client.post("/api/v1/workspaces", json={"name": "0700.HK", "computer_id": comp_id}),
        )

    assert sorted([resp_a.status_code, resp_b.status_code]) == [201, 409]
    conflict_resp = resp_b if resp_b.status_code == 409 else resp_a
    assert conflict_resp.json()["detail"]["code"] == "company_workspace_exists"

    async with bundle7_live_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT name FROM workspaces
                WHERE user_id = %s AND status <> 'deleted' AND name <> 'Home'
                ORDER BY name
                """,
                (uid,),
            )
            names = [r["name"] for r in await cur.fetchall()]
    assert len(names) == 2  # Nvidia + exactly one of (700.HK, 0700.HK)


async def test_e2_web_concurrent_alias_same_ticker_real_postgres_commits_unit_in_same_tx(
    bundle7_live_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 2 (real PG): Two concurrent alias creates ('腾讯控股' and 'Tencent')
    sharing ticker '0700.HK'/'700.HK' with LLM returning empty matches commit
    the coverage unit in the same PostgreSQL transaction as the workspace insert;
    only one workspace is created and the second receives 409 company_workspace_exists."""
    from src.server.app.workspaces import router as ws_router
    from src.server.dependencies.principal import Principal, get_principal

    uid = f"b7-al-{uuid.uuid4().hex[:8]}"
    email = f"{uid}@team.local"
    await create_user(user_id=uid, email=email, name="Alias User")
    comp = await create_computer(
        uid,
        kind="docker",
        name="Alias Box",
        is_primary=True,
        status="stopped",
        provider_ref=f"b7-al-{uuid.uuid4()}",
    )
    comp_id = str(comp["computer_id"])

    ws_mgr = object.__new__(WorkspaceManager)

    async def _ensure_primary(_self, _uid: str, *, default_name=None, resource_tier=None):
        del default_name, resource_tier
        return dict(comp)

    async def _resolve_binding(_self, _wid: str, *, workspace=None):
        del workspace
        return None

    monkeypatch.setattr(
        WorkspaceManager,
        "ensure_primary_computer",
        create_autospec(WorkspaceManager.ensure_primary_computer, side_effect=_ensure_primary),
    )
    monkeypatch.setattr(
        WorkspaceManager,
        "resolve_binding",
        create_autospec(WorkspaceManager.resolve_binding, side_effect=_resolve_binding),
    )
    monkeypatch.setattr(
        "src.server.app.workspaces.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=ws_mgr),
    )
    _enable_everflow_env(monkeypatch, (email,))
    monkeypatch.setattr(
        dedup_mod,
        "is_coverage_units_enabled",
        create_autospec(_REAL_IS_COV_ENABLED, return_value=True),
    )
    monkeypatch.setattr(
        "src.dalton.settings.is_coverage_units_enabled",
        create_autospec(_REAL_IS_COV_ENABLED, return_value=True),
    )
    monkeypatch.setattr(
        "src.dalton.settings.get_dalton_settings",
        create_autospec(_REAL_GET_DALTON_SETTINGS, return_value=SimpleNamespace(enabled=True)),
    )
    monkeypatch.setattr(
        dedup_mod,
        "is_dalton_supabase_auth_enabled",
        create_autospec(_REAL_IS_SUPA_AUTH, return_value=True),
    )
    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(dedup_mod.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        access_tiers_mod,
        "is_internal_or_admin",
        create_autospec(access_tiers_mod.is_internal_or_admin, return_value=True),
    )
    monkeypatch.setattr(
        access_tiers_mod,
        "is_admin_user",
        create_autospec(access_tiers_mod.is_admin_user, return_value=False),
    )
    monkeypatch.setattr(
        "src.dalton.settings.is_dalton_local_auth_enabled",
        create_autospec(_REAL_IS_LOCAL_AUTH, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_dalton_local_auth_enabled",
        create_autospec(_REAL_IS_LOCAL_AUTH, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "get_dalton_allowed_emails",
        create_autospec(_REAL_ALLOWED_EMAILS, return_value=frozenset({email})),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_invite_pm",
        create_autospec(_REAL_IS_INVITE_PM, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_company_system_user",
        create_autospec(_REAL_IS_COMPANY_USER, return_value=False),
    )
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_model",
        create_autospec(dedup_mod.resolve_assist_model, return_value="gpt-4o-mini"),
    )
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_fallback_models",
        create_autospec(dedup_mod.resolve_assist_fallback_models, return_value=[]),
    )
    monkeypatch.setattr(
        dedup_mod,
        "model_requires_user_oauth",
        create_autospec(dedup_mod.model_requires_user_oauth, return_value=False),
    )
    monkeypatch.setattr(
        dedup_mod, "_collect_candidates", _wrap_collect_barrier(dedup_mod._collect_candidates)
    )
    # LLM always returns empty matches.
    monkeypatch.setattr(dedup_mod, "dalton_complete", _make_llm_stub())

    app = create_test_app(ws_router)
    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=uid, auth="session", computer_id=None
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp_a, resp_b = await asyncio.gather(
            client.post(
                "/api/v1/workspaces",
                json={
                    "name": "腾讯控股",
                    "computer_id": comp_id,
                    "coverage_kind": "company",
                    "ticker": "0700.HK",
                },
            ),
            client.post(
                "/api/v1/workspaces",
                json={
                    "name": "Tencent",
                    "computer_id": comp_id,
                    "coverage_kind": "company",
                    "ticker": "700.HK",
                },
            ),
        )

    assert sorted([resp_a.status_code, resp_b.status_code]) == [201, 409]
    conflict_resp = resp_b if resp_b.status_code == 409 else resp_a
    assert conflict_resp.json()["detail"]["code"] == "company_workspace_exists"

    async with bundle7_live_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT workspace_id::text AS workspace_id, name
                FROM workspaces
                WHERE user_id = %s AND status <> 'deleted'
                """,
                (uid,),
            )
            ws_rows = await cur.fetchall()
            await cur.execute(
                """
                SELECT workspace_id::text AS workspace_id, ticker, ticker_key
                FROM dalton_coverage_units
                WHERE owner_user_id = %s AND status = 'active'
                """,
                (uid,),
            )
            unit_rows = await cur.fetchall()
    assert len(ws_rows) == 1
    assert len(unit_rows) == 1
    assert unit_rows[0]["workspace_id"] == ws_rows[0]["workspace_id"]
    assert unit_rows[0]["ticker_key"] == "00700"


async def test_e2_web_cross_user_concurrent_alias_same_ticker_real_postgres_creates_only_one(
    bundle7_live_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r3 Item 1 & Item 3 (real PG): Two DIFFERENT internal members concurrently
    creating different aliases for the same normalized ticker with LLM returning
    empty matches serialize on pg_advisory_xact_lock(hashtext('ws_ticker:' || ticker))
    after lock_user_writes; only ONE workspace is created (both with coverage_kind
    and with config-only dalton_company_ticker), and PUT config updates preserve
    dalton_company_ticker while refusing client overrides."""
    from src.server.app.workspaces import router as ws_router
    from src.server.dependencies.principal import Principal, get_principal

    uid_a = f"b7-xu-a-{uuid.uuid4().hex[:8]}"
    uid_b = f"b7-xu-b-{uuid.uuid4().hex[:8]}"
    email_a = f"{uid_a}@team.local"
    email_b = f"{uid_b}@team.local"
    await create_user(user_id=uid_a, email=email_a, name="CrossUser Alice")
    await create_user(user_id=uid_b, email=email_b, name="CrossUser Bob")
    comp_a = await create_computer(
        uid_a,
        kind="docker",
        name="Alice Box",
        is_primary=True,
        status="stopped",
        provider_ref=f"b7-xua-{uuid.uuid4()}",
    )
    comp_b = await create_computer(
        uid_b,
        kind="docker",
        name="Bob Box",
        is_primary=True,
        status="stopped",
        provider_ref=f"b7-xub-{uuid.uuid4()}",
    )
    comp_a_id = str(comp_a["computer_id"])
    comp_b_id = str(comp_b["computer_id"])
    comp_by_user = {uid_a: comp_a, uid_b: comp_b}

    ws_mgr = object.__new__(WorkspaceManager)

    async def _ensure_primary(_self, uid: str, *, default_name=None, resource_tier=None):
        del default_name, resource_tier
        return dict(comp_by_user[uid])

    async def _resolve_binding(_self, _wid: str, *, workspace=None):
        del workspace
        return None

    monkeypatch.setattr(
        WorkspaceManager,
        "ensure_primary_computer",
        create_autospec(WorkspaceManager.ensure_primary_computer, side_effect=_ensure_primary),
    )
    monkeypatch.setattr(
        WorkspaceManager,
        "resolve_binding",
        create_autospec(WorkspaceManager.resolve_binding, side_effect=_resolve_binding),
    )
    monkeypatch.setattr(
        "src.server.app.workspaces.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=ws_mgr),
    )
    _enable_everflow_env(monkeypatch, (email_a, email_b))
    monkeypatch.setattr(
        dedup_mod,
        "is_coverage_units_enabled",
        create_autospec(_REAL_IS_COV_ENABLED, return_value=True),
    )
    monkeypatch.setattr(
        "src.dalton.settings.is_coverage_units_enabled",
        create_autospec(_REAL_IS_COV_ENABLED, return_value=True),
    )
    monkeypatch.setattr(
        "src.dalton.settings.get_dalton_settings",
        create_autospec(_REAL_GET_DALTON_SETTINGS, return_value=SimpleNamespace(enabled=True)),
    )
    monkeypatch.setattr(
        dedup_mod,
        "is_dalton_supabase_auth_enabled",
        create_autospec(_REAL_IS_SUPA_AUTH, return_value=True),
    )
    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(dedup_mod.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        access_tiers_mod,
        "is_internal_or_admin",
        create_autospec(access_tiers_mod.is_internal_or_admin, return_value=True),
    )
    monkeypatch.setattr(
        access_tiers_mod,
        "is_admin_user",
        create_autospec(access_tiers_mod.is_admin_user, return_value=False),
    )
    monkeypatch.setattr(
        "src.dalton.settings.is_dalton_local_auth_enabled",
        create_autospec(_REAL_IS_LOCAL_AUTH, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_dalton_local_auth_enabled",
        create_autospec(_REAL_IS_LOCAL_AUTH, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "get_dalton_allowed_emails",
        create_autospec(_REAL_ALLOWED_EMAILS, return_value=frozenset({email_a, email_b})),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_invite_pm",
        create_autospec(_REAL_IS_INVITE_PM, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_company_system_user",
        create_autospec(_REAL_IS_COMPANY_USER, return_value=False),
    )
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_model",
        create_autospec(dedup_mod.resolve_assist_model, return_value="gpt-4o-mini"),
    )
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_fallback_models",
        create_autospec(dedup_mod.resolve_assist_fallback_models, return_value=[]),
    )
    monkeypatch.setattr(
        dedup_mod,
        "model_requires_user_oauth",
        create_autospec(dedup_mod.model_requires_user_oauth, return_value=False),
    )
    # LLM always returns empty matches.
    monkeypatch.setattr(dedup_mod, "dalton_complete", _make_llm_stub())

    app = create_test_app(ws_router)

    async def _principal_from_header(request: Request) -> Principal:
        return Principal(user_id=request.headers["X-User-Id"], auth="session")

    app.dependency_overrides[get_principal] = _principal_from_header

    orig_collect = dedup_mod._collect_candidates
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Case 1: Two different users with coverage_kind='company' and different aliases.
        monkeypatch.setattr(dedup_mod, "_collect_candidates", _wrap_collect_barrier(orig_collect))
        resp_a, resp_b = await asyncio.gather(
            client.post(
                "/api/v1/workspaces",
                headers={"X-User-Id": uid_a},
                json={
                    "name": "腾讯控股",
                    "computer_id": comp_a_id,
                    "coverage_kind": "company",
                    "ticker": "0700.HK",
                },
            ),
            client.post(
                "/api/v1/workspaces",
                headers={"X-User-Id": uid_b},
                json={
                    "name": "Tencent",
                    "computer_id": comp_b_id,
                    "coverage_kind": "company",
                    "ticker": "700.HK",
                },
            ),
        )

        assert sorted([resp_a.status_code, resp_b.status_code]) == [201, 409]
        winner_resp = resp_a if resp_a.status_code == 201 else resp_b
        conflict_resp = resp_b if resp_b.status_code == 409 else resp_a
        assert conflict_resp.json()["detail"]["code"] == "company_workspace_exists"
        assert (
            conflict_resp.json()["detail"]["matches"][0]["workspace_id"]
            == winner_resp.json()["workspace_id"]
        )

        # Case 2: Two different users WITHOUT coverage_kind (same-tx config ticker stamp).
        monkeypatch.setattr(dedup_mod, "_collect_candidates", _wrap_collect_barrier(orig_collect))
        resp_c, resp_d = await asyncio.gather(
            client.post(
                "/api/v1/workspaces",
                headers={"X-User-Id": uid_a},
                json={
                    "name": "阿里巴巴",
                    "computer_id": comp_a_id,
                    "ticker": "9988.HK",
                },
            ),
            client.post(
                "/api/v1/workspaces",
                headers={"X-User-Id": uid_b},
                json={
                    "name": "Alibaba",
                    "computer_id": comp_b_id,
                    "ticker": "09988",
                },
            ),
        )
        assert sorted([resp_c.status_code, resp_d.status_code]) == [201, 409]
        cfg_winner = resp_c if resp_c.status_code == 201 else resp_d
        cfg_owner = uid_a if resp_c.status_code == 201 else uid_b
        cfg_ws_id = cfg_winner.json()["workspace_id"]

        # Verify PUT whole-config replacement preserves dalton_company_ticker and
        # refuses client overrides in real PostgreSQL.
        put_normal = await client.put(
            f"/api/v1/workspaces/{cfg_ws_id}",
            headers={"X-User-Id": cfg_owner},
            json={"config": {"theme": "dark"}},
        )
        assert put_normal.status_code == 200, put_normal.text
        put_override = await client.put(
            f"/api/v1/workspaces/{cfg_ws_id}",
            headers={"X-User-Id": cfg_owner},
            json={"config": {"dalton_company_ticker": "FORGED", "theme": "light"}},
        )
        assert put_override.status_code == 200, put_override.text

    async with bundle7_live_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT workspace_id::text AS workspace_id, user_id, name, config
                FROM workspaces
                WHERE user_id = ANY(%s::text[]) AND status <> 'deleted'
                ORDER BY created_at ASC
                """,
                ([uid_a, uid_b],),
            )
            ws_rows = await cur.fetchall()
            await cur.execute(
                """
                SELECT workspace_id::text AS workspace_id, ticker_key
                FROM dalton_coverage_units
                WHERE owner_user_id = ANY(%s::text[]) AND status = 'active'
                """,
                ([uid_a, uid_b],),
            )
            unit_rows = await cur.fetchall()
    # Exactly 1 workspace for 00700 + 1 workspace for 09988 across both users.
    assert len(ws_rows) == 2
    assert len(unit_rows) == 1
    assert unit_rows[0]["ticker_key"] == "00700"
    cfg_row = next(r for r in ws_rows if r["workspace_id"] == cfg_ws_id)
    assert cfg_row["config"] == {"theme": "light", "dalton_company_ticker": "09988"}


async def test_bd4_locked_recheck_reads_real_postgres_company_knowledge_doc_ticker(
    bundle7_live_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BD4 (real PG): check_duplicate_snapshot_unchanged reads real
    dalton_knowledge_versions company doc ticker under lock."""
    uid_a = f"b7-bd4a-{uuid.uuid4().hex[:8]}"
    uid_b = f"b7-bd4b-{uuid.uuid4().hex[:8]}"
    email_a = f"{uid_a}@team.local"
    email_b = f"{uid_b}@team.local"
    await create_user(user_id=uid_a, email=email_a, name="Alice")
    await create_user(user_id=uid_b, email=email_b, name="Bob")
    comp_b = await create_computer(
        uid_b,
        kind="docker",
        name="Bob Box",
        is_primary=True,
        status="stopped",
        provider_ref=f"b7-bd4-{uuid.uuid4()}",
    )
    ws_b = await ws_db.create_workspace_on_computer(
        uid_b, "腾讯深度研究", str(comp_b["computer_id"])
    )
    assert ws_b is not None
    ws_b_id = str(ws_b["workspace_id"])

    doc_payload = json.dumps({"ticker": "0700.HK", "name": "Tencent"}, sort_keys=True)
    doc_sha = hashlib.sha256(doc_payload.encode("utf-8")).hexdigest()
    async with bundle7_live_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_knowledge_versions (
                    user_id, workspace_id, doc_key, version_no, source,
                    content, content_sha256, reason, schema_valid
                )
                VALUES (%s, %s::uuid, 'company', 1, 'server', %s::jsonb, %s, 'seed test company doc', TRUE)
                """,
                (uid_b, ws_b_id, doc_payload, doc_sha),
            )

    _enable_everflow_env(monkeypatch, (email_a, email_b))
    monkeypatch.setattr(
        "src.dalton.settings.is_dalton_local_auth_enabled",
        create_autospec(_REAL_IS_LOCAL_AUTH, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_dalton_local_auth_enabled",
        create_autospec(_REAL_IS_LOCAL_AUTH, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "get_dalton_allowed_emails",
        create_autospec(_REAL_ALLOWED_EMAILS, return_value=frozenset({email_a, email_b})),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_invite_pm",
        create_autospec(_REAL_IS_INVITE_PM, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_company_system_user",
        create_autospec(_REAL_IS_COMPANY_USER, return_value=False),
    )

    async with bundle7_live_pool.connection() as conn, conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            conflict_col: dict[str, Any] = {}
            ok_colleague = await ws_db.check_duplicate_snapshot_unchanged(
                cur,
                uid_a,
                "700.HK",
                check_colleague_duplicate=True,
                conflict_out=conflict_col,
                conn=conn,
            )
            assert ok_colleague is False
            assert conflict_col.get("workspace_id") == ws_b_id

            conflict_own: dict[str, Any] = {}
            ok_own = await ws_db.check_duplicate_snapshot_unchanged(
                cur,
                uid_b,
                "00700",
                check_colleague_duplicate=False,
                conflict_out=conflict_own,
                conn=conn,
            )
            assert ok_own is False
            assert conflict_own.get("workspace_id") == ws_b_id


async def test_o1_q1_execution_history_real_postgres_access_rules(
    bundle7_live_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O1 / Q1 + Item 5 (real PG):
    1. Hidden colleague workspace with colleague_dispatch=False strips both
       workspace_id and conversation_thread_id while keeping the execution row.
    2. Soft-deleted colleague workspace with colleague_dispatch=True keeps the
       initiator's readable conversation_thread_id while clearing workspace_id."""
    uid_a = f"b7-o1a-{uuid.uuid4().hex[:8]}"
    uid_b = f"b7-o1b-{uuid.uuid4().hex[:8]}"
    email_a = f"{uid_a}@team.local"
    email_b = f"{uid_b}@team.local"
    await create_user(user_id=uid_a, email=email_a, name="Alice")
    await create_user(user_id=uid_b, email=email_b, name="Bob")
    comp_b = await create_computer(
        uid_b,
        kind="docker",
        name="Bob Box",
        is_primary=True,
        status="stopped",
        provider_ref=f"b7-o1-{uuid.uuid4()}",
    )
    ws_b = await ws_db.create_workspace_on_computer(
        uid_b, "Bob Hidden WS", str(comp_b["computer_id"])
    )
    assert ws_b is not None
    ws_b_id = str(ws_b["workspace_id"])
    thread_id = str(uuid.uuid4())
    auto_id = str(uuid.uuid4())
    exec_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    await conv_db.create_thread(
        conversation_thread_id=thread_id,
        workspace_id=ws_b_id,
        current_status="completed",
        user_id=uid_a,
    )

    async with bundle7_live_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_workspace_sharing (workspace_id, web_access, cos_access, updated_by)
                VALUES (%s::uuid, 'hidden', 'hidden', %s)
                ON CONFLICT (workspace_id) DO UPDATE
                SET web_access = 'hidden', cos_access = 'hidden', updated_by = EXCLUDED.updated_by
                """,
                (ws_b_id, uid_b),
            )
            await cur.execute(
                """
                INSERT INTO automations (
                    automation_id, user_id, name, trigger_type, timezone,
                    instruction, workspace_id, thread_strategy, status, scope
                )
                VALUES (%s::uuid, %s, 'Colleague Hidden Run', 'once', 'UTC',
                        'run', %s::uuid, 'new', 'active', 'personal')
                """,
                (auto_id, uid_a, ws_b_id),
            )
            await cur.execute(
                """
                INSERT INTO automation_executions (
                    automation_execution_id, automation_id, status,
                    conversation_thread_id, scheduled_at, created_at
                )
                VALUES (%s::uuid, %s::uuid, 'completed', %s::uuid, %s, %s)
                """,
                (exec_id, auto_id, thread_id, now, now),
            )

    _enable_everflow_env(monkeypatch, (email_a, email_b))
    monkeypatch.setattr(
        sharing_mod,
        "_sharing_auth_enabled",
        create_autospec(_REAL_SHARING_AUTH_ENABLED, return_value=True),
    )
    monkeypatch.setattr(
        sharing_mod,
        "_multiuser_auth_enabled",
        create_autospec(_REAL_MULTIUSER_AUTH_ENABLED, return_value=True),
    )
    monkeypatch.setattr(
        sharing_mod,
        "get_dalton_allowed_emails",
        create_autospec(_REAL_ALLOWED_EMAILS, return_value=frozenset({email_a, email_b})),
    )
    monkeypatch.setattr(
        "src.dalton.settings.is_dalton_local_auth_enabled",
        create_autospec(_REAL_IS_LOCAL_AUTH, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_dalton_local_auth_enabled",
        create_autospec(_REAL_IS_LOCAL_AUTH, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_invite_pm",
        create_autospec(_REAL_IS_INVITE_PM, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_company_system_user",
        create_autospec(_REAL_IS_COMPANY_USER, return_value=False),
    )

    # Case 1: colleague_dispatch=False + hidden colleague workspace -> strips both IDs.
    monkeypatch.setattr(
        sharing_mod,
        "is_colleague_dispatch_enabled",
        create_autospec(_REAL_IS_COLLEAGUE_DISPATCH, return_value=False),
    )
    rows, _ = await exec_db.list_executions(uid_a, limit=20)
    by_id = {str(r["automation_execution_id"]): r for r in rows}
    assert exec_id in by_id
    assert by_id[exec_id]["workspace_id"] is None
    assert by_id[exec_id]["conversation_thread_id"] is None

    # Case 2: colleague workspace soft-deleted + colleague_dispatch=True ->
    # initiator still has web read access to their own thread (keeps thread_id, clears workspace_id).
    async with bundle7_live_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE workspaces SET status = 'deleted' WHERE workspace_id = %s::uuid",
                (ws_b_id,),
            )
    monkeypatch.setattr(
        sharing_mod,
        "is_colleague_dispatch_enabled",
        create_autospec(_REAL_IS_COLLEAGUE_DISPATCH, return_value=True),
    )
    rows_del, _ = await exec_db.list_executions(uid_a, limit=20)
    by_id_del = {str(r["automation_execution_id"]): r for r in rows_del}
    assert exec_id in by_id_del
    assert str(by_id_del[exec_id]["conversation_thread_id"]) == thread_id
    assert by_id_del[exec_id]["workspace_id"] is None

    # Case 3 (r3 Item 4): automation migrated to caller's own live workspace
    # while colleague workspace stays soft-deleted -> MUST NOT pair the new
    # workspace ID with the old readable thread (workspace_id stays None).
    comp_a = await create_computer(
        uid_a,
        kind="docker",
        name="Alice Box",
        is_primary=True,
        status="stopped",
        provider_ref=f"b7-o1a-{uuid.uuid4()}",
    )
    ws_a = await ws_db.create_workspace_on_computer(
        uid_a, "Alice Migrated WS", str(comp_a["computer_id"])
    )
    assert ws_a is not None
    ws_a_id = str(ws_a["workspace_id"])
    async with bundle7_live_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE automations SET workspace_id = %s::uuid WHERE automation_id = %s::uuid",
                (ws_a_id, auto_id),
            )
    rows_migrated, _ = await exec_db.list_executions(uid_a, limit=20)
    by_id_migrated = {str(r["automation_execution_id"]): r for r in rows_migrated}
    assert exec_id in by_id_migrated
    assert str(by_id_migrated[exec_id]["conversation_thread_id"]) == thread_id
    assert by_id_migrated[exec_id]["workspace_id"] is None
