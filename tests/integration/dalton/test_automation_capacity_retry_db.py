"""Real PostgreSQL integration tests for automation capacity retry, atomic
settlement + thread reset, delivery reconciliation, signal release, and
wait_backend_idle.sh SQL.

Runs against an isolated ``*_test`` PostgreSQL database (see
``tests/integration/dalton/conftest.py``).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec, patch
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from src.dalton.automations import capacity_retry as cr
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database import watch as watch_db
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
from src.server.database import conversation as conv_db
from src.server.database.runs import lifecycle as tl_db
from src.server.database.runs import outbox as outbox_db
from src.server.services import automation_delivery as delivery_mod
from src.server.services import automation_settlement as settle_mod
from src.server.services.automation_outcomes import Outcome
from src.server.services.automation_scheduler import AutomationScheduler
from src.server.services.automations import lifecycle as auto_lifecycle
from src.server.services.thread_lifecycle_feed import publish_automation_wait
from src.server.services.webhook_client import WebhookClient

# Ensure pool-holding modules are loaded before patched_get_db_connection sweeps sys.modules.
_POOL_HOLDERS = (cr, cg_db, watch_db, auto_db, exec_db, conv_db, tl_db, outbox_db)

pytestmark = pytest.mark.integration


def _make_dispatch_mock(*, side_effect: Any = None):
    sched = create_autospec(AutomationScheduler, instance=True)
    if side_effect is not None:
        sched.dispatch.side_effect = side_effect
    return sched.dispatch


async def _exec_sql(sql: str, params: tuple[Any, ...] = ()) -> None:
    async with exec_db.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)


async def _fetch_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    async with exec_db.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            row = await cur.fetchone()
            return dict(row) if row is not None else None


async def _fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    async with exec_db.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return [dict(r) for r in await cur.fetchall()]


async def _create_automation(
    user_id: str,
    workspace_id: str,
    *,
    name: str = "dalton-nvda-delta",
    delivery_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await auto_db.create_automation(
        user_id=user_id,
        name=name,
        trigger_type="cron",
        instruction="Run delta check",
        cron_expression="0 11 * * 6",
        workspace_id=workspace_id,
        delivery_config=delivery_config,
    )


async def _create_thread(
    workspace_id: str,
    user_id: str,
    *,
    status: str = "in_progress",
) -> str:
    tid = str(uuid4())
    await conv_db.create_thread(
        conversation_thread_id=tid,
        workspace_id=workspace_id,
        current_status=status,
        title="Capacity retry test thread",
        user_id=user_id,
    )
    return tid


async def _insert_execution(
    automation_id: str,
    *,
    status: str = "running",
    thread_id: str | None = None,
) -> str:
    eid = str(uuid4())
    await _exec_sql(
        """
        INSERT INTO automation_executions (
            automation_execution_id, automation_id, status,
            conversation_thread_id, scheduled_at, started_at,
            heartbeat_at, created_at
        )
        VALUES (%s::uuid, %s::uuid, %s, %s::uuid, NOW(), NOW(), NOW(), NOW())
        """,
        (eid, automation_id, status, thread_id),
    )
    return eid


# ---------------------------------------------------------------------------
# 1. Two workers concurrently claiming -> strictly ONE dispatches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_two_workers_claim_only_one_dispatches_pg(
    seed_user, seed_workspace, patched_get_db_connection, monkeypatch
) -> None:
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    user_id = seed_user["user_id"]
    ws_id = str(seed_workspace["workspace_id"])
    auto = await _create_automation(user_id, ws_id)
    tid = await _create_thread(ws_id, user_id, status="in_progress")
    eid = await _insert_execution(str(auto["automation_id"]), status="running", thread_id=tid)

    parked, _ = await cr.park_execution_for_capacity(
        eid,
        parked_status="active",
        error_message="WriterGuardUnavailable: budget exhausted (25 held)",
        thread_id=tid,
        base_backoff_seconds=1.0,
        jitter_fn=lambda a, b: 1.0,
    )
    assert parked is True

    # Make next_retry_at due immediately
    await _exec_sql(
        """
        UPDATE automation_executions
        SET delivery = jsonb_set(
            delivery, '{capacity_retry,next_retry_at}',
            to_jsonb(NOW() - INTERVAL '5 seconds'), true
        )
        WHERE automation_execution_id = %s::uuid
        """,
        (eid,),
    )

    dispatched_a: list[str] = []
    dispatched_b: list[str] = []
    disp_a = _make_dispatch_mock(
        side_effect=lambda a, e, *, name, claim_token=None: dispatched_a.append(e),
    )
    disp_b = _make_dispatch_mock(
        side_effect=lambda a, e, *, name, claim_token=None: dispatched_b.append(e),
    )

    both_listed = asyncio.Event()
    arrivals = [0]
    orig_list = cr.list_capacity_parked_executions

    async def _barrier_list(limit: int = 50, **kw):
        rows = await orig_list(limit=limit, **kw)
        arrivals[0] += 1
        if arrivals[0] >= 2:
            both_listed.set()
        await both_listed.wait()
        return rows

    with patch.object(
        cr,
        "list_capacity_parked_executions",
        new=create_autospec(cr.list_capacity_parked_executions, side_effect=_barrier_list),
    ):
        await asyncio.gather(
            cr.sweep_capacity_parked(disp_a, stopping=lambda: False),
            cr.sweep_capacity_parked(disp_b, stopping=lambda: False),
        )

    assert len(dispatched_a) + len(dispatched_b) == 1
    row = await _fetch_one(
        "SELECT status, delivery FROM automation_executions WHERE automation_execution_id = %s::uuid",
        (eid,),
    )
    assert row is not None
    assert row["status"] == "pending"
    assert row["delivery"]["capacity_retry"].get("dispatch_lease_until") is not None


# ---------------------------------------------------------------------------
# 2. Claim committed then worker crashes before dispatch -> reclaimed after lease
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_then_worker_exit_reclaimed_by_second_worker_pg(
    seed_user, seed_workspace, patched_get_db_connection, monkeypatch
) -> None:
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    user_id = seed_user["user_id"]
    ws_id = str(seed_workspace["workspace_id"])
    auto = await _create_automation(user_id, ws_id)
    eid = await _insert_execution(str(auto["automation_id"]), status="running")

    parked, _ = await cr.park_execution_for_capacity(
        eid, parked_status="active", error_message="WriterGuardUnavailable: full"
    )
    assert parked is True
    await _exec_sql(
        """
        UPDATE automation_executions
        SET delivery = jsonb_set(
            delivery, '{capacity_retry,next_retry_at}',
            to_jsonb(NOW() - INTERVAL '5 seconds'), true
        )
        WHERE automation_execution_id = %s::uuid
        """,
        (eid,),
    )

    # Worker 1 claims waiting -> pending, then simulates crashing before dispatch()
    tok1 = await cr.release_capacity_parked_execution(eid, lease_seconds=30.0)
    assert isinstance(tok1, str) and tok1
    # While lease is active, Worker 2 cannot reclaim it
    assert await cr.release_capacity_parked_execution(eid, lease_seconds=30.0) is None

    # Abandoned sweep must NOT kill the orphaned capacity pending row
    abandoned = await exec_db.list_abandoned_executions(quiet_seconds=0)
    assert eid not in {str(r["automation_execution_id"]) for r in abandoned}

    # Once dispatch lease expires, Worker 2 reclaims and dispatches it with a fresh token
    await _exec_sql(
        """
        UPDATE automation_executions
        SET delivery = jsonb_set(
            delivery, '{capacity_retry,dispatch_lease_until}',
            to_jsonb(NOW() - INTERVAL '1 second'), true
        )
        WHERE automation_execution_id = %s::uuid
        """,
        (eid,),
    )
    reclaimed: list[tuple[str, str | None]] = []
    disp = _make_dispatch_mock(
        side_effect=lambda a, e, *, name, claim_token=None: reclaimed.append((e, claim_token)),
    )
    await cr.sweep_capacity_parked(disp, stopping=lambda: False)
    assert len(reclaimed) == 1
    assert reclaimed[0][0] == eid
    tok2 = reclaimed[0][1]
    assert isinstance(tok2, str) and tok2 != tok1

    # Worker 1's stale token cannot clear Worker 2's lease or enter running
    assert await cr.release_dispatch_lease(eid, claim_token=tok1) is False
    stale_start = await exec_db.begin_execution_if_not_deleted(
        eid, started_at=datetime.now(timezone.utc), claim_token=tok1
    )
    assert stale_start is None

    # Conditionally starting pending -> running succeeds strictly once for the current token holder
    first_start = await exec_db.begin_execution_if_not_deleted(
        eid, started_at=datetime.now(timezone.utc), claim_token=tok2
    )
    second_start = await exec_db.begin_execution_if_not_deleted(
        eid, started_at=datetime.now(timezone.utc), claim_token=tok2
    )
    assert first_start is not None
    assert second_start is None


# ---------------------------------------------------------------------------
# 3. Retry succeeds -> execution reaches completed & delivers strictly once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_succeeds_reaches_completed_and_delivers_once_pg(
    seed_user, seed_workspace, seed_response, patched_get_db_connection, monkeypatch
) -> None:
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    user_id = seed_user["user_id"]
    ws_id = str(seed_workspace["workspace_id"])
    auto = await _create_automation(
        user_id, ws_id, delivery_config={"methods": ["slack:T/C1"]}
    )
    tid = await _create_thread(ws_id, user_id, status="in_progress")
    eid = await _insert_execution(str(auto["automation_id"]), status="running", thread_id=tid)

    held = delivery_mod.Delivery(
        id=eid,
        targets=[delivery_mod.Target(entry="slack:T/C1", address="slack:T/C1", name="#ops", ok=True)],
    )
    assert await exec_db.stamp_delivery(eid, delivery_mod.stamp(held)) is True

    # Attempt 1 parks for capacity -> thread reset away from in_progress in same tx
    parked, _ = await cr.park_execution_for_capacity(
        eid, parked_status="active", error_message="WriterGuardUnavailable", thread_id=tid
    )
    assert parked is True
    trow = await _fetch_one(
        "SELECT current_status FROM conversation_threads WHERE conversation_thread_id = %s::uuid",
        (tid,),
    )
    assert trow["current_status"] == "completed"

    # Backoff elapses -> claim -> begin_execution -> run completes -> settle_by_run
    await _exec_sql(
        """
        UPDATE automation_executions
        SET delivery = jsonb_set(
            delivery, '{capacity_retry,next_retry_at}',
            to_jsonb(NOW() - INTERVAL '5 seconds'), true
        )
        WHERE automation_execution_id = %s::uuid
        """,
        (eid,),
    )
    claim_token = await cr.release_capacity_parked_execution(eid)
    assert isinstance(claim_token, str) and claim_token
    started = await exec_db.begin_execution_if_not_deleted(
        eid, started_at=datetime.now(timezone.utc), claim_token=claim_token
    )
    assert started is not None

    run_id = str(uuid4())
    await seed_response(run_id, tid, turn_index=0, status="completed")
    await exec_db.transition_execution(
        eid, from_statuses=("running",), to="running", conversation_response_id=run_id
    )

    finish_mock = create_autospec(
        delivery_mod.finish_run,
        return_value=delivery_mod.Finish(
            [{"method": "slack:T/C1", "address": "slack:T/C1", "name": "#ops", "success": True, "via": "fallback", "error": None}],
            answered=True,
        ),
    )
    with (
        patch.object(delivery_mod, "finish_run", new=finish_mock),
        patch.object(
            settle_mod,
            "read_run_answer",
            new=create_autospec(
                settle_mod.read_run_answer,
                return_value=settle_mod.RunAnswer(text="Done"),
            ),
        ),
        patch(
            "src.dalton.notify.dispatch.on_automation_settled",
            new=create_autospec(
                __import__("src.dalton.notify.dispatch", fromlist=["on_automation_settled"]).on_automation_settled,
                return_value=None,
            ),
        ),
    ):
        run_row = await exec_db.get_settling_run(run_id)
        outcome = await settle_mod.settle_by_run(
            auto, eid, run_id, run_row, thread_id=tid, workspace_id=ws_id
        )
        await settle_mod.drain_tails()

        assert outcome is Outcome.COMPLETED
        erow = await _fetch_one(
            "SELECT status, failure_reason, conversation_response_id FROM automation_executions WHERE automation_execution_id = %s::uuid",
            (eid,),
        )
        assert erow["status"] == "completed"
        assert erow["failure_reason"] is None
        assert str(erow["conversation_response_id"]) == run_id

        # Production success path enqueues strictly ONE automation_finish outbox job (never in-process finish_run)
        finish_mock.assert_not_awaited()
        outbox_rows = await _fetch_all(
            """
            SELECT hook_outbox_id, hook_type, status, idempotency_key, payload
            FROM hook_outbox
            WHERE run_id = %s::uuid
              AND hook_type = %s
            """,
            (run_id, settle_mod.FINISH_HOOK),
        )
        assert len(outbox_rows) == 1
        assert outbox_rows[0]["idempotency_key"] == f"{run_id}:{settle_mod.FINISH_HOOK}"
        assert outbox_rows[0]["payload"]["execution_id"] == eid
        assert outbox_rows[0]["payload"]["status"] == "completed"

        # Claiming & executing that single outbox job delivers once and records delivery_result
        claimed_jobs = await outbox_db.claim_outbox_jobs(limit=10)
        matching_jobs = [
            j for j in claimed_jobs if str(j["hook_outbox_id"]) == str(outbox_rows[0]["hook_outbox_id"])
        ]
        assert len(matching_jobs) == 1
        await settle_mod._finish_held_run(matching_jobs[0])
        finish_mock.assert_awaited_once()
        delivered_row = await _fetch_one(
            "SELECT delivery_result FROM automation_executions WHERE automation_execution_id = %s::uuid",
            (eid,),
        )
        assert delivered_row["delivery_result"][0]["success"] is True


# ---------------------------------------------------------------------------
# 4. Computer park first (24h) -> capacity fail -> expires in ~15m as FAILED_OURS
#    AND reverse: capacity park first (15m) -> computer park gets full 24h
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_computer_park_then_capacity_fail_expires_in_15m_and_reverse_pg(
    seed_user, seed_workspace, patched_get_db_connection, monkeypatch
) -> None:
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    user_id = seed_user["user_id"]
    ws_id = str(seed_workspace["workspace_id"])
    auto = await _create_automation(user_id, ws_id)
    comp_id = str(uuid4())

    # Path A: Computer park (24h awaits_until) -> released -> capacity park -> 15m deadline
    eid_a = await _insert_execution(str(auto["automation_id"]), status="running")
    assert (
        await exec_db.park_execution(
            eid_a,
            awaits_computer=comp_id,
            parked_status="active",
            wait_seconds=24 * 3600,
        )
        is True
    )
    before = await _fetch_one(
        "SELECT awaits_until FROM automation_executions WHERE automation_execution_id = %s::uuid",
        (eid_a,),
    )
    assert before["awaits_until"] > datetime.now(timezone.utc) + timedelta(hours=23)

    # Simulate computer returning (status back to running, awaits_until preserved as upstream does)
    await _exec_sql(
        "UPDATE automation_executions SET status = 'running' WHERE automation_execution_id = %s::uuid",
        (eid_a,),
    )
    parked, _ = await cr.park_execution_for_capacity(
        eid_a, parked_status="active", error_message="WriterGuardUnavailable: full"
    )
    assert parked is True

    after = await _fetch_one(
        "SELECT awaits_until, delivery FROM automation_executions WHERE automation_execution_id = %s::uuid",
        (eid_a,),
    )
    # awaits_until untouched (still ~24h), capacity deadline_at is ~15m from now
    assert after["awaits_until"] == before["awaits_until"]
    cap_deadline = datetime.fromisoformat(after["delivery"]["capacity_retry"]["deadline_at"])
    remaining = (cap_deadline - datetime.now(timezone.utc)).total_seconds()
    assert 14 * 60 <= remaining <= 15 * 60 + 5

    # Advance capacity deadline past NOW() (while awaits_until is still +24h!) -> settles FAILED_OURS
    await _exec_sql(
        """
        UPDATE automation_executions
        SET delivery = jsonb_set(
            delivery, '{capacity_retry,deadline_at}',
            to_jsonb(NOW() - INTERVAL '1 second'), true
        )
        WHERE automation_execution_id = %s::uuid
        """,
        (eid_a,),
    )
    disp = _make_dispatch_mock()
    webhook_client = create_autospec(WebhookClient, instance=True)
    webhook_client.fire_event.return_value = None
    with (
        patch.object(cr, "WebhookClient", new=create_autospec(WebhookClient, return_value=webhook_client)),
        patch.object(settle_mod, "WebhookClient", new=create_autospec(WebhookClient, return_value=webhook_client)),
        patch.object(settle_mod, "publish_automation_wait", new=create_autospec(publish_automation_wait, return_value=None)),
    ):
        await cr.sweep_capacity_parked(disp, stopping=lambda: False)
        await settle_mod.drain_tails()

    row_a = await _fetch_one(
        "SELECT status, failure_reason FROM automation_executions WHERE automation_execution_id = %s::uuid",
        (eid_a,),
    )
    assert row_a["status"] == "failed"
    assert row_a["failure_reason"] == "server_error"
    disp.assert_not_called()

    # Path B (reverse): Capacity park first (15m) -> awaits_until stays NULL -> computer park gets full 24h
    eid_b = await _insert_execution(str(auto["automation_id"]), status="running")
    parked_b, _ = await cr.park_execution_for_capacity(
        eid_b, parked_status="active", error_message="WriterGuardUnavailable: full"
    )
    assert parked_b is True
    mid_b = await _fetch_one(
        "SELECT awaits_until FROM automation_executions WHERE automation_execution_id = %s::uuid",
        (eid_b,),
    )
    assert mid_b["awaits_until"] is None

    await _exec_sql(
        "UPDATE automation_executions SET status = 'running' WHERE automation_execution_id = %s::uuid",
        (eid_b,),
    )
    assert (
        await exec_db.park_execution(
            eid_b,
            awaits_computer=comp_id,
            parked_status="active",
            wait_seconds=24 * 3600,
        )
        is True
    )
    end_b = await _fetch_one(
        "SELECT awaits_until, wait_reason FROM automation_executions WHERE automation_execution_id = %s::uuid",
        (eid_b,),
    )
    assert end_b["wait_reason"] == "computer_unreachable"
    assert end_b["awaits_until"] > datetime.now(timezone.utc) + timedelta(hours=23)


# ---------------------------------------------------------------------------
# 5. Switch OFF settles already-waiting executions as FAILED_OURS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_drains_and_settles_waiting_executions_as_failed_ours_pg(
    seed_user, seed_workspace, patched_get_db_connection, monkeypatch
) -> None:
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    user_id = seed_user["user_id"]
    ws_id = str(seed_workspace["workspace_id"])
    auto = await _create_automation(user_id, ws_id)
    tid = await _create_thread(ws_id, user_id, status="in_progress")
    eid = await _insert_execution(str(auto["automation_id"]), status="running", thread_id=tid)

    parked, _ = await cr.park_execution_for_capacity(
        eid, parked_status="active", error_message="WriterGuardUnavailable: full", thread_id=tid
    )
    assert parked is True

    # Turn switch OFF while execution is waiting (not yet expired, not yet due)
    monkeypatch.setenv("DALTON_AUTOMATION_CAPACITY_RETRY", "0")
    disp = _make_dispatch_mock()
    webhook_client = create_autospec(WebhookClient, instance=True)
    webhook_client.fire_event.return_value = None
    with (
        patch.object(cr, "WebhookClient", new=create_autospec(WebhookClient, return_value=webhook_client)),
        patch.object(settle_mod, "WebhookClient", new=create_autospec(WebhookClient, return_value=webhook_client)),
        patch.object(settle_mod, "publish_automation_wait", new=create_autospec(publish_automation_wait, return_value=None)),
    ):
        await cr.sweep_capacity_parked(disp, stopping=lambda: False)
        await settle_mod.drain_tails()

    row = await _fetch_one(
        "SELECT status, failure_reason FROM automation_executions WHERE automation_execution_id = %s::uuid",
        (eid,),
    )
    assert row["status"] == "failed"
    assert row["failure_reason"] == "server_error"
    disp.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Pause & manual skip release pre-START claimed signals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_and_manual_skip_release_pre_start_claimed_signals_pg(
    seed_user, seed_workspace, patched_get_db_connection, monkeypatch
) -> None:
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    user_id = seed_user["user_id"]
    ws_id = str(seed_workspace["workspace_id"])
    auto = await _create_automation(user_id, ws_id, name="dalton-nvda-delta")

    async def _seed_and_claim_signal(eid: str, key: str) -> str:
        sig_id = str(uuid4())
        await _exec_sql(
            """
            INSERT INTO dalton_signals (
                signal_id, workspace_id, ticker, probe, signal_key,
                priority, summary, payload, woken_execution_id, woken_at
            )
            VALUES (
                %s::uuid, %s::uuid, 'NVDA', 'price', %s,
                'P1', 'Price move', '{"status": "woken"}'::jsonb, %s::uuid, NOW()
            )
            """,
            (sig_id, ws_id, key, eid),
        )
        return sig_id

    # Case A: Automation paused while capacity-parked -> sweep skips & releases signal
    eid_pause = await _insert_execution(str(auto["automation_id"]), status="running")
    sig_pause = await _seed_and_claim_signal(eid_pause, f"pause-{uuid4().hex[:8]}")
    assert (
        await cr.park_execution_for_capacity(
            eid_pause, parked_status="active", error_message="WriterGuardUnavailable"
        )
    )[0] is True
    await _exec_sql(
        "UPDATE automations SET status = 'paused' WHERE automation_id = %s::uuid",
        (str(auto["automation_id"]),),
    )
    disp = _make_dispatch_mock()
    with patch.object(settle_mod, "publish_automation_wait", new=create_autospec(publish_automation_wait, return_value=None)):
        await cr.sweep_capacity_parked(disp, stopping=lambda: False)
        await settle_mod.drain_tails()

    erow = await _fetch_one(
        "SELECT status, skip_reason FROM automation_executions WHERE automation_execution_id = %s::uuid",
        (eid_pause,),
    )
    assert erow["status"] == "skipped"
    assert erow["skip_reason"] == "user"
    srow = await _fetch_one(
        "SELECT woken_execution_id, payload->>'status' AS st FROM dalton_signals WHERE signal_id = %s::uuid",
        (sig_pause,),
    )
    assert srow["woken_execution_id"] is None
    assert srow["st"] == "pending"

    # Case B: Manual skip_execution on waiting execution -> releases signal
    await _exec_sql(
        "UPDATE automations SET status = 'active' WHERE automation_id = %s::uuid",
        (str(auto["automation_id"]),),
    )
    eid_skip = await _insert_execution(str(auto["automation_id"]), status="running")
    sig_skip = await _seed_and_claim_signal(eid_skip, f"skip-{uuid4().hex[:8]}")
    assert (
        await cr.park_execution_for_capacity(
            eid_skip, parked_status="active", error_message="WriterGuardUnavailable"
        )
    )[0] is True
    with patch.object(settle_mod, "publish_automation_wait", new=create_autospec(publish_automation_wait, return_value=None)):
        res = await auto_lifecycle.skip_execution(str(auto["automation_id"]), eid_skip, user_id)
        await settle_mod.drain_tails()
    assert res is not None and res["status"] == "skipped"
    srow2 = await _fetch_one(
        "SELECT woken_execution_id, payload->>'status' AS st FROM dalton_signals WHERE signal_id = %s::uuid",
        (sig_skip,),
    )
    assert srow2["woken_execution_id"] is None
    assert srow2["st"] == "pending"


# ---------------------------------------------------------------------------
# 7. Two PG connections: concurrent START vs settlement thread-status reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settlement_and_thread_reset_concurrent_start_race_pg(
    seed_user, seed_workspace, patched_get_db_connection, test_db_uri
) -> None:
    """Conn 1 holds conversation_threads row lock in a START tx (inserting an
    in_progress response and updating current_status='in_progress'); Conn 2 runs
    settle(..., reset_prestart_thread_status=True). Conn 2 waits on SELECT FOR UPDATE,
    then reads Conn 1's committed in_progress response on a fresh snapshot and does
    NOT overwrite current_status='in_progress'."""
    user_id = seed_user["user_id"]
    ws_id = str(seed_workspace["workspace_id"])
    auto = await _create_automation(user_id, ws_id)
    tid = await _create_thread(ws_id, user_id, status="in_progress")
    eid = await _insert_execution(str(auto["automation_id"]), status="running", thread_id=tid)
    new_run_id = str(uuid4())

    conn1_locked = asyncio.Event()
    release_conn1 = asyncio.Event()

    async def _concurrent_start() -> None:
        async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=False) as c1:
            async with c1.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO conversation_responses (
                        conversation_response_id, conversation_thread_id,
                        turn_index, status, created_at
                    )
                    VALUES (%s::uuid, %s::uuid, 0, 'in_progress', NOW())
                    """,
                    (new_run_id, tid),
                )
                await cur.execute(
                    """
                    UPDATE conversation_threads
                    SET current_status = 'in_progress', updated_at = NOW()
                    WHERE conversation_thread_id = %s::uuid
                    """,
                    (tid,),
                )
            conn1_locked.set()
            await release_conn1.wait()
            await c1.commit()

    start_task = asyncio.create_task(_concurrent_start())
    await conn1_locked.wait()

    webhook_client = create_autospec(WebhookClient, instance=True)
    webhook_client.fire_event.return_value = None
    with patch.object(
        settle_mod, "WebhookClient", new=create_autospec(WebhookClient, return_value=webhook_client)
    ):
        settle_task = asyncio.create_task(
            settle_mod.settle(
                auto,
                eid,
                Outcome.FAILED_OURS,
                thread_id=tid,
                workspace_id=ws_id,
                error="WriterGuardUnavailable",
                reset_prestart_thread_status=True,
            )
        )
        await asyncio.sleep(0.15)
        # Conn 2 must still be waiting on Conn 1's thread lock
        assert not settle_task.done()
        release_conn1.set()
        await start_task
        settled = await settle_task
        await settle_mod.drain_tails()

    assert settled is True
    trow = await _fetch_one(
        "SELECT current_status FROM conversation_threads WHERE conversation_thread_id = %s::uuid",
        (tid,),
    )
    assert trow["current_status"] == "in_progress"


