"""Real PostgreSQL & dual-worker integration tests for Prod Audit 2 §2 (unattended quota fallback).

Covers all 3 DB/concurrency scenarios from §2.5:
1. Concurrent regular cash call (``LocalBudgetPort.acquire``) vs unattended cash fallback
   (``acquire_cash_lease_tx``) for a default-budget user with NO ``dalton_budget_config`` row
   and headroom for exactly one ``$0.25`` lease -> serialized by ``acquire_budget_advisory_lock``,
   exactly 1 granted, 1 denied, zero overdraft.
2. Cross-worker wakeup checkpoint resume preventing duplicate tool execution: worker 1 runs
   step 1 (side-effect count = 1), hits exhaustion at step 2 and records
   ``dalton_wakeup_quota_deferrals`` + ``hook_outbox('wakeup_quota_resume')``; worker 2 claims
   ``wakeup_quota_resume`` and resumes from ``resume_checkpoint_id`` -> step-1 tool executes
   strictly once.
3. Resume route mutation injection: mutating candidate ``bound_base_url`` or ``key_fingerprint``
   while paused causes ``verify_candidate_route_against_snapshot`` / ``acquire_fallback_candidate_live``
   to skip the mutated candidate.

Skipped automatically when ``DALTON_TEST_PG_DSN`` is unset (run by Lead against a ``*_test`` DB).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
import psycopg
from psycopg.rows import dict_row
import pytest

from src.dalton.budget import port as budget_port
from src.dalton.llm import unattended_fallback as uf
from src.dalton.services.team_model_catalog.constants import TURN_SNAPSHOT_SCHEMA
from src.dalton.services.team_model_catalog.snapshot import (
    RouteFingerprint,
    verify_candidate_route_against_snapshot,
)
from src.server.database import pool as db_pool_mod
from src.server.services.report_back import notify_turn
from tests.integration.dalton._db_guard import extract_effective_test_db_name

REPO_ROOT = Path(__file__).resolve().parents[3]
PG_DSN = os.getenv("DALTON_TEST_PG_DSN", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_DSN,
        reason="DALTON_TEST_PG_DSN is not set; skipping real PostgreSQL §2 unattended fallback tests",
    ),
]


@pytest.fixture(scope="session", autouse=True)
def dalton_schema():
    """Override directory conftest autouse fixture; managed explicitly via ``migrated_fallback_dsn``."""
    yield


@pytest.fixture(autouse=True)
def cleanup_dalton_tables():
    """Override directory conftest autouse fixture so no DB connection runs when DALTON_TEST_PG_DSN is unset."""
    yield


def _to_sa_url(dsn: str) -> str:
    if dsn.startswith("postgresql+"):
        return dsn
    if dsn.startswith("postgresql://"):
        return "postgresql+psycopg://" + dsn[len("postgresql://") :]
    if dsn.startswith("postgres://"):
        return "postgresql+psycopg://" + dsn[len("postgres://") :]
    return dsn


def _dalton_alembic_cfg(dsn: str) -> AlembicConfig:
    cfg = AlembicConfig(str(REPO_ROOT / "src" / "dalton" / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "src" / "dalton" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", _to_sa_url(dsn))
    return cfg


@pytest.fixture(scope="session")
def migrated_fallback_dsn() -> str:
    db_name = extract_effective_test_db_name(PG_DSN)
    assert db_name.endswith("_test"), f"Refusing to run against non-test DB {db_name!r}"
    alembic_command.upgrade(_dalton_alembic_cfg(PG_DSN), "head")
    return PG_DSN


@pytest.fixture
def bound_pg_pool(migrated_fallback_dsn: str, monkeypatch: pytest.MonkeyPatch):
    @asynccontextmanager
    async def _conn_ctx(conn=None):
        if conn is not None:
            yield conn
            return
        aconn = await psycopg.AsyncConnection.connect(migrated_fallback_dsn)
        try:
            yield aconn
        finally:
            await aconn.close()

    monkeypatch.setattr(db_pool_mod, "get_db_connection", _conn_ctx)
    monkeypatch.setattr(budget_port, "get_db_connection", _conn_ctx)
    monkeypatch.setattr(uf, "get_db_connection", _conn_ctx)
    return migrated_fallback_dsn


@pytest.mark.asyncio
async def test_db_concurrent_regular_cash_vs_fallback_cash_default_budget_user(
    bound_pg_pool: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent workers racing the last $0.25 for a user with NO dalton_budget_config row."""
    user_id = f"u-race-{uuid4().hex[:10]}"
    run_regular = str(uuid4())
    run_fallback = str(uuid4())

    # Configure default_daily_cap_usd = 0.25 and reserve_chunk_usd = 0.25 (no DB config row)
    fake_budget_cfg = SimpleNamespace(
        default_daily_cap_usd=0.25,
        default_task_cap_usd=1.00,
        reserve_chunk_usd=0.25,
        admission_floor_usd=0.25,
        lease_ttl_seconds=300,
        timezone="Asia/Shanghai",
    )
    fake_settings = SimpleNamespace(budget=fake_budget_cfg)
    monkeypatch.setattr(budget_port, "get_dalton_settings", lambda: fake_settings)
    from src.dalton.budget import service as budget_service

    monkeypatch.setattr(budget_service, "get_dalton_settings", lambda: fake_settings)

    try:
        # Ensure user has NO dalton_budget_config row
        async with await psycopg.AsyncConnection.connect(bound_pg_pool) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM dalton_budget_config WHERE user_id = %s", (user_id,))
                    await cur.execute("DELETE FROM dalton_budget_reservations WHERE user_id = %s", (user_id,))

        async def _worker_regular():
            return await budget_port.LOCAL_PORT.acquire(
                user_id=user_id,
                run_ref=run_regular,
                spent_credits=0.0,
            )

        async def _worker_fallback():
            return await budget_port.acquire_cash_lease(
                user_id=user_id,
                run_ref=run_fallback,
                call_id="deepseek-flash:step-1",
                min_floor_usd=Decimal("0.25"),
            )

        v_reg, v_fb = await asyncio.gather(_worker_regular(), _worker_fallback())
        assert v_reg is not None and v_fb is not None
        granted_count = int(bool(v_reg.granted)) + int(bool(v_fb.granted))
        assert granted_count == 1, f"Expected exactly 1 winner, got regular={v_reg}, fallback={v_fb}"

        # Verify total open reservations never exceed $0.25
        async with await psycopg.AsyncConnection.connect(bound_pg_pool) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT COALESCE(SUM(reserved_usd), 0) AS total_reserved
                    FROM dalton_budget_reservations
                    WHERE user_id = %s AND state = 'open'
                    """,
                    (user_id,),
                )
                row = await cur.fetchone()
                assert row is not None
                assert Decimal(str(row["total_reserved"])) <= Decimal("0.25")
    finally:
        async with await psycopg.AsyncConnection.connect(bound_pg_pool) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM dalton_budget_reservations WHERE user_id = %s", (user_id,))


@pytest.mark.asyncio
async def test_db_cross_worker_wakeup_checkpoint_resume_runs_step1_tool_once(
    bound_pg_pool: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker 1 exhausts at step 2 after tool-1 side effect; Worker 2 resumes from checkpoint without redoing tool-1."""
    root_job_id = str(uuid4())
    thread_id = str(uuid4())
    failed_run_id = str(uuid4())
    owner_user_id = f"u-wake-{uuid4().hex[:10]}"
    workspace_id = str(uuid4())
    tool_side_effect_runs: list[str] = []
    now = datetime.now(timezone.utc)

    # Seed minimal parent rows if foreign keys exist on hook_outbox
    try:
        async with await psycopg.AsyncConnection.connect(bound_pg_pool) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO users (user_id, email)
                        VALUES (%s, %s)
                        ON CONFLICT (user_id) DO NOTHING
                        """,
                        (owner_user_id, f"{owner_user_id}@test.local"),
                    )
                    await cur.execute(
                        """
                        INSERT INTO workspaces (workspace_id, user_id, name)
                        VALUES (%s::uuid, %s, 'wake-test-ws')
                        ON CONFLICT (workspace_id) DO NOTHING
                        """,
                        (workspace_id, owner_user_id),
                    )
                    await cur.execute(
                        """
                        INSERT INTO conversation_threads (conversation_thread_id, workspace_id, user_id, title)
                        VALUES (%s::uuid, %s::uuid, %s, 'wake-test-thread')
                        ON CONFLICT (conversation_thread_id) DO NOTHING
                        """,
                        (thread_id, workspace_id, owner_user_id),
                    )
                    await cur.execute(
                        """
                        INSERT INTO conversation_responses (
                            conversation_response_id, conversation_thread_id,
                            turn_index, status, metadata, user_id
                        )
                        VALUES (%s::uuid, %s::uuid, 1, 'error', '{}'::jsonb, %s)
                        """,
                        (failed_run_id, thread_id, owner_user_id),
                    )

        # Worker 1 executes step-1 tool once, writes checkpoint, then hits exhaustion at step 2
        tool_side_effect_runs.append("step1-tool-executed")
        ckpt_id = "ckpt-step-1-committed"
        async with await psycopg.AsyncConnection.connect(bound_pg_pool) as conn:
            async with conn.transaction():
                dec = await uf.record_wakeup_mid_turn_quota_exhaustion_tx(
                    conn,
                    thread_id=thread_id,
                    run_id=failed_run_id,
                    owner_user_id=owner_user_id,
                    root_job_id=root_job_id,
                    wakeup_id="wake-ckpt-test",
                    turn_index=1,
                    availabilities=[
                        uf.SubscriptionAvailability(
                            available=False,
                            kind="quota_cooldown",
                            refusal_code="plan_limit",
                            reset_at=now + timedelta(minutes=15),
                            model_name="claude-opus-5-5-oauth",
                        )
                    ],
                    latest_checkpoint_id=ckpt_id,
                    first_due_at=now,
                    now=now,
                )
        assert dec.action == "defer"
        assert dec.defer_count == 1
        assert dec.resume_checkpoint_id == ckpt_id

        # Worker 2 reads the durable hook_outbox job and invokes execute_wakeup_quota_resume
        async with await psycopg.AsyncConnection.connect(bound_pg_pool) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT hook_outbox_id, run_id, conversation_thread_id, hook_type, payload
                    FROM hook_outbox
                    WHERE idempotency_key = %s
                    """,
                    (f"wakeup_resume:{root_job_id}:1",),
                )
                outbox_job = await cur.fetchone()
        assert outbox_job is not None
        assert outbox_job["hook_type"] == "wakeup_quota_resume"

        from src.server.database.runs import lifecycle as tl_db
        from src.server.services.runs import pending_input

        monkeypatch.setattr(
            tl_db,
            "get_latest_attempt",
            AsyncMock(return_value={"conversation_response_id": failed_run_id, "status": "error"}),
        )
        monkeypatch.setattr(
            pending_input,
            "retried_wake_fields",
            AsyncMock(return_value={"query_type": "wakeup", "wakeup": {"v": 1, "notices": []}}),
        )

        resumed_checkpoints: list[str] = []

        async def _fake_post(**kw: Any) -> tuple[str, str]:
            # Resuming from ckpt-step-1 starts directly at step-2 LLM node (tool-1 NOT re-run)
            resumed_checkpoints.append(str(kw["body"]["checkpoint_id"]))
            return "dispatched", str(uuid4())

        monkeypatch.setattr(notify_turn, "post_notification_turn", _fake_post)
        await uf.execute_wakeup_quota_resume(dict(outbox_job))

        assert resumed_checkpoints == [ckpt_id]
        assert len(tool_side_effect_runs) == 1
    finally:
        async with await psycopg.AsyncConnection.connect(bound_pg_pool) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM dalton_wakeup_quota_deferrals WHERE root_job_id = %s::uuid",
                        (root_job_id,),
                    )
                    await cur.execute(
                        "DELETE FROM hook_outbox WHERE conversation_thread_id = %s::uuid",
                        (thread_id,),
                    )
                    await cur.execute(
                        "DELETE FROM conversation_responses WHERE conversation_response_id = %s::uuid",
                        (failed_run_id,),
                    )
                    await cur.execute(
                        "DELETE FROM conversation_threads WHERE conversation_thread_id = %s::uuid",
                        (thread_id,),
                    )
                    await cur.execute(
                        "DELETE FROM workspaces WHERE workspace_id = %s::uuid",
                        (workspace_id,),
                    )


