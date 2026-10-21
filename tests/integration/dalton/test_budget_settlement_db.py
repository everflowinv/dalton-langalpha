"""Integration tests for Dalton budget reservation settlement vs subagent `usage_settled_at` (J5d)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

import src.dalton.budget.service as budget_service
import src.dalton.database.budget as budget_db
from src.dalton.budget.service import (
    _local_day_bounds,
    reconcile_and_summarize_day,
)
from src.dalton.database.budget import upsert_budget_config
from src.server.database.conversation import create_thread
from src.server.database.conversation.usage import create_usage_record
from src.server.database.runs import credit_ledger, subagent_runs

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _seed_settled_parent_response(
    conn: Any,
    *,
    run_ref: str,
    thread_id: str,
    user_id: str,
    turn_index: int = 1,
    settled_age_minutes: int = 0,
) -> None:
    """Insert a `conversation_responses` row born `in_progress` and transition to `completed` with `usage_settled_at`."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO conversation_responses (
                conversation_response_id,
                conversation_thread_id,
                turn_index,
                status,
                user_id,
                created_at
            )
            VALUES (%s, %s, %s, 'in_progress', %s, NOW())
            """,
            (run_ref, thread_id, turn_index, user_id),
        )
        await cur.execute(
            """
            UPDATE conversation_responses
            SET status = 'completed',
                usage_settled_at = NOW() - make_interval(mins => %s)
            WHERE conversation_response_id = %s
            """,
            (int(settled_age_minutes), run_ref),
        )


async def _insert_reservation(
    conn: Any,
    *,
    user_id: str,
    run_ref: str,
    thread_id: str,
    workspace_id: str,
    state: str = "released",
    reserved_usd: Decimal = Decimal("0.250000"),
    observed_usd: Decimal = Decimal("0.000000"),
    settled_usd: Decimal | None = None,
    settle_source: str | None = None,
    settled_age_days: int = 0,
) -> None:
    b_day, _, _, _ = _local_day_bounds("Asia/Shanghai")
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO dalton_budget_reservations (
                user_id,
                budget_day,
                run_ref,
                thread_id,
                workspace_id,
                reserved_usd,
                observed_usd,
                state,
                expires_at,
                released_at,
                settled_at,
                settled_usd,
                settle_source
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                NOW() + INTERVAL '1 hour',
                CASE WHEN %s = 'released' THEN NOW() ELSE NULL END,
                CASE WHEN %s = 'settled' THEN NOW() - make_interval(days => %s) ELSE NULL END,
                %s,
                %s
            )
            """,
            (
                user_id,
                b_day,
                run_ref,
                thread_id,
                workspace_id,
                reserved_usd,
                observed_usd,
                state,
                state,
                state,
                int(settled_age_days),
                settled_usd,
                settle_source,
            ),
        )


async def _insert_usage_row(
    conn: Any,
    *,
    response_id: str,
    user_id: str,
    thread_id: str,
    workspace_id: str,
    msg_type: str,
    cash_usd: Decimal,
) -> None:
    """Insert a `conversation_usages` row whose `dalton_cash_usd` evaluates to `cash_usd`."""
    if cash_usd > Decimal(0):
        token_usage = {
            "by_model": {
                "deepseek-v4": {
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "total_cost": float(cash_usd),
                    "rates": [{"billing_type": "byok"}],
                }
            },
            "per_call_costs": [
                {
                    "model_name": "deepseek-v4",
                    "billing_type": "byok",
                    "cost": str(cash_usd),
                    "input_tokens": 1000,
                    "output_tokens": 500,
                }
            ],
        }
    else:
        token_usage = {
            "by_model": {},
            "per_call_costs": [
                {
                    "model_name": "claude-opus-5-5-oauth",
                    "billing_type": "oauth",
                    "cost": "0",
                    "input_tokens": 500,
                    "output_tokens": 100,
                }
            ],
        }

    await create_usage_record(
        {
            "conversation_usage_id": str(uuid.uuid4()),
            "conversation_response_id": response_id,
            "user_id": user_id,
            "conversation_thread_id": thread_id,
            "workspace_id": workspace_id,
            "msg_type": msg_type,
            "status": "completed",
            "token_usage": token_usage,
            "infrastructure_usage": {},
            "token_credits": 0.0,
            "infrastructure_credits": 0.0,
            "total_credits": 0.0,
            "is_byok": True,
            "created_at": datetime.now(UTC),
        },
        conn=conn,
    )