# ---------------------------------------------------------------------------
# 8. Channel gateway registration update/revoke & wait_backend_idle.sh SQL on PG
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_gateway_register_updates_on_entry_change_and_revokes_on_clear_pg(
    seed_user, seed_workspace, patched_get_db_connection
) -> None:
    """register_automation_run re-resolves unstarted rows when entries change (including
    <uuid>.2 delivery IDs), and revoke_unstarted_automation_run deletes unstarted rows."""
    user_id = seed_user["user_id"]
    ws_id = str(seed_workspace["workspace_id"])
    eid = str(uuid4())

    await _exec_sql(
        """
        INSERT INTO dalton_channel_identities (user_id, platform, platform_user_id, platform_username)
        VALUES (%s, 'slack', 'U_SLACK', 'alice'), (%s, 'telegram', 'U_TG', 'alice_tg')
        ON CONFLICT DO NOTHING
        """,
        (user_id, user_id),
    )

    first = await cg_db.register_automation_run(
        execution_id=eid,
        user_id=user_id,
        workspace_id=ws_id,
        automation_name="delta",
        entries=["slack"],
    )
    assert [t["entry"] for t in first["targets"]] == ["slack"]

    # Changed recipient with <uuid>.2 delivery ID replaces old targets in place
    second = await cg_db.register_automation_run(
        execution_id=f"{eid}.2",
        user_id=user_id,
        workspace_id=ws_id,
        automation_name="delta",
        entries=["telegram"],
    )
    assert [t["entry"] for t in second["targets"]] == ["telegram"]

    # Revoke deletes the unstarted registration -> /finish returns 404 (zero old deliveries)
    assert await cg_db.revoke_unstarted_automation_run(eid, user_id=user_id) is True
    remaining = await _fetch_one(
        "SELECT execution_id FROM dalton_channel_automation_runs WHERE execution_id = %s::uuid",
        (eid,),
    )
    assert remaining is None


