"""Real PostgreSQL & dual-worker integration tests for Prod Audit 2 §2 (unattended quota fallback).

Reuses the directory conftest's ``dalton_schema`` (which depends on ``test_db_pool`` and
applies upstream Alembic migrations first, then Dalton migrations to ``head``) and
``test_db_uri``.

Exercises real production call paths against a real PostgreSQL ``*_test`` database:
1. Concurrent regular cash call (``LocalBudgetPort.acquire``) vs ``acquire_cash_lease``
   (pure budget-port concurrency serialization under ``acquire_budget_advisory_lock``) ->
   strictly 1 granted, 1 denied, zero overdraft.
2. Cross-worker wakeup mid-turn exhaustion & checkpoint resume via REAL ``LocalRunExecutor._finalize_run``
   -> ``RunCoordinator.finalize_run`` -> ``lifecycle.finalize_run`` atomic transaction (writing
   ``status='error'``, ``dalton_wakeup_quota_deferrals``, and ``hook_outbox('wakeup_quota_resume')``) ->
   Worker 2 claiming ``wakeup_quota_resume`` -> ``RetryRequest`` validation -> real LangGraph
   ``StateGraph`` + ``MemorySaver`` checkpoint replay asserting step-1 tool executes STRICTLY ONCE.
3. Own dispatched resume run recognition by deterministic ``request_key`` + ``retry_of_run_id``
   against real ``conversation_responses`` (never marking ``dalton_wakeup_quota_deferrals`` settled),
   24h deadline enforcement via ``check_wakeup_deferral_admission_deadline``, and switch-off settlement.
4. Concurrent first-row deferral creation serialized by ``pg_advisory_xact_lock`` + persisted
   ``seen_step_keys`` idempotency + 4th cooldown termination.
5. Resume route mutation injection (``bound_base_url`` / ``key_fingerprint``) + Alembic d062 <-> d061
   round-trip against real PG.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict
from unittest.mock import create_autospec
from uuid import UUID, uuid4

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi import HTTPException
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
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
from src.llms.provider_errors import ProviderRefusal
from src.ptc_agent.agent.middleware.model_resilience import _RemoteQuotaExhaustedError
from src.server.database import pool as db_pool_mod
from src.server.database.runs import lifecycle as tl_db
from src.server.models.workflow import RetryRequest
from src.server.services.report_back import notify_turn
from src.server.services.runs import finalization as finalization_mod
from src.server.services.runs.coordinator import RunCoordinator, RunHandle
from src.server.services.runs.executor import LocalRunExecution, LocalRunExecutor, LocalRunStatus

REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


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
    cfg.set_main_option("sqlalchemy.url", _to_sa_url(dsn).replace("%", "%%"))
    return cfg


@pytest.fixture
def bound_pg_pool(dalton_schema, test_db_uri: str, monkeypatch: pytest.MonkeyPatch) -> str:
    @asynccontextmanager
    async def _conn_ctx(conn=None):
        if conn is not None:
            yield conn
            return
        aconn = await psycopg.AsyncConnection.connect(test_db_uri)
        try:
            yield aconn
        finally:
            await aconn.close()

    monkeypatch.setattr(db_pool_mod, "get_db_connection", _conn_ctx)
    monkeypatch.setattr(budget_port, "get_db_connection", _conn_ctx)
    monkeypatch.setattr(uf, "get_db_connection", _conn_ctx)
    monkeypatch.setattr(tl_db.pool, "get_db_connection", _conn_ctx)
    return test_db_uri


async def test_db_concurrent_budget_port_vs_acquire_cash_lease_default_budget_user(
    bound_pg_pool: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent workers racing the last $0.25 through LocalBudgetPort vs acquire_cash_lease."""
    user_id = f"u-race-{uuid4().hex[:10]}"
    run_regular = str(uuid4())
    run_second = str(uuid4())

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

        async def _worker_cash_lease():
            return await budget_port.acquire_cash_lease(
                user_id=user_id,
                run_ref=run_second,
                call_id="cash-race:step-1",
                min_floor_usd=Decimal("0.25"),
            )

        v_reg, v_second = await asyncio.gather(_worker_regular(), _worker_cash_lease())
        assert v_reg is not None and v_second is not None
        granted_count = int(bool(v_reg.granted)) + int(bool(v_second.granted))
        assert granted_count == 1, f"Expected strictly 1 winner, got regular={v_reg}, second={v_second}"

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