async def _fetch_reservation_row(conn: Any, run_ref: str) -> dict[str, Any]:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT state, reserved_usd, observed_usd, settled_usd, settle_source
            FROM dalton_budget_reservations
            WHERE run_ref = %s
            """,
            (run_ref,),
        )
        row = await cur.fetchone()
        assert row is not None
        return dict(row)


async def test_completed_subagent_without_usage_settled_at_blocks_early_zero_settlement(
    seed_workspace,
    patched_get_db_connection,
):
    """Case 1: A `released` reservation with parent `usage_settled_at` + $0 `ptc` usage row +
    `completed` subagent whose `usage_settled_at` is NULL must NOT settle; `spent_usd >= reserved_usd`.
    Once the subagent's `task` usage row is inserted and `usage_settled_at` is stamped, reconcile
    settles the reservation at the `task` usage cash amount."""
    from src.server.database.pool import get_db_connection

    _ = (budget_service, budget_db)
    user_id = str(seed_workspace["user_id"])
    ws_id = str(seed_workspace["workspace_id"])
    await upsert_budget_config(user_id, Decimal("30.00"), None, "Asia/Shanghai")

    thread_id = str(uuid.uuid4())
    await create_thread(
        conversation_thread_id=thread_id,
        workspace_id=ws_id,
        current_status="in_progress",
        msg_type="ptc",
    )

    run_ref = str(uuid.uuid4())
    task_run_id = str(uuid.uuid4())
    async with get_db_connection() as conn:
        await _seed_settled_parent_response(
            conn, run_ref=run_ref, thread_id=thread_id, user_id=user_id, turn_index=1
        )
        await _insert_reservation(
            conn,
            user_id=user_id,
            run_ref=run_ref,
            thread_id=thread_id,
            workspace_id=ws_id,
            state="released",
            reserved_usd=Decimal("0.250000"),
        )
        await _insert_usage_row(
            conn,
            response_id=run_ref,
            user_id=user_id,
            thread_id=thread_id,
            workspace_id=ws_id,
            msg_type="ptc",
            cash_usd=Decimal(0),
        )
        await subagent_runs.start_task_run(
            task_run_id=task_run_id,
            thread_id=thread_id,
            task_id="task-1",
            cause="init",
            parent_run_id=run_ref,
            conn=conn,
        )
        await subagent_runs.finalize_task_run(
            task_run_id=task_run_id, status="completed", conn=conn
        )

    async with get_db_connection() as conn:
        async with conn.transaction():
            summary_before = await reconcile_and_summarize_day(conn, user_id)
        resv_before = await _fetch_reservation_row(conn, run_ref)

    assert resv_before["state"] == "released"
    assert resv_before["settled_usd"] is None
    assert summary_before["_decimals"]["spent_usd"] >= Decimal("0.250000")

    task_cash = Decimal("0.034085")
    async with get_db_connection() as conn, conn.transaction():
        await _insert_usage_row(
            conn,
            response_id=run_ref,
            user_id=user_id,
            thread_id=thread_id,
            workspace_id=ws_id,
            msg_type="task",
            cash_usd=task_cash,
        )
        assert await credit_ledger.settle_task_run(task_run_id, conn=conn)

    async with get_db_connection() as conn:
        async with conn.transaction():
            summary_after = await reconcile_and_summarize_day(conn, user_id)
        resv_after = await _fetch_reservation_row(conn, run_ref)

    assert resv_after["state"] == "settled"
    assert resv_after["settle_source"] == "usage"
    assert resv_after["settled_usd"] == task_cash
    assert summary_after["_decimals"]["settled_usd"] == task_cash
    assert summary_after["_decimals"]["spent_usd"] == task_cash


async def test_already_settled_zero_reservation_ratchets_up_when_late_task_usage_arrives(
    seed_workspace,
    patched_get_db_connection,
):
    """Case 2: A recently settled $0 reservation (`state='settled'`, `settled_usd=0`) ratchets up
    to the actual cash usage when late `task` usage rows exist under the same `run_ref`, while a
    reservation settled 3 days ago (outside `_LATE_USAGE_RATCHET_WINDOW`) does NOT ratchet up."""
    from src.server.database.pool import get_db_connection

    user_id = str(seed_workspace["user_id"])
    ws_id = str(seed_workspace["workspace_id"])
    await upsert_budget_config(user_id, Decimal("30.00"), None, "Asia/Shanghai")

    thread_id = str(uuid.uuid4())
    await create_thread(
        conversation_thread_id=thread_id,
        workspace_id=ws_id,
        current_status="completed",
        msg_type="ptc",
    )

    run_ref = str(uuid.uuid4())
    old_run_ref = str(uuid.uuid4())
    late_cash = Decimal("0.034085")
    async with get_db_connection() as conn:
        await _seed_settled_parent_response(
            conn, run_ref=run_ref, thread_id=thread_id, user_id=user_id, turn_index=1
        )
        await _insert_reservation(
            conn,
            user_id=user_id,
            run_ref=run_ref,
            thread_id=thread_id,
            workspace_id=ws_id,
            state="settled",
            reserved_usd=Decimal("0.250000"),
            settled_usd=Decimal("0.000000"),
            settle_source="usage",
        )
        await _insert_usage_row(
            conn,
            response_id=run_ref,
            user_id=user_id,
            thread_id=thread_id,
            workspace_id=ws_id,
            msg_type="task",
            cash_usd=late_cash,
        )

        await _seed_settled_parent_response(
            conn, run_ref=old_run_ref, thread_id=thread_id, user_id=user_id, turn_index=2
        )
        await _insert_reservation(
            conn,
            user_id=user_id,
            run_ref=old_run_ref,
            thread_id=thread_id,
            workspace_id=ws_id,
            state="settled",
            reserved_usd=Decimal("0.250000"),
            settled_usd=Decimal("0.000000"),
            settle_source="usage",
            settled_age_days=3,
        )
        await _insert_usage_row(
            conn,
            response_id=old_run_ref,
            user_id=user_id,
            thread_id=thread_id,
            workspace_id=ws_id,
            msg_type="task",
            cash_usd=Decimal("0.050000"),
        )

    async with get_db_connection() as conn:
        async with conn.transaction():
            summary = await reconcile_and_summarize_day(conn, user_id)
        resv = await _fetch_reservation_row(conn, run_ref)
        old_resv = await _fetch_reservation_row(conn, old_run_ref)

    assert resv["state"] == "settled"
    assert resv["settle_source"] == "usage"
    assert resv["settled_usd"] == late_cash
    assert old_resv["state"] == "settled"
    assert old_resv["settled_usd"] == Decimal("0.000000")
    assert summary["_decimals"]["settled_usd"] == late_cash
    assert summary["_decimals"]["spent_usd"] == late_cash


async def test_abandoned_subagent_settles_conservatively_only_after_30m_grace(
    seed_workspace,
    patched_get_db_connection,
):
    """Case 3: Abandoned unsettled subagent conservative settlement requires BOTH parent
    `usage_settled_at` and every unsettled child's `finalized_at` to be older than 30 minutes."""
    from src.server.database.pool import get_db_connection

    user_id = str(seed_workspace["user_id"])
    ws_id = str(seed_workspace["workspace_id"])
    await upsert_budget_config(user_id, Decimal("30.00"), None, "Asia/Shanghai")

    thread_id = str(uuid.uuid4())
    await create_thread(
        conversation_thread_id=thread_id,
        workspace_id=ws_id,
        current_status="completed",
        msg_type="ptc",
    )

    run_ref = str(uuid.uuid4())
    task_run_id = str(uuid.uuid4())
    young_task_run_id = str(uuid.uuid4())
    async with get_db_connection() as conn:
        await _seed_settled_parent_response(
            conn,
            run_ref=run_ref,
            thread_id=thread_id,
            user_id=user_id,
            turn_index=1,
            settled_age_minutes=31,
        )
        await _insert_reservation(
            conn,
            user_id=user_id,
            run_ref=run_ref,
            thread_id=thread_id,
            workspace_id=ws_id,
            state="released",
            reserved_usd=Decimal("0.250000"),
            observed_usd=Decimal("0.400000"),
        )
        await _insert_usage_row(
            conn,
            response_id=run_ref,
            user_id=user_id,
            thread_id=thread_id,
            workspace_id=ws_id,
            msg_type="ptc",
            cash_usd=Decimal("0.050000"),
        )
        await subagent_runs.start_task_run(
            task_run_id=task_run_id,
            thread_id=thread_id,
            task_id="task-abandoned",
            cause="init",
            parent_run_id=run_ref,
            conn=conn,
        )
        await subagent_runs.finalize_task_run(
            task_run_id=task_run_id, status="completed", conn=conn
        )
        # (a) Child finalized 5 minutes ago (parent 31m ago) -> must NOT settle
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE subagent_runs SET finalized_at = NOW() - INTERVAL '5 minutes' WHERE task_run_id = %s",
                (task_run_id,),
            )

    async with get_db_connection() as conn:
        async with conn.transaction():
            await reconcile_and_summarize_day(conn, user_id)
        resv_recent_child = await _fetch_reservation_row(conn, run_ref)

    assert resv_recent_child["state"] == "released"
    assert resv_recent_child["settled_usd"] is None

    # (b) Child finalized 31 minutes ago, but parent settled only 5 minutes ago -> must NOT settle
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE subagent_runs SET finalized_at = NOW() - INTERVAL '31 minutes' WHERE task_run_id = %s",
                (task_run_id,),
            )
            await cur.execute(
                "UPDATE conversation_responses SET usage_settled_at = NOW() - INTERVAL '5 minutes' WHERE conversation_response_id = %s",
                (run_ref,),
            )
        async with conn.transaction():
            await reconcile_and_summarize_day(conn, user_id)
        resv_recent_parent = await _fetch_reservation_row(conn, run_ref)

    assert resv_recent_parent["state"] == "released"
    assert resv_recent_parent["settled_usd"] is None

    # (c) Parent settled 31m ago + one old unsettled child (31m) + one young unsettled child (5m) -> must NOT settle
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE conversation_responses SET usage_settled_at = NOW() - INTERVAL '31 minutes' WHERE conversation_response_id = %s",
                (run_ref,),
            )
        await subagent_runs.start_task_run(
            task_run_id=young_task_run_id,
            thread_id=thread_id,
            task_id="task-abandoned-young",
            cause="init",
            parent_run_id=run_ref,
            conn=conn,
        )
        await subagent_runs.finalize_task_run(
            task_run_id=young_task_run_id, status="completed", conn=conn
        )
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE subagent_runs SET finalized_at = NOW() - INTERVAL '5 minutes' WHERE task_run_id = %s",
                (young_task_run_id,),
            )
        async with conn.transaction():
            await reconcile_and_summarize_day(conn, user_id)
        resv_mixed_children = await _fetch_reservation_row(conn, run_ref)

    assert resv_mixed_children["state"] == "released"
    assert resv_mixed_children["settled_usd"] is None

    # (d) Both children 31m ago and parent 31m ago -> settles conservatively at GREATEST(0.25, 0.40, 0.05) = 0.40
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE subagent_runs SET finalized_at = NOW() - INTERVAL '31 minutes' WHERE task_run_id = %s",
                (young_task_run_id,),
            )
        async with conn.transaction():
            summary_abandoned = await reconcile_and_summarize_day(conn, user_id)
        resv_abandoned = await _fetch_reservation_row(conn, run_ref)

    assert resv_abandoned["state"] == "settled"
    assert resv_abandoned["settle_source"] == "conservative"
    assert resv_abandoned["settled_usd"] == Decimal("0.400000")
    assert summary_abandoned["_decimals"]["settled_usd"] == Decimal("0.400000")