@pytest.mark.asyncio
async def test_wait_backend_idle_sql_on_real_postgres(
    seed_user, seed_workspace, patched_get_db_connection
) -> None:
    """Execute the exact shared backend_busy_threads.sql (used by wait_backend_idle.sh
    and deploy_main.sh) against real Postgres."""
    sql_path = (
        Path(__file__).resolve().parents[3]
        / "scripts/dalton/ops/prod/backend_busy_threads.sql"
    )
    thread_busy_sql = " ".join(sql_path.read_text(encoding="utf-8").split())
    total_busy_sql = (
        f"SELECT ({thread_busy_sql}) + "
        "(SELECT count(*) FROM subagent_runs WHERE status='in_progress')"
    )

    user_id = seed_user["user_id"]
    ws_id = str(seed_workspace["workspace_id"])
    auto = await _create_automation(user_id, ws_id)

    # 1) Stale (>15m) automation thread with all terminal executions -> NOT busy (count = 0)
    stale_tid = await _create_thread(ws_id, user_id, status="in_progress")
    await _insert_execution(str(auto["automation_id"]), status="failed", thread_id=stale_tid)
    await _exec_sql(
        "UPDATE conversation_threads SET updated_at = NOW() - INTERVAL '20 minutes' WHERE conversation_thread_id = %s::uuid",
        (stale_tid,),
    )
    res = await _fetch_one(thread_busy_sql)
    assert int(next(iter(res.values()))) == 0
    total_res = await _fetch_one(total_busy_sql)
    assert int(next(iter(total_res.values()))) == 0

    # 2) Interactive chat thread (>15m, no automation_executions) -> BUSY (count = 1)
    chat_tid = await _create_thread(ws_id, user_id, status="in_progress")
    await _exec_sql(
        "UPDATE conversation_threads SET updated_at = NOW() - INTERVAL '25 minutes' WHERE conversation_thread_id = %s::uuid",
        (chat_tid,),
    )
    res2 = await _fetch_one(thread_busy_sql)
    assert int(next(iter(res2.values()))) == 1
    total_res2 = await _fetch_one(total_busy_sql)
    assert int(next(iter(total_res2.values()))) == 1


