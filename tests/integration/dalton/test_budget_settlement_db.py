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
    task_run_id: str | None = None,
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
    if task_run_id is not None:
        token_usage["task_run_id"] = task_run_id

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


async def test_workflow_driver_with_complete_usage_settles_at_actual_usage(
    seed_workspace,
    patched_get_db_connection,
):
    """Case 5: After 30m grace, a finished RunWorkflow driver (`workflow_driver=True`)
    whose workflow internal children (`workflow_driver_run_id`) and direct siblings ALL
    have matching `task_run_id` usage rows and valid `per_call_costs` settles by actual
    usage ($0 for OAuth, actual cash total for cash runs), while never dropping `observed_usd`."""
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

    # (a) OAuth workflow + OAuth workflow child -> $0 usage
    run_oauth = str(uuid.uuid4())
    wf_run_oauth = str(uuid.uuid4())
    child_run_oauth = str(uuid.uuid4())

    # (b) Workflow + cash workflow child ($0.03) + settled cash direct sibling ($0.05) -> $0.08 usage
    run_cash = str(uuid.uuid4())
    wf_run_cash = str(uuid.uuid4())
    child_run_cash = str(uuid.uuid4())
    sibling_run_cash = str(uuid.uuid4())
    child_cash = Decimal("0.030000")
    sibling_cash = Decimal("0.050000")

    # (c) Workflow with complete usage rows + observed_usd ($0.40) > usage_usd ($0) -> preserves $0.40
    run_observed = str(uuid.uuid4())
    wf_run_observed = str(uuid.uuid4())

    async with get_db_connection() as conn:
        for turn_idx, run_ref, observed in [
            (1, run_oauth, Decimal("0.000000")),
            (2, run_cash, child_cash + sibling_cash),
            (3, run_observed, Decimal("0.400000")),
        ]:
            await _seed_settled_parent_response(
                conn,
                run_ref=run_ref,
                thread_id=thread_id,
                user_id=user_id,
                turn_index=turn_idx,
                settled_age_minutes=31,
            )
            await _insert_reservation(
                conn,
                user_id=user_id,
                run_ref=run_ref,
                thread_id=thread_id,
                workspace_id=ws_id,
                state="released",
                reserved_usd=Decimal("1.000000"),
                observed_usd=observed,
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

        # Admitted workflow drivers
        for wf_run_id, parent_ref, task_id in [
            (wf_run_oauth, run_oauth, "wf-oauth-1"),
            (wf_run_cash, run_cash, "wf-cash-1"),
            (wf_run_observed, run_observed, "wf-obs-1"),
        ]:
            row = await subagent_runs.start_task_run(
                task_run_id=wf_run_id,
                thread_id=thread_id,
                task_id=task_id,
                cause="init",
                subagent_type="workflow",
                parent_run_id=parent_ref,
                workflow_driver=True,
                conn=conn,
            )
            assert row["replay_facts"] == {"workflow_driver": True}
            await subagent_runs.finalize_task_run(
                task_run_id=wf_run_id, status="completed", conn=conn
            )

        # Workflow internal children (parent_run_id=None, linked via workflow_driver_run_id)
        for child_run_id, driver_run_id, parent_ref, task_id, cash in [
            (child_run_oauth, wf_run_oauth, run_oauth, "wf-child-oauth-1", Decimal(0)),
            (child_run_cash, wf_run_cash, run_cash, "wf-child-cash-1", child_cash),
        ]:
            c_row = await subagent_runs.start_task_run(
                task_run_id=child_run_id,
                thread_id=thread_id,
                task_id=task_id,
                cause="init",
                subagent_type="research",
                parent_run_id=None,
                workflow_driver_run_id=driver_run_id,
                conn=conn,
            )
            assert c_row["replay_facts"] == {"workflow_driver_run_id": driver_run_id}
            await subagent_runs.finalize_task_run(
                task_run_id=child_run_id, status="completed", conn=conn
            )
            await _insert_usage_row(
                conn,
                response_id=parent_ref,
                user_id=user_id,
                thread_id=thread_id,
                workspace_id=ws_id,
                msg_type="task",
                cash_usd=cash,
                task_run_id=child_run_id,
            )
            assert await credit_ledger.settle_task_run(child_run_id, conn=conn)

        # Settled direct sibling on run_cash with matching task_run_id usage row
        await subagent_runs.start_task_run(
            task_run_id=sibling_run_cash,
            thread_id=thread_id,
            task_id="task-sibling-1",
            cause="init",
            subagent_type="research",
            parent_run_id=run_cash,
            conn=conn,
        )
        await subagent_runs.finalize_task_run(
            task_run_id=sibling_run_cash, status="completed", conn=conn
        )
        await _insert_usage_row(
            conn,
            response_id=run_cash,
            user_id=user_id,
            thread_id=thread_id,
            workspace_id=ws_id,
            msg_type="task",
            cash_usd=sibling_cash,
            task_run_id=sibling_run_cash,
        )
        assert await credit_ledger.settle_task_run(sibling_run_cash, conn=conn)

        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE subagent_runs
                SET finalized_at = NOW() - INTERVAL '31 minutes'
                WHERE task_run_id = ANY(%s)
                """,
                (
                    [
                        wf_run_oauth,
                        child_run_oauth,
                        wf_run_cash,
                        child_run_cash,
                        sibling_run_cash,
                        wf_run_observed,
                    ],
                ),
            )

    async with get_db_connection() as conn:
        async with conn.transaction():
            summary = await reconcile_and_summarize_day(conn, user_id)
        resv_oauth = await _fetch_reservation_row(conn, run_oauth)
        resv_cash = await _fetch_reservation_row(conn, run_cash)
        resv_observed = await _fetch_reservation_row(conn, run_observed)

    assert resv_oauth["state"] == "settled"
    assert resv_oauth["settle_source"] == "usage"
    assert resv_oauth["settled_usd"] == Decimal("0")

    assert resv_cash["state"] == "settled"
    assert resv_cash["settle_source"] == "usage"
    assert resv_cash["settled_usd"] == child_cash + sibling_cash

    assert resv_observed["state"] == "settled"
    assert resv_observed["settle_source"] == "conservative"
    assert resv_observed["settled_usd"] == Decimal("0.400000")

    expected_total = child_cash + sibling_cash + Decimal("0.400000")
    assert summary["_decimals"]["settled_usd"] == expected_total
    assert summary["_decimals"]["spent_usd"] == expected_total


async def test_workflow_driver_with_missing_or_live_workflow_child_usage_stays_conservative(
    seed_workspace,
    patched_get_db_connection,
):
    """Case 6: If ANY workflow internal child linked via `workflow_driver_run_id` is
    missing its `task_run_id` usage row (even if `usage_settled_at` was stamped by
    recovery sweep) or is still `in_progress`, Step 2 MUST settle conservatively."""
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

    run_missing_child = str(uuid.uuid4())
    wf_missing = str(uuid.uuid4())
    child_missing = str(uuid.uuid4())

    run_live_child = str(uuid.uuid4())
    wf_live = str(uuid.uuid4())
    child_live = str(uuid.uuid4())

    async with get_db_connection() as conn:
        for turn_idx, run_ref in [(1, run_missing_child), (2, run_live_child)]:
            await _seed_settled_parent_response(
                conn,
                run_ref=run_ref,
                thread_id=thread_id,
                user_id=user_id,
                turn_index=turn_idx,
                settled_age_minutes=31,
            )
            await _insert_reservation(
                conn,
                user_id=user_id,
                run_ref=run_ref,
                thread_id=thread_id,
                workspace_id=ws_id,
                state="released",
                reserved_usd=Decimal("1.000000"),
                observed_usd=Decimal("0.000000"),
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

        # (a) Finished workflow child WITHOUT conversation_usages row (even with usage_settled_at stamped)
        await subagent_runs.start_task_run(
            task_run_id=wf_missing,
            thread_id=thread_id,
            task_id="wf-missing-1",
            cause="init",
            subagent_type="workflow",
            parent_run_id=run_missing_child,
            workflow_driver=True,
            conn=conn,
        )
        await subagent_runs.finalize_task_run(
            task_run_id=wf_missing, status="completed", conn=conn
        )
        await subagent_runs.start_task_run(
            task_run_id=child_missing,
            thread_id=thread_id,
            task_id="child-missing-1",
            cause="init",
            subagent_type="research",
            parent_run_id=None,
            workflow_driver_run_id=wf_missing,
            conn=conn,
        )
        await subagent_runs.finalize_task_run(
            task_run_id=child_missing, status="completed", conn=conn
        )
        assert await credit_ledger.settle_task_run(child_missing, conn=conn)

        # (b) Workflow child still in_progress after driver finalized
        await subagent_runs.start_task_run(
            task_run_id=wf_live,
            thread_id=thread_id,
            task_id="wf-live-child-1",
            cause="init",
            subagent_type="workflow",
            parent_run_id=run_live_child,
            workflow_driver=True,
            conn=conn,
        )
        await subagent_runs.finalize_task_run(
            task_run_id=wf_live, status="completed", conn=conn
        )
        await subagent_runs.start_task_run(
            task_run_id=child_live,
            thread_id=thread_id,
            task_id="child-live-1",
            cause="init",
            subagent_type="research",
            parent_run_id=None,
            workflow_driver_run_id=wf_live,
            conn=conn,
        )

        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE subagent_runs
                SET finalized_at = NOW() - INTERVAL '31 minutes'
                WHERE task_run_id = ANY(%s)
                """,
                ([wf_missing, child_missing, wf_live],),
            )

    async with get_db_connection() as conn:
        async with conn.transaction():
            await reconcile_and_summarize_day(conn, user_id)
        resv_missing = await _fetch_reservation_row(conn, run_missing_child)
        resv_live = await _fetch_reservation_row(conn, run_live_child)

    assert resv_missing["state"] == "settled"
    assert resv_missing["settle_source"] == "conservative"
    assert resv_missing["settled_usd"] == Decimal("1.000000")

    assert resv_live["state"] == "settled"
    assert resv_live["settle_source"] == "conservative"
    assert resv_live["settled_usd"] == Decimal("1.000000")