async def test_in_progress_subagent_blocks_settlement(
    seed_workspace,
    patched_get_db_connection,
):
    """Case 4: A subagent run in `status='in_progress'` prevents reservation settlement."""
    from src.server.database.pool import get_db_connection

    user_id = str(seed_workspace["user_id"])
    ws_id = str(seed_workspace["workspace_id"])
    await upsert_budget_config(user_id, Decimal("30.00"), None, "Asia/Shanghai")

    thread_id = str(uuid.uuid4())
    await create_thread(
        conversation_thread_id=thread_id,
        workspace_id=ws_id,
        current_status="in_progress",
        msg_type="ptc",
    )

    run_ref = str(uuid.uuid4())
    task_run_id = str(uuid.uuid4())
    async with get_db_connection() as conn:
        # Parent settled past the grace window, so only the in_progress child can hold settlement.
        await _seed_settled_parent_response(
            conn, run_ref=run_ref, thread_id=thread_id, user_id=user_id, turn_index=1, settled_age_minutes=31
        )
        await _insert_reservation(
            conn,
            user_id=user_id,
            run_ref=run_ref,
            thread_id=thread_id,
            workspace_id=ws_id,
            state="released",
            reserved_usd=Decimal("0.250000"),
        )
        await _insert_usage_row(
            conn,
            response_id=run_ref,
            user_id=user_id,
            thread_id=thread_id,
            workspace_id=ws_id,
            msg_type="ptc",
            cash_usd=Decimal(0),
        )
        await subagent_runs.start_task_run(
            task_run_id=task_run_id,
            thread_id=thread_id,
            task_id="task-live",
            cause="init",
            parent_run_id=run_ref,
            conn=conn,
        )

    async with get_db_connection() as conn:
        async with conn.transaction():
            summary = await reconcile_and_summarize_day(conn, user_id)
        resv = await _fetch_reservation_row(conn, run_ref)

    assert resv["state"] == "released"
    assert resv["settled_usd"] is None
    assert summary["_decimals"]["spent_usd"] >= Decimal("0.250000")