# ---------------------------------------------------------------------------
# 9. R3 PG: claim-token fencing & capacity deadline on begin_execution (Item 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_begin_execution_rejects_expired_capacity_deadline_and_stale_token_pg(
    seed_user, seed_workspace, patched_get_db_connection
) -> None:
    """Real PG: two workers reclaiming get distinct tokens (only current token
    enters running); a claimed pending row whose 15m deadline expired refuses to
    enter running."""
    user_id = seed_user["user_id"]
    ws_id = str(seed_workspace["workspace_id"])
    auto = await _create_automation(user_id, ws_id)
    eid = await _insert_execution(str(auto["automation_id"]), status="running")

    parked, _ = await cr.park_execution_for_capacity(
        eid, parked_status="active", error_message="WriterGuardUnavailable: full"
    )
    assert parked is True
    await _exec_sql(
        """
        UPDATE automation_executions
        SET delivery = jsonb_set(
            delivery, '{capacity_retry,next_retry_at}',
            to_jsonb(NOW() - INTERVAL '5 seconds'), true
        )
        WHERE automation_execution_id = %s::uuid
        """,
        (eid,),
    )
    tok1 = await cr.release_capacity_parked_execution(eid, lease_seconds=30.0)
    assert isinstance(tok1, str) and tok1

    # Even with valid token, once deadline_at passes, begin_execution refuses to start
    await _exec_sql(
        """
        UPDATE automation_executions
        SET delivery = jsonb_set(
            delivery, '{capacity_retry,deadline_at}',
            to_jsonb(NOW() - INTERVAL '1 second'), true
        )
        WHERE automation_execution_id = %s::uuid
        """,
        (eid,),
    )
    expired_start = await exec_db.begin_execution_if_not_deleted(
        eid, started_at=datetime.now(timezone.utc), claim_token=tok1
    )
    assert expired_start is None
    row = await _fetch_one(
        "SELECT status FROM automation_executions WHERE automation_execution_id = %s::uuid",
        (eid,),
    )
    assert row["status"] == "pending"