async def test_direct_subagent_stamped_by_recovery_without_usage_row_stays_conservative(
    seed_workspace,
    patched_get_db_connection,
):
    """Case 7: A direct Task sibling whose `usage_settled_at` was stamped by recovery
    sweep (`settle_abandoned`) without inserting a matching `task_run_id` row in
    `conversation_usages` MUST keep the reservation conservative."""
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
    wf_run_id = str(uuid.uuid4())
    direct_swept_run = str(uuid.uuid4())

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
            reserved_usd=Decimal("1.000000"),
            observed_usd=Decimal("0.000000"),
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
            task_run_id=wf_run_id,
            thread_id=thread_id,
            task_id="wf-sweep-1",
            cause="init",
            subagent_type="workflow",
            parent_run_id=run_ref,
            workflow_driver=True,
            conn=conn,
        )
        await subagent_runs.finalize_task_run(
            task_run_id=wf_run_id, status="completed", conn=conn
        )

        # Direct Task finalized + stamped usage_settled_at (simulating settle_abandoned)
        # with NO conversation_usages row for direct_swept_run.
        await subagent_runs.start_task_run(
            task_run_id=direct_swept_run,
            thread_id=thread_id,
            task_id="task-swept-1",
            cause="init",
            subagent_type="research",
            parent_run_id=run_ref,
            conn=conn,
        )
        await subagent_runs.finalize_task_run(
            task_run_id=direct_swept_run, status="completed", conn=conn
        )
        assert await credit_ledger.settle_task_run(direct_swept_run, conn=conn)

        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE subagent_runs
                SET finalized_at = NOW() - INTERVAL '31 minutes'
                WHERE task_run_id = ANY(%s)
                """,
                ([wf_run_id, direct_swept_run],),
            )

    async with get_db_connection() as conn:
        async with conn.transaction():
            await reconcile_and_summarize_day(conn, user_id)
        resv = await _fetch_reservation_row(conn, run_ref)

    assert resv["state"] == "settled"
    assert resv["settle_source"] == "conservative"
    assert resv["settled_usd"] == Decimal("1.000000")


async def test_byok_usage_row_missing_per_call_costs_stays_conservative(
    seed_workspace,
    patched_get_db_connection,
):
    """Case 8: If ANY `is_byok=True` usage row on `r.run_ref` is missing `per_call_costs`
    (`token_usage IS NULL` or missing `per_call_costs` key alongside a valid child row),
    Step 2 MUST treat usage as incomplete and settle conservatively."""
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

    run_null_tu = str(uuid.uuid4())
    wf_null_tu = str(uuid.uuid4())

    run_missing_pcc = str(uuid.uuid4())
    wf_missing_pcc = str(uuid.uuid4())
    child_valid = str(uuid.uuid4())

    async with get_db_connection() as conn:
        for turn_idx, run_ref in [(1, run_null_tu), (2, run_missing_pcc)]:
            await _seed_settled_parent_response(
                conn,
                run_ref=run_ref,
                thread_id=thread_id,
                user_id=user_id,
                turn_index=turn_idx,
                settled_age_minutes=31,
            )
            await _insert_reservation(
                conn,
                user_id=user_id,
                run_ref=run_ref,
                thread_id=thread_id,
                workspace_id=ws_id,
                state="released",
                reserved_usd=Decimal("1.000000"),
                observed_usd=Decimal("0.000000"),
            )

        # (a) BYOK parent row with token_usage=None (pricing exception fallback)
        await create_usage_record(
            {
                "conversation_usage_id": str(uuid.uuid4()),
                "conversation_response_id": run_null_tu,
                "user_id": user_id,
                "conversation_thread_id": thread_id,
                "workspace_id": ws_id,
                "msg_type": "ptc",
                "status": "completed",
                "token_usage": None,
                "infrastructure_usage": {},
                "token_credits": 0.0,
                "infrastructure_credits": 0.0,
                "total_credits": 0.0,
                "is_byok": True,
                "created_at": datetime.now(UTC),
            },
            conn=conn,
        )
        await subagent_runs.start_task_run(
            task_run_id=wf_null_tu,
            thread_id=thread_id,
            task_id="wf-byok-null",
            cause="init",
            subagent_type="workflow",
            parent_run_id=run_null_tu,
            workflow_driver=True,
            conn=conn,
        )
        await subagent_runs.finalize_task_run(
            task_run_id=wf_null_tu, status="completed", conn=conn
        )

        # (b) BYOK parent row missing per_call_costs key alongside a valid child row
        await create_usage_record(
            {
                "conversation_usage_id": str(uuid.uuid4()),
                "conversation_response_id": run_missing_pcc,
                "user_id": user_id,
                "conversation_thread_id": thread_id,
                "workspace_id": ws_id,
                "msg_type": "ptc",
                "status": "completed",
                "token_usage": {"by_model": {}},
                "infrastructure_usage": {},
                "token_credits": 0.0,
                "infrastructure_credits": 0.0,
                "total_credits": 0.0,
                "is_byok": True,
                "created_at": datetime.now(UTC),
            },
            conn=conn,
        )
        await subagent_runs.start_task_run(
            task_run_id=wf_missing_pcc,
            thread_id=thread_id,
            task_id="wf-byok-nopcc",
            cause="init",
            subagent_type="workflow",
            parent_run_id=run_missing_pcc,
            workflow_driver=True,
            conn=conn,
        )
        await subagent_runs.finalize_task_run(
            task_run_id=wf_missing_pcc, status="completed", conn=conn
        )
        await subagent_runs.start_task_run(
            task_run_id=child_valid,
            thread_id=thread_id,
            task_id="child-valid-pcc",
            cause="init",
            subagent_type="research",
            parent_run_id=None,
            workflow_driver_run_id=wf_missing_pcc,
            conn=conn,
        )
        await subagent_runs.finalize_task_run(
            task_run_id=child_valid, status="completed", conn=conn
        )
        await _insert_usage_row(
            conn,
            response_id=run_missing_pcc,
            user_id=user_id,
            thread_id=thread_id,
            workspace_id=ws_id,
            msg_type="task",
            cash_usd=Decimal(0),
            task_run_id=child_valid,
        )
        assert await credit_ledger.settle_task_run(child_valid, conn=conn)

        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE subagent_runs
                SET finalized_at = NOW() - INTERVAL '31 minutes'
                WHERE task_run_id = ANY(%s)
                """,
                ([wf_null_tu, wf_missing_pcc, child_valid],),
            )

    async with get_db_connection() as conn:
        async with conn.transaction():
            await reconcile_and_summarize_day(conn, user_id)
        resv_null = await _fetch_reservation_row(conn, run_null_tu)
        resv_nopcc = await _fetch_reservation_row(conn, run_missing_pcc)

    assert resv_null["state"] == "settled"
    assert resv_null["settle_source"] == "conservative"
    assert resv_null["settled_usd"] == Decimal("1.000000")

    assert resv_nopcc["state"] == "settled"
    assert resv_nopcc["settle_source"] == "conservative"
    assert resv_nopcc["settled_usd"] == Decimal("1.000000")