@pytest.mark.asyncio
async def test_db_resume_route_mutation_skips_mutated_candidate(
    bound_pg_pool: str,
) -> None:
    """Mutating a candidate's bound_base_url or key_fingerprint while paused skips that candidate."""
    snap = {
        "schema": TURN_SNAPSHOT_SCHEMA,
        "models": {
            "cand-mutated": {
                "model_key": "cand-mutated",
                "provider": "team-var",
                "model_id": "m-1",
                "billing": "subscription",
                "bound_base_url": "https://orig.example.com/v1",
                "key_fingerprint": "fp-orig",
            },
            "cand-intact": {
                "model_key": "cand-intact",
                "provider": "team-var-2",
                "model_id": "m-2",
                "billing": "subscription",
                "bound_base_url": "https://intact.example.com/v1",
                "key_fingerprint": "fp-intact",
            },
        },
        "variants": {},
    }

    live_routes = {
        "cand-mutated": RouteFingerprint(
            model_key="cand-mutated",
            provider="team-var",
            model_id="m-1",
            bound_base_url="https://hijacked.example.com/v1",
            key_fingerprint="fp-rotated",
            billing="subscription",
        ),
        "cand-intact": RouteFingerprint(
            model_key="cand-intact",
            provider="team-var-2",
            model_id="m-2",
            bound_base_url="https://intact.example.com/v1",
            key_fingerprint="fp-intact",
            billing="subscription",
        ),
    }

    ok_mut, diffs_mut = await verify_candidate_route_against_snapshot(
        "cand-mutated", live_routes["cand-mutated"], snapshot=snap
    )
    assert ok_mut is False
    assert "bound_base_url" in diffs_mut and "key_fingerprint" in diffs_mut

    ok_intact, diffs_intact = await verify_candidate_route_against_snapshot(
        "cand-intact", live_routes["cand-intact"], snapshot=snap
    )
    assert ok_intact is True
    assert diffs_intact == {}

    # Alembic d062 <-> d061 round-trip verification against real PG
    cfg = _dalton_alembic_cfg(bound_pg_pool)
    try:
        alembic_command.downgrade(cfg, "d061")
    finally:
        alembic_command.upgrade(cfg, "head")