# ---------------------------------------------------------------------------
# 10. R3 PG: locked expiry settlement re-checks wait_reason, token, deadline (Item 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expiry_settlement_never_clobbers_computer_offline_wait_pg(
    seed_user, seed_workspace, patched_get_db_connection
) -> None:
    """Real PG: after a capacity-claimed pending row starts and parks for an
    offline computer (wait_reason='computer_unreachable'), a stale capacity
    expiry sweep cannot settle it as capacity failure."""
    user_id = seed_user["user_id"]
    ws_id = str(seed_workspace["workspace_id"])
    auto = await _create_automation(user_id, ws_id)
    eid = await _insert_execution(str(auto["automation_id"]), status="running")
    comp_id = str(uuid4())

    parked, _ = await cr.park_execution_for_capacity(
        eid, parked_status="active", error_message="WriterGuardUnavailable: full"
    )
    assert parked is True
    await _exec_sql(
        """
        UPDATE automation_executions
        SET delivery = jsonb_set(
            delivery, '{capacity_retry,next_retry_at}',
            to_jsonb(NOW() - INTERVAL '5 seconds'), true
        )
        WHERE automation_execution_id = %s::uuid
        """,
        (eid,),
    )
    tok = await cr.release_capacity_parked_execution(eid, lease_seconds=30.0)
    assert isinstance(tok, str) and tok

    # Snapshot stale sweep row as if capacity sweep read it right as deadline elapsed
    stale_row = await _fetch_one(
        """
        SELECT e.automation_execution_id, e.automation_id, e.status, e.parked_status,
               e.conversation_thread_id, e.delivery, a.user_id, a.workspace_id
        FROM automation_executions e
        JOIN automations a ON a.automation_id = e.automation_id
        WHERE e.automation_execution_id = %s::uuid
        """,
        (eid,),
    )
    assert stale_row is not None
    stale_row["expired"] = True
    stale_row["due"] = True

    # Meanwhile, the dispatched worker starts and parks for offline computer
    started = await exec_db.begin_execution_if_not_deleted(
        eid, started_at=datetime.now(timezone.utc), claim_token=tok
    )
    assert started is not None
    assert (
        await exec_db.park_execution(
            eid, awaits_computer=comp_id, parked_status="active", wait_seconds=24 * 3600
        )
        is True
    )
    # Even if old capacity deadline is now past NOW(), locked recheck sees wait_reason='computer_unreachable'
    await _exec_sql(
        """
        UPDATE automation_executions
        SET delivery = jsonb_set(
            delivery, '{capacity_retry,deadline_at}',
            to_jsonb(NOW() - INTERVAL '1 second'), true
        )
        WHERE automation_execution_id = %s::uuid
        """,
        (eid,),
    )
    disp = _make_dispatch_mock()
    await cr.sweep_one_capacity_parked(stale_row, disp, stopping=lambda: False)
    await settle_mod.drain_tails()

    after = await _fetch_one(
        "SELECT status, wait_reason FROM automation_executions WHERE automation_execution_id = %s::uuid",
        (eid,),
    )
    assert after["status"] == "waiting"
    assert after["wait_reason"] == "computer_unreachable"