async def test_historical_unmarked_driver_stays_conservative_and_in_progress_driver_blocks(
    seed_workspace,
    patched_get_db_connection,
):
    """Case 9: (a) A historical workflow driver row without `replay_facts.workflow_driver`
    settles conservatively ($1.00); (b) an `in_progress` workflow driver blocks settlement."""
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

    run_hist = str(uuid.uuid4())
    wf_hist = str(uuid.uuid4())

    run_live = str(uuid.uuid4())
    wf_live = str(uuid.uuid4())

    async with get_db_connection() as conn:
        for turn_idx, run_ref in [(1, run_hist), (2, run_live)]:
            await _seed_settled_parent_response(
                conn,
                run_ref=run_ref,
                thread_id=thread_id,
                user_id=user_id,
                turn_index=turn_idx,
                settled_age_minutes=31,
            )
            await _insert_reservation(
                conn,
                user_id=user_id,
                run_ref=run_ref,
                thread_id=thread_id,
                workspace_id=ws_id,
                state="released",
                reserved_usd=Decimal("1.000000"),
                observed_usd=Decimal("0.000000"),
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

        # (a) Historical unmarked driver (subagent_type='workflow', replay_facts=None)
        hist_row = await subagent_runs.start_task_run(
            task_run_id=wf_hist,
            thread_id=thread_id,
            task_id="wf-hist-1",
            cause="init",
            subagent_type="workflow",
            parent_run_id=run_hist,
            workflow_driver=False,
            conn=conn,
        )
        assert hist_row["replay_facts"] is None
        await subagent_runs.finalize_task_run(
            task_run_id=wf_hist, status="completed", conn=conn
        )
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE subagent_runs SET finalized_at = NOW() - INTERVAL '31 minutes' WHERE task_run_id = %s",
                (wf_hist,),
            )

        # (b) In-progress marked workflow driver
        await subagent_runs.start_task_run(
            task_run_id=wf_live,
            thread_id=thread_id,
            task_id="wf-live-1",
            cause="init",
            subagent_type="workflow",
            parent_run_id=run_live,
            workflow_driver=True,
            conn=conn,
        )

    async with get_db_connection() as conn:
        async with conn.transaction():
            summary = await reconcile_and_summarize_day(conn, user_id)
        resv_hist = await _fetch_reservation_row(conn, run_hist)
        resv_live = await _fetch_reservation_row(conn, run_live)

    assert resv_hist["state"] == "settled"
    assert resv_hist["settle_source"] == "conservative"
    assert resv_hist["settled_usd"] == Decimal("1.000000")

    assert resv_live["state"] == "released"
    assert resv_live["settled_usd"] is None
    assert summary["_decimals"]["spent_usd"] >= Decimal("2.000000")