async def test_db_executor_finalize_to_cross_worker_checkpoint_resume_runs_step1_tool_once(
    bound_pg_pool: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker 1 runs real LangGraph graph -> fails at step 2 -> real LocalRunExecutor._finalize_run
    commits status='error' + wakeup_quota_resume in ONE Postgres tx -> Worker 2 claims outbox job,
    validates RetryRequest, and resumes the real graph checkpoint with step-1 tool executed ONCE."""
    monkeypatch.setenv("DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED", "1")
    root_job_id = str(uuid4())
    thread_id = str(uuid4())
    failed_run_id = str(uuid4())
    owner_user_id = f"u-wake-{uuid4().hex[:10]}"
    workspace_id = str(uuid4())
    now = datetime.now(timezone.utc)

    class _State(TypedDict):
        step1_done: bool
        answer: str

    tool_side_effect_runs: list[str] = []
    quota_flag = {"exhausted": True}

    def _tool_node(state: _State) -> dict[str, Any]:
        tool_side_effect_runs.append("step1-tool-executed")
        return {"step1_done": True}

    def _llm_node(state: _State) -> dict[str, Any]:
        if quota_flag["exhausted"]:
            raise _RemoteQuotaExhaustedError(
                "Plan limit reached",
                429,
                expires_at=(now + timedelta(minutes=15)).timestamp(),
                provider_refusal=ProviderRefusal("plan_limit", until=now + timedelta(minutes=15)),
            )
        return {"answer": "db-resumed-ok"}

    builder = StateGraph(_State)
    builder.add_node("tool_step", _tool_node)
    builder.add_node("llm_step", _llm_node)
    builder.add_edge(START, "tool_step")
    builder.add_edge("tool_step", "llm_step")
    builder.add_edge("llm_step", END)
    graph = builder.compile(checkpointer=MemorySaver())

    start_meta = uf.build_wakeup_quota_start_metadata(
        SimpleNamespace(
            query_type="wakeup",
            wakeup={
                "v": 1,
                "root_job_id": root_job_id,
                "wakeup_id": "wake-ckpt-db",
                "first_due_at": now.isoformat(),
                "notices": [{"wakeup_id": "wake-ckpt-db"}],
            },
            llm_model="claude-opus-5-5-oauth",
        ),
        user_id=owner_user_id,
        workspace_id=workspace_id,
        effective_model="claude-opus-5-5-oauth",
    )

    try:
        async with await psycopg.AsyncConnection.connect(bound_pg_pool) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO users (user_id, email) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
                        (owner_user_id, f"{owner_user_id}@test.local"),
                    )
                    await cur.execute(
                        "INSERT INTO workspaces (workspace_id, user_id, name) VALUES (%s::uuid, %s, 'wake-test-ws') ON CONFLICT (workspace_id) DO NOTHING",
                        (workspace_id, owner_user_id),
                    )
                    await cur.execute(
                        "INSERT INTO conversation_threads (conversation_thread_id, workspace_id, user_id, title) VALUES (%s::uuid, %s::uuid, %s, 'wake-test-thread') ON CONFLICT (conversation_thread_id) DO NOTHING",
                        (thread_id, workspace_id, owner_user_id),
                    )
                    await cur.execute(
                        """
                        INSERT INTO conversation_responses (
                            conversation_response_id, conversation_thread_id,
                            turn_index, status, metadata, user_id
                        )
                        VALUES (%s::uuid, %s::uuid, 1, 'in_progress', %s, %s)
                        """,
                        (
                            failed_run_id,
                            thread_id,
                            uf.SafeJson({"workspace_id": workspace_id, "user_id": owner_user_id, **start_meta}),
                            owner_user_id,
                        ),
                    )

        # Worker 1 runs real graph: tool_step executes ONCE & commits checkpoint, llm_step raises
        caught_exc: BaseException | None = None
        try:
            await graph.ainvoke({"step1_done": False, "answer": ""}, config={"configurable": {"thread_id": thread_id}})
        except _RemoteQuotaExhaustedError as exc:
            caught_exc = exc
        assert caught_exc is not None
        assert tool_side_effect_runs == ["step1-tool-executed"]

        saved_state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        ckpt_id = saved_state.config["configurable"]["checkpoint_id"]

        coord = RunCoordinator.get_instance()
        monkeypatch.setattr(
            coord,
            "_newest_checkpoint",
            create_autospec(
                coord._newest_checkpoint,
                instance=False,
                return_value=tl_db.NewestCheckpoint(checkpoint_id=ckpt_id, run_id=failed_run_id),
            ),
        )
        monkeypatch.setattr(coord, "post_finalize_tail", create_autospec(coord.post_finalize_tail, instance=False))
        monkeypatch.setattr(
            finalization_mod,
            "_delivered_steering_inputs",
            create_autospec(finalization_mod._delivered_steering_inputs, instance=False, return_value=[]),
        )

        executor = LocalRunExecutor.__new__(LocalRunExecutor)
        executor.task_lock = asyncio.Lock()
        executor.executions = {}
        executor.event_storage_backend = "memory"
        executor._orphan_collectors = {}
        executor._shielded_tasks = set()
        monkeypatch.setattr(executor, "append_run_end_event", create_autospec(executor.append_run_end_event, instance=False))
        monkeypatch.setattr(executor, "_note_credential_refusal", create_autospec(executor._note_credential_refusal, instance=False))

        handle = RunHandle(
            run_id=failed_run_id,
            thread_id=thread_id,
            turn_index=1,
            attempt_no=1,
            workspace_id=workspace_id,
            user_id=owner_user_id,
            recorded_as=owner_user_id,
            is_byok=True,
            started_at=now,
        )
        fake_handler = SimpleNamespace(
            agent_config=SimpleNamespace(
                llm=SimpleNamespace(name="claude-opus-5-5-oauth"),
                fallback_llm_clients=[],
                feature_enabled=lambda name: False,
            ),
            saw_interrupt=False,
            captured_images={},
            captured_images_by_message={},
            saved_image_paths=[],
            record=lambda stop_events=None: tl_db.ProducerRecord(),
        )
        executor.executions[(thread_id, failed_run_id)] = LocalRunExecution(
            thread_id=thread_id,
            run_id=failed_run_id,
            status=LocalRunStatus.RUNNING,
            created_at=now,
            graph=graph,
            metadata={
                "run_handle": handle,
                "handler": fake_handler,
                "workspace_id": workspace_id,
                "user_id": owner_user_id,
                "recorded_as": owner_user_id,
                "start_time": now.timestamp(),
            },
        )

        await executor._finalize_run(thread_id, failed_run_id, kind="failed", error=str(caught_exc), exc=caught_exc)

        async with await psycopg.AsyncConnection.connect(bound_pg_pool) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT status FROM conversation_responses WHERE conversation_response_id = %s::uuid",
                    (failed_run_id,),
                )
                r_row = await cur.fetchone()
                assert r_row is not None and r_row["status"] == "error"

                await cur.execute(
                    """
                    SELECT hook_outbox_id, run_id, conversation_thread_id, hook_type, payload, attempts
                    FROM hook_outbox
                    WHERE idempotency_key = %s
                    """,
                    (f"wakeup_resume:{root_job_id}:1",),
                )
                outbox_job = await cur.fetchone()
        assert outbox_job is not None
        assert outbox_job["hook_type"] == "wakeup_quota_resume"
        assert outbox_job["payload"]["resume_checkpoint_id"] == ckpt_id

        resumed_answer: dict[str, Any] = {}
        resumed_run_id = str(uuid4())

        async def _dispatch_retry(*args: Any, **kwargs: Any) -> tuple[str, str | None]:
            parsed = RetryRequest.model_validate(kwargs["body"])
            UUID(str(parsed.request_key))
            assert parsed.workspace_id == workspace_id
            assert parsed.checkpoint_id == ckpt_id
            # Insert the dispatched retry run row into real PG to also verify own-run recognition on re-claim
            async with await psycopg.AsyncConnection.connect(bound_pg_pool) as conn:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            INSERT INTO conversation_responses (
                                conversation_response_id, conversation_thread_id,
                                turn_index, attempt_no, status, retry_of_run_id, request_key, user_id
                            )
                            VALUES (%s::uuid, %s::uuid, 1, 2, 'in_progress', %s::uuid, %s, %s)
                            """,
                            (
                                resumed_run_id,
                                thread_id,
                                failed_run_id,
                                str(parsed.request_key),
                                owner_user_id,
                            ),
                        )
            quota_flag["exhausted"] = False
            out = await graph.ainvoke(
                None,
                config={"configurable": {"thread_id": thread_id, "checkpoint_id": parsed.checkpoint_id}},
            )
            resumed_answer.update(out)
            return "dispatched", resumed_run_id

        post_mock = create_autospec(notify_turn.post_notification_turn, instance=False, side_effect=_dispatch_retry)
        monkeypatch.setattr(notify_turn, "post_notification_turn", post_mock)
        await uf.execute_wakeup_quota_resume(dict(outbox_job))

        assert resumed_answer.get("answer") == "db-resumed-ok"
        assert tool_side_effect_runs == ["step1-tool-executed"]

        # Re-claiming the unacked outbox job recognizes resumed_run_id via (request_key, retry_of_run_id)
        # and leaves dalton_wakeup_quota_deferrals.status == 'deferred' (Codex #7)!
        await uf.execute_wakeup_quota_resume(dict(outbox_job))
        assert post_mock.await_count == 1
        async with await psycopg.AsyncConnection.connect(bound_pg_pool) as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT status FROM dalton_wakeup_quota_deferrals WHERE root_job_id = %s::uuid",
                    (root_job_id,),
                )
                d_row = await cur.fetchone()
        assert d_row is not None and d_row["status"] == "deferred"
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
                        "DELETE FROM conversation_responses WHERE conversation_thread_id = %s::uuid",
                        (thread_id,),
                    )
                    await cur.execute(
                        "DELETE FROM conversation_threads WHERE conversation_thread_id = %s::uuid",
                        (thread_id,),
                    )
                    await cur.execute(
                        "DELETE FROM workspaces WHERE workspace_id = %s::uuid",
                        (workspace_id,),
                    )