# ---------------------------------------------------------------------------
# 11. R3 PG: default Feishu group re-resolution & durable threadless failure
#     notify & pending skip / abandoned thread reset (Items 4, 6, 7, 8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_gateway_re_resolves_default_feishu_group_when_entries_unchanged_pg(
    seed_user, seed_workspace, patched_get_db_connection
) -> None:
    """Real PG: register_automation_run re-resolves unchanged default entry ['feishu']
    when workspace default group changes before start, updating the unstarted row."""
    user_id = seed_user["user_id"]
    ws_id = str(seed_workspace["workspace_id"])
    eid = str(uuid4())

    await _exec_sql(
        """
        INSERT INTO dalton_channel_identities (user_id, platform, platform_user_id, platform_username)
        VALUES (%s, 'feishu', 'ou_user_1', 'alice')
        ON CONFLICT DO NOTHING
        """,
        (user_id,),
    )
    await _exec_sql(
        """
        INSERT INTO dalton_channel_user_settings (user_id, apps, updated_at)
        VALUES (%s, %s::jsonb, NOW())
        ON CONFLICT (user_id) DO UPDATE SET apps = EXCLUDED.apps, updated_at = NOW()
        """,
        (
            user_id,
            f'{{"feishu": {{"automation_output": {{"{ws_id}": "feishu:oc_old_grp"}}}}}}',
        ),
    )

    verify_mock = create_autospec(
        cg_db._verify_group_target_deliverable,
        side_effect=lambda cur, *, chat_address, **kw: (True, None, f"Group {chat_address}"),
    )
    with patch.object(cg_db, "_verify_group_target_deliverable", new=verify_mock):
        first = await cg_db.register_automation_run(
            execution_id=eid,
            user_id=user_id,
            workspace_id=ws_id,
            automation_name="delta",
            entries=["feishu"],
        )
        assert [t["address"] for t in first["targets"]] == ["feishu:oc_old_grp"]

        # User switches workspace default Feishu group while methods stays ["feishu"]
        await _exec_sql(
            """
            UPDATE dalton_channel_user_settings
            SET apps = %s::jsonb, updated_at = NOW()
            WHERE user_id = %s
            """,
            (
                f'{{"feishu": {{"automation_output": {{"{ws_id}": "feishu:oc_new_grp"}}}}}}',
                user_id,
            ),
        )
        second = await cg_db.register_automation_run(
            execution_id=f"{eid}.2",
            user_id=user_id,
            workspace_id=ws_id,
            automation_name="delta",
            entries=["feishu"],
        )
        assert [t["address"] for t in second["targets"]] == ["feishu:oc_new_grp"]

    stored = await _fetch_one(
        "SELECT targets FROM dalton_channel_automation_runs WHERE execution_id = %s::uuid",
        (eid,),
    )
    assert stored is not None
    assert [t["address"] for t in stored["targets"]] == ["feishu:oc_new_grp"]


