"""Real PostgreSQL integration tests for Bundle 7 (run by lead outside sandbox):
- E2: Concurrent web workspace creation under real pg_advisory_xact_lock (lock_user_writes).
- BD4: Real dalton_knowledge_versions company doc ticker read inside check_duplicate_snapshot_unchanged.
- O1/Q1: Real list_executions stripping inaccessible colleague workspace_id & thread_id.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import create_autospec
import uuid

from httpx import ASGITransport, AsyncClient
from psycopg.rows import dict_row
import pytest
import pytest_asyncio

from src.dalton import sharing as sharing_mod
from src.dalton.coverage import dedup as dedup_mod
from src.dalton.coverage.dedup import DedupLlmResult, DedupMatchItem
from src.server.database import automation_executions as exec_db
from src.server.database import pool as db_pool
from src.server.database import workspace as ws_db
from src.server.database.computer import create_computer
from src.server.database.user import create_user
from src.server.services.workspace_manager import WorkspaceManager
from tests.conftest import create_test_app

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def bundle7_live_pool(test_db_uri: str, run_migrations):
    """Open an async pool against the migrated test database."""
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        conninfo=test_db_uri,
        min_size=2,
        max_size=6,
        open=False,
        kwargs={"autocommit": True},
    )
    await pool.open()
    prev_pool = db_pool._pool
    db_pool._pool = pool
    try:
        yield pool
    finally:
        db_pool._pool = prev_pool
        await pool.close()


async def test_e2_web_concurrent_create_real_postgres_only_creates_one_workspace(
    bundle7_live_pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2 (real PG): Two concurrent POST /api/v1/workspaces requests for the same
    company ('700.HK' and '0700.HK') serialize on pg_advisory_xact_lock inside
    create_workspace_on_computer; only one workspace is created and the second
    returns 409 company_workspace_exists."""
    from src.server.app.workspaces import router as ws_router

    uid = f"b7-e2-{uuid.uuid4().hex[:8]}"
    email = f"{uid}@team.local"
    await create_user(user_id=uid, email=email, name="E2 User")
    comp = await create_computer(
        uid,
        kind="docker",
        name="E2 Box",
        is_primary=True,
        status="stopped",
        provider_ref=f"b7-e2-{uuid.uuid4()}",
    )
    comp_id = str(comp["computer_id"])

    ws_mgr = object.__new__(WorkspaceManager)

    async def _ensure_primary(_self, _uid: str, **_kw):
        return dict(comp)

    async def _resolve_binding(_self, _wid: str, **_kw):
        return None

    monkeypatch.setattr(WorkspaceManager, "ensure_primary_computer", _ensure_primary)
    monkeypatch.setattr(WorkspaceManager, "resolve_binding", _resolve_binding)
    monkeypatch.setattr(
        "src.server.app.workspaces.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=ws_mgr),
    )
    monkeypatch.setattr(dedup_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(dedup_mod.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(sharing_mod, "is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(
        sharing_mod, "get_dalton_allowed_emails", lambda: frozenset({email})
    )
    monkeypatch.setattr(sharing_mod, "is_invite_pm", lambda _u: False)
    monkeypatch.setattr(sharing_mod, "is_company_system_user", lambda _u: False)
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
    monkeypatch.setattr(dedup_mod, "model_requires_user_oauth", lambda _m: False)

    barrier_count = 0
    both_entered = asyncio.Event()
    orig_collect = dedup_mod._collect_candidates

    async def _synced_collect(*args, **kwargs):
        nonlocal barrier_count
        res = await orig_collect(*args, **kwargs)
        barrier_count += 1
        if barrier_count <= 2:
            if barrier_count == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=5.0)
        return res

    async def _fake_complete(**kwargs):
        matches = []
        for line in kwargs["user_prompt"].splitlines():
            if line.startswith("c") and ("700.HK" in line or "0700.HK" in line):
                cid = line.split("|", 1)[0].strip()
                matches.append(DedupMatchItem(id=cid, confidence="high", reason="same ticker"))
        return SimpleNamespace(ok=True, data=DedupLlmResult(matches=matches))

    monkeypatch.setattr(dedup_mod, "_collect_candidates", _synced_collect)
    monkeypatch.setattr(dedup_mod, "dalton_complete", _fake_complete)

    from src.server.dependencies.principal import Principal, get_principal

    app = create_test_app(ws_router)
    app.dependency_overrides[get_principal] = lambda: Principal(user_id=uid, computer_id=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp_a, resp_b = await asyncio.gather(
            client.post("/api/v1/workspaces", json={"name": "700.HK", "computer_id": comp_id}),
            client.post("/api/v1/workspaces", json={"name": "0700.HK", "computer_id": comp_id}),
        )

    assert sorted([resp_a.status_code, resp_b.status_code]) == [201, 409]
    live = await ws_db.get_live_workspace_names(uid)
    assert len(live) == 1
    conflict_resp = resp_b if resp_b.status_code == 409 else resp_a
    assert conflict_resp.json()["detail"]["code"] == "company_workspace_exists"


async def test_bd4_locked_recheck_reads_real_postgres_company_knowledge_doc_ticker(
    bundle7_live_pool,
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

    async with bundle7_live_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_knowledge_versions
                    (workspace_id, doc_key, version_no, content, schema_valid)
                VALUES (%s::uuid, 'company', 1, '{"ticker": "0700.HK", "name": "Tencent"}'::jsonb, TRUE)
                """,
                (ws_b_id,),
            )

    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(sharing_mod, "is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(
        sharing_mod, "get_dalton_allowed_emails", lambda: frozenset({email_a, email_b})
    )
    monkeypatch.setattr(sharing_mod, "is_invite_pm", lambda _u: False)
    monkeypatch.setattr(sharing_mod, "is_company_system_user", lambda _u: False)

    async with bundle7_live_pool.connection() as conn, conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            ok_colleague = await ws_db.check_duplicate_snapshot_unchanged(
                cur,
                uid_a,
                "700.HK",
                check_colleague_duplicate=True,
                conn=conn,
            )
            assert ok_colleague is False

            ok_own = await ws_db.check_duplicate_snapshot_unchanged(
                cur,
                uid_b,
                "00700",
                check_colleague_duplicate=False,
                conn=conn,
            )
            assert ok_own is False


async def test_o1_q1_execution_history_real_postgres_strips_hidden_colleague_ids(
    bundle7_live_pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O1 / Q1 (real PG): When colleague dispatch is disabled and colleague's
    workspace is hidden, list_executions returns the execution row with both
    workspace_id and conversation_thread_id stripped to None."""
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

    async with bundle7_live_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_workspace_sharing (workspace_id, web_access, cos_access)
                VALUES (%s::uuid, 'hidden', 'hidden')
                ON CONFLICT (workspace_id) DO UPDATE
                SET web_access = 'hidden', cos_access = 'hidden'
                """,
                (ws_b_id,),
            )
            await cur.execute(
                """
                INSERT INTO conversation_threads (conversation_thread_id, workspace_id, user_id)
                VALUES (%s::uuid, %s::uuid, %s)
                """,
                (thread_id, ws_b_id, uid_a),
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

    monkeypatch.setattr(sharing_mod, "_sharing_auth_enabled", lambda: True)
    monkeypatch.setattr(sharing_mod, "_multiuser_auth_enabled", lambda: True)
    monkeypatch.setattr(sharing_mod, "is_colleague_dispatch_enabled", lambda: False)
    monkeypatch.setattr(
        sharing_mod, "get_dalton_allowed_emails", lambda: frozenset({email_a, email_b})
    )
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(sharing_mod, "is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(sharing_mod, "is_invite_pm", lambda _u: False)
    monkeypatch.setattr(sharing_mod, "is_company_system_user", lambda _u: False)

    rows, _ = await exec_db.list_executions(uid_a, limit=20)
    by_id = {str(r["automation_execution_id"]): r for r in rows}
    assert exec_id in by_id
    assert by_id[exec_id]["workspace_id"] is None
    assert by_id[exec_id]["conversation_thread_id"] is None