async def test_db_concurrent_first_deferral_serialized_and_4th_terminates_and_deadline_enforced(
    bound_pg_pool: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent workers racing on a brand-new root_job_id serialize cleanly under advisory lock;
    admission deadline check enforces 24h cap against real PG."""
    root_job_id = str(uuid4())
    root_expired = str(uuid4())
    thread_id = str(uuid4())
    owner_user_id = f"u-conc-{uuid4().hex[:10]}"
    t0 = datetime.now(timezone.utc)
    avail = [
        uf.SubscriptionAvailability(
            available=False,
            kind="quota_cooldown",
            refusal_code="plan_limit",
            reset_at=t0 + timedelta(minutes=30),
            model_name="claude-opus-5-5-oauth",
        )
    ]
    try:
        d_a, d_b = await asyncio.gather(
            uf.evaluate_and_record_wakeup_deferral(
                root_job_id=root_job_id,
                wakeup_id="wake-race",
                thread_id=thread_id,
                owner_user_id=owner_user_id,
                step_key=f"pre:{root_job_id}:1",
                availabilities=avail,
                first_due_at=t0,
                now=t0,
            ),
            uf.evaluate_and_record_wakeup_deferral(
                root_job_id=root_job_id,
                wakeup_id="wake-race",
                thread_id=thread_id,
                owner_user_id=owner_user_id,
                step_key=f"pre:{root_job_id}:1",
                availabilities=avail,
                first_due_at=t0,
                now=t0,
            ),
        )
        assert d_a.defer_count == 1 and d_b.defer_count == 1
        assert int(d_a.idempotent_replay) + int(d_b.idempotent_replay) == 1

        d2 = await uf.evaluate_and_record_wakeup_deferral(
            root_job_id=root_job_id,
            wakeup_id="wake-race",
            thread_id=thread_id,
            owner_user_id=owner_user_id,
            step_key=f"pre:{root_job_id}:2",
            availabilities=avail,
            first_due_at=t0,
            now=t0 + timedelta(minutes=35),
        )
        assert d2.defer_count == 2

        d3 = await uf.evaluate_and_record_wakeup_deferral(
            root_job_id=root_job_id,
            wakeup_id="wake-race",
            thread_id=thread_id,
            owner_user_id=owner_user_id,
            step_key=f"pre:{root_job_id}:3",
            availabilities=avail,
            first_due_at=t0,
            now=t0 + timedelta(minutes=70),
        )
        assert d3.defer_count == 3

        d4 = await uf.evaluate_and_record_wakeup_deferral(
            root_job_id=root_job_id,
            wakeup_id="wake-race",
            thread_id=thread_id,
            owner_user_id=owner_user_id,
            step_key=f"pre:{root_job_id}:4",
            availabilities=avail,
            first_due_at=t0,
            now=t0 + timedelta(minutes=105),
        )
        assert d4.action == "exhausted_failed"
        assert d4.defer_count == 3

        # Pre-dispatch held wakeup crossing 24h deadline is rejected by check_wakeup_deferral_admission_deadline
        past_t0 = t0 - timedelta(hours=25)
        await uf.evaluate_and_record_wakeup_deferral(
            root_job_id=root_expired,
            wakeup_id="wake-exp",
            thread_id=thread_id,
            owner_user_id=owner_user_id,
            step_key=f"pre:{root_expired}:1",
            availabilities=[
                uf.SubscriptionAvailability(
                    available=False,
                    kind="quota_cooldown",
                    refusal_code="plan_limit",
                    reset_at=past_t0 + timedelta(hours=1),
                    model_name="claude-opus-5-5-oauth",
                )
            ],
            first_due_at=past_t0,
            now=past_t0,
        )
        with pytest.raises(HTTPException) as exp_exc:
            await uf.check_wakeup_deferral_admission_deadline(
                root_job_id=root_expired,
                wakeup_id="wake-exp",
                thread_id=thread_id,
                owner_user_id=owner_user_id,
                now=t0,
            )
        assert exp_exc.value.status_code == 400
        assert exp_exc.value.detail["type"] == uf.DALTON_WAKEUP_QUOTA_EXHAUSTED_TYPE
    finally:
        async with await psycopg.AsyncConnection.connect(bound_pg_pool) as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM dalton_wakeup_quota_deferrals WHERE root_job_id IN (%s::uuid, %s::uuid)",
                        (root_job_id, root_expired),
                    )


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

    cfg = _dalton_alembic_cfg(bound_pg_pool)
    try:
        alembic_command.downgrade(cfg, "d061")
    finally:
        alembic_command.upgrade(cfg, "head")