@pytest.mark.asyncio
async def test_durable_failure_notify_threadless_survives_crash_and_delivers_on_sweep_pg(
    seed_user, seed_workspace, patched_get_db_connection, monkeypatch
) -> None:
    """Real PG: expiry settlement of a threadless execution writes delivery.failure_notify
    inside the settlement transaction; even if _after_settling crashes before sending,
    drain_pending_failure_notifications delivers it once and marks it done."""
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    user_id = seed_user["user_id"]
    ws_id = str(seed_workspace["workspace_id"])
    auto = await _create_automation(
        user_id, ws_id, delivery_config={"methods": ["feishu"]}
    )
    eid = await _insert_execution(
        str(auto["automation_id"]), status="running", thread_id=None
    )

    parked, _ = await cr.park_execution_for_capacity(
        eid, parked_status="active", error_message="HTTPException: Credit gate 503"
    )
    assert parked is True
    await _exec_sql(
        """
        UPDATE automation_executions
        SET delivery = jsonb_set(
            delivery, '{capacity_retry,deadline_at}',
            to_jsonb(NOW() - INTERVAL '1 second'), true
        )
        WHERE automation_execution_id = %s::uuid
        """,
        (eid,),
    )

    # Simulate process crashing right after settlement commit (deliver never runs in _after_settling)
    with patch.object(
        cr,
        "deliver_settled_failure_notification",
        new=create_autospec(cr.deliver_settled_failure_notification, return_value=False),
    ):
        settled = await cr._settle_capacity_failure(
            auto,
            eid,
            thread_id=None,
            workspace_id=ws_id,
            error="HTTPException: Credit gate 503",
            expected_claim_token=None,
            require_capacity_expired=True,
        )
        await settle_mod.drain_tails()
    assert settled is True

    after_commit = await _fetch_one(
        "SELECT status, conversation_thread_id, delivery FROM automation_executions WHERE automation_execution_id = %s::uuid",
        (eid,),
    )
    assert after_commit["status"] == "failed"
    assert after_commit["conversation_thread_id"] is None
    assert after_commit["delivery"][cr.FAILURE_NOTIFY_KEY]["status"] == "pending"

    # Next worker sweeps and delivers the durable failure notification
    resolved = delivery_mod.Delivery(
        id=eid,
        targets=[delivery_mod.Target(entry="feishu", address="feishu:oc_new", name="Group", ok=True)],
    )
    start_mock = create_autospec(delivery_mod.start_run, return_value=resolved)
    finish_mock = create_autospec(
        delivery_mod.finish_run,
        return_value=delivery_mod.Finish(
            [{"method": "feishu", "address": "feishu:oc_new", "name": "Group", "success": True, "via": "notice", "error": None}],
            answered=True,
        ),
    )
    with (
        patch.object(delivery_mod, "start_run", new=start_mock),
        patch.object(delivery_mod, "finish_run", new=finish_mock),
    ):
        drained = await cr.drain_pending_failure_notifications()
    assert drained == 1
    start_mock.assert_awaited_once()
    finish_mock.assert_awaited_once()

    final_row = await _fetch_one(
        "SELECT delivery, delivery_result FROM automation_executions WHERE automation_execution_id = %s::uuid",
        (eid,),
    )
    assert final_row["delivery"][cr.FAILURE_NOTIFY_KEY]["status"] == "done"
    assert final_row["delivery_result"][0]["success"] is True


@pytest.mark.asyncio
async def test_manual_skip_capacity_pending_and_abandoned_reset_concurrent_start_pg(
    seed_user, seed_workspace, patched_get_db_connection, test_db_uri
) -> None:
    """Real PG:
    1) skip_execution skips capacity-claimed pending (releasing signals) while
       refusing ordinary pending.
    2) settle_abandoned without a conversation_response resets thread status in-tx,
       and respects a concurrent START holding the conversation_threads row lock."""
    user_id = seed_user["user_id"]
    ws_id = str(seed_workspace["workspace_id"])
    auto = await _create_automation(user_id, ws_id, name="dalton-nvda-delta")

    # 1a) Capacity-claimed pending CAN be skipped manually and releases signal
    eid_cap = await _insert_execution(str(auto["automation_id"]), status="running")
    sig_id = str(uuid4())
    await _exec_sql(
        """
        INSERT INTO dalton_signals (
            signal_id, workspace_id, ticker, probe, signal_key,
            priority, summary, payload, woken_execution_id, woken_at
        )
        VALUES (
            %s::uuid, %s::uuid, 'NVDA', 'price', %s,
            'P1', 'Price move', '{"status": "woken"}'::jsonb, %s::uuid, NOW()
        )
        """,
        (sig_id, ws_id, f"cap-pending-{uuid4().hex[:8]}", eid_cap),
    )
    assert (
        await cr.park_execution_for_capacity(
            eid_cap, parked_status="active", error_message="WriterGuardUnavailable"
        )
    )[0] is True
    await _exec_sql(
        """
        UPDATE automation_executions
        SET delivery = jsonb_set(
            delivery, '{capacity_retry,next_retry_at}',
            to_jsonb(NOW() - INTERVAL '5 seconds'), true
        )
        WHERE automation_execution_id = %s::uuid
        """,
        (eid_cap,),
    )
    tok = await cr.release_capacity_parked_execution(eid_cap)
    assert isinstance(tok, str) and tok
    with patch.object(
        settle_mod,
        "publish_automation_wait",
        new=create_autospec(publish_automation_wait, return_value=None),
    ):
        skipped = await auto_lifecycle.skip_execution(
            str(auto["automation_id"]), eid_cap, user_id
        )
        await settle_mod.drain_tails()
    assert skipped is not None and skipped["status"] == "skipped"
    srow = await _fetch_one(
        "SELECT woken_execution_id, payload->>'status' AS st FROM dalton_signals WHERE signal_id = %s::uuid",
        (sig_id,),
    )
    assert srow["woken_execution_id"] is None
    assert srow["st"] == "pending"

    # 1b) Ordinary pending (wait_reason IS NULL) is still refused by skip_execution
    eid_ord = await _insert_execution(str(auto["automation_id"]), status="pending")
    with pytest.raises(ValueError, match="no longer waiting"):
        await auto_lifecycle.skip_execution(str(auto["automation_id"]), eid_ord, user_id)

    # 2a) Abandoned execution without conversation_response resets thread status in-tx
    tid_clean = await _create_thread(ws_id, user_id, status="in_progress")
    eid_ab1 = await _insert_execution(
        str(auto["automation_id"]), status="running", thread_id=tid_clean
    )
    await _exec_sql(
        "UPDATE automation_executions SET heartbeat_at = NOW() - INTERVAL '10 minutes' WHERE automation_execution_id = %s::uuid",
        (eid_ab1,),
    )
    ab_row1 = {
        "automation_execution_id": eid_ab1,
        "automation_id": str(auto["automation_id"]),
        "status": "running",
        "conversation_thread_id": tid_clean,
        "conversation_response_id": None,
        "started_at": datetime.now(timezone.utc) - timedelta(minutes=10),
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=10),
        "delivery": None,
        "user_id": user_id,
        "workspace_id": ws_id,
    }
    webhook_client = create_autospec(WebhookClient, instance=True)
    webhook_client.fire_event.return_value = None
    with patch.object(
        settle_mod,
        "WebhookClient",
        new=create_autospec(WebhookClient, return_value=webhook_client),
    ):
        out1 = await settle_mod.settle_abandoned(auto, ab_row1, 300)
        await settle_mod.drain_tails()
    assert out1 is Outcome.INTERRUPTED
    trow_clean = await _fetch_one(
        "SELECT current_status FROM conversation_threads WHERE conversation_thread_id = %s::uuid",
        (tid_clean,),
    )
    assert trow_clean["current_status"] == "cancelled"

    # 2b) Two real PG connections: concurrent START vs settle_abandoned thread reset
    tid_race = await _create_thread(ws_id, user_id, status="in_progress")
    eid_ab2 = await _insert_execution(
        str(auto["automation_id"]), status="running", thread_id=tid_race
    )
    await _exec_sql(
        "UPDATE automation_executions SET heartbeat_at = NOW() - INTERVAL '10 minutes' WHERE automation_execution_id = %s::uuid",
        (eid_ab2,),
    )
    new_run_id = str(uuid4())
    conn1_locked = asyncio.Event()
    release_conn1 = asyncio.Event()

    async def _concurrent_start() -> None:
        async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=False) as c1:
            async with c1.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO conversation_responses (
                        conversation_response_id, conversation_thread_id,
                        turn_index, status, created_at
                    )
                    VALUES (%s::uuid, %s::uuid, 0, 'in_progress', NOW())
                    """,
                    (new_run_id, tid_race),
                )
                await cur.execute(
                    """
                    UPDATE conversation_threads
                    SET current_status = 'in_progress', updated_at = NOW()
                    WHERE conversation_thread_id = %s::uuid
                    """,
                    (tid_race,),
                )
            conn1_locked.set()
            await release_conn1.wait()
            await c1.commit()

    start_task = asyncio.create_task(_concurrent_start())
    await conn1_locked.wait()

    ab_row2 = {
        **ab_row1,
        "automation_execution_id": eid_ab2,
        "conversation_thread_id": tid_race,
    }
    with patch.object(
        settle_mod,
        "WebhookClient",
        new=create_autospec(WebhookClient, return_value=webhook_client),
    ):
        settle_task = asyncio.create_task(
            settle_mod.settle_abandoned(auto, ab_row2, 300)
        )
        await asyncio.sleep(0.15)
        assert not settle_task.done()
        release_conn1.set()
        await start_task
        out2 = await settle_task
        await settle_mod.drain_tails()

    assert out2 is Outcome.INTERRUPTED
    trow_race = await _fetch_one(
        "SELECT current_status FROM conversation_threads WHERE conversation_thread_id = %s::uuid",
        (tid_race,),
    )
    assert trow_race["current_status"] == "in_progress"
