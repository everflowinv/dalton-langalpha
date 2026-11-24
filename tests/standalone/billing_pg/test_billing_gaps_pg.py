"""Real-PostgreSQL integration tests for billing gap SQL (`reconcile_reservations_sql`,
secondary `usage_settled_at` stamping in `finalize_run`, bounded fallback settlement,
`credit_ledger.settle_abandoned`, and `fetch_unreserved_usage_today`).

Lives in its own standalone directory (`tests/standalone/billing_pg/`) with its own
`conftest.py`, completely isolated from `tests/integration/conftest.py` and
`tests/integration/dalton/conftest.py`. Skipped automatically when `DALTON_TEST_PG_DSN`
is unset. Creates an isolated temporary schema with the minimal tables and
`dalton_cash_usd` function, executes the real SQL statements against PostgreSQL,
and drops the schema on teardown without touching `public` or `TEST_DB_*`.

Run against a throwaway PostgreSQL container:
    DALTON_TEST_PG_DSN="postgresql://postgres:postgres@127.0.0.1:5432/postgres" \\
        .venv/bin/python -m pytest -v -p no:cacheprovider -m integration \\
        tests/standalone/billing_pg/test_billing_gaps_pg.py
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import create_autospec

import pytest
from psycopg.rows import dict_row

from src.dalton.database import budget as budget_db
from src.server.database import conversation as qr_db
from src.server.database.runs import credit_ledger as credit_ledger_db
from src.server.database.runs import lifecycle as lifecycle_db
from src.server.database.runs.lifecycle import RunOutcome, finalize_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_PG_DSN = os.environ.get("DALTON_TEST_PG_DSN", "").strip()

if not _PG_DSN:
    pytest.skip(
        "DALTON_TEST_PG_DSN is not set; skipping real-PostgreSQL billing gap SQL integration tests",
        allow_module_level=True,
    )


def _patch_non_sql_finalize_hooks(monkeypatch) -> None:
    monkeypatch.setattr(
        qr_db,
        "update_thread_status",
        create_autospec(qr_db.update_thread_status, return_value=True),
    )
    monkeypatch.setattr(
        qr_db,
        "_sync_provenance_for_response",
        create_autospec(qr_db._sync_provenance_for_response, return_value=None),
    )
    monkeypatch.setattr(
        lifecycle_db,
        "build_finalize_jobs_from_run_row",
        create_autospec(
            lifecycle_db.build_finalize_jobs_from_run_row,
            return_value=lambda _status: [],
        ),
    )
    monkeypatch.setattr(
        lifecycle_db,
        "release_deferred_notifications",
        create_autospec(lifecycle_db.release_deferred_notifications, return_value=0),
    )
    monkeypatch.setattr(
        lifecycle_db,
        "settle_run_steers",
        create_autospec(lifecycle_db.settle_run_steers, return_value=0),
    )


async def _insert_response(
    conn: Any,
    *,
    run_id: str,
    thread_id: str,
    user_id: str | None,
    status: str = "in_progress",
    in_flight_credits: Decimal = Decimal("0"),
    usage_settled_at: datetime | None = None,
    created_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO conversation_responses (
                conversation_response_id, conversation_thread_id, user_id,
                status, in_flight_credits, usage_settled_at, created_at, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, NOW()), %s::jsonb)
            """,
            (
                run_id,
                thread_id,
                user_id,
                status,
                in_flight_credits,
                usage_settled_at,
                created_at,
                json.dumps(metadata or {}),
            ),
        )


async def _insert_reservation(
    conn: Any,
    *,
    user_id: str,
    run_ref: str,
    thread_id: str,
    workspace_id: str,
    reserved_usd: Decimal = Decimal("1.000000"),
    observed_usd: Decimal = Decimal("0.000000"),
    state: str = "released",
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO dalton_budget_reservations (
                user_id, budget_day, run_ref, thread_id, workspace_id,
                reserved_usd, observed_usd, state, expires_at, released_at
            )
            VALUES (
                %s, CURRENT_DATE, %s, %s, %s, %s, %s, %s,
                NOW() + INTERVAL '1 hour',
                CASE WHEN %s = 'released' THEN NOW() ELSE NULL END
            )
            """,
            (user_id, run_ref, thread_id, workspace_id, reserved_usd, observed_usd, state, state),
        )


async def _insert_usage(
    conn: Any,
    *,
    response_id: str,
    user_id: str,
    thread_id: str,
    workspace_id: str,
    msg_type: str = "ptc",
    token_usage: dict[str, Any] | None = None,
    is_byok: bool = True,
    total_credits: Decimal = Decimal("0"),
    infra_credits: Decimal = Decimal("0"),
) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO conversation_usages (
                conversation_response_id, user_id, conversation_thread_id, workspace_id,
                msg_type, status, token_usage, is_byok, total_credits, infrastructure_credits
            )
            VALUES (%s, %s, %s, %s, %s, 'completed', %s::jsonb, %s, %s, %s)
            """,
            (
                response_id,
                user_id,
                thread_id,
                workspace_id,
                msg_type,
                json.dumps(token_usage) if token_usage is not None else None,
                is_byok,
                total_credits,
                infra_credits,
            ),
        )


async def _fetch_resv(conn: Any, run_ref: str) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
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


async def _fetch_resp(conn: Any, run_id: str) -> dict[str, Any]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT status, usage_settled_at, metadata
            FROM conversation_responses
            WHERE conversation_response_id = %s
            """,
            (run_id,),
        )
        row = await cur.fetchone()
        assert row is not None
        return dict(row)


async def test_pg_finalize_run_secondary_stamp_sql(pg_conn, monkeypatch):
    """Real Postgres execution of `finalize_run` primary CAS + secondary `EXISTS` stamp:
    - `ptc` row with non-empty `per_call_costs` -> stamps `usage_settled_at`;
    - missing `ptc` row (even with a `task` row or tool-only `ptc` row) -> leaves
      `usage_settled_at IS NULL` and records `metadata->>'dalton_finalized_at'`;
    - `no_model_calls=True` -> stamps `usage_settled_at` upfront AND persists
      `metadata.dalton_no_model_calls=true` alongside `dalton_finalized_at`."""
    _patch_non_sql_finalize_hooks(monkeypatch)
    user_id = "u-pg-finalize"
    ws_id = str(uuid.uuid4())
    th_id = str(uuid.uuid4())

    run_ok = str(uuid.uuid4())
    run_missing = str(uuid.uuid4())
    run_zero = str(uuid.uuid4())

    for rid in (run_ok, run_missing, run_zero):
        await _insert_response(pg_conn, run_id=rid, thread_id=th_id, user_id=user_id)
    await pg_conn.commit()

    # 1. With valid ptc usage row -> secondary UPDATE stamps usage_settled_at
    async def _write_ptc(c: Any, _status: str) -> None:
        await _insert_usage(
            c,
            response_id=run_ok,
            user_id=user_id,
            thread_id=th_id,
            workspace_id=ws_id,
            msg_type="ptc",
            token_usage={
                "per_call_costs": [
                    {"model_name": "gpt-6.1-terra", "billing_type": "byok", "cost": 0.15}
                ]
            },
        )

    res_ok = await finalize_run(
        run_id=run_ok,
        thread_id=th_id,
        outcome=RunOutcome(status="completed"),
        usage_writer=_write_ptc,
        conn=pg_conn,
    )
    assert res_ok.applied is True
    assert res_ok.run["usage_settled_at"] is not None

    # 2. Only a task usage row + tool-only ptc row (token_usage=NULL) -> stays NULL + dalton_finalized_at set
    await _insert_usage(
        pg_conn,
        response_id=run_missing,
        user_id=user_id,
        thread_id=th_id,
        workspace_id=ws_id,
        msg_type="task",
        token_usage={
            "task_run_id": str(uuid.uuid4()),
            "per_call_costs": [
                {"model_name": "claude-opus-5-5-oauth", "billing_type": "oauth", "cost": 0.0}
            ],
        },
    )
    await pg_conn.commit()

    async def _write_tool_only(c: Any, _status: str) -> None:
        await _insert_usage(
            c,
            response_id=run_missing,
            user_id=user_id,
            thread_id=th_id,
            workspace_id=ws_id,
            msg_type="ptc",
            token_usage=None,
            total_credits=Decimal("1"),
            infra_credits=Decimal("1"),
        )

    res_missing = await finalize_run(
        run_id=run_missing,
        thread_id=th_id,
        outcome=RunOutcome(status="completed"),
        usage_writer=_write_tool_only,
        conn=pg_conn,
    )
    assert res_missing.applied is True
    assert res_missing.run["usage_settled_at"] is None
    resp_missing = await _fetch_resp(pg_conn, run_missing)
    assert resp_missing["usage_settled_at"] is None
    assert "dalton_finalized_at" in (resp_missing["metadata"] or {})
    assert "dalton_no_model_calls" not in (resp_missing["metadata"] or {})

    # 3. Proven zero model calls -> stamped upfront with dalton_no_model_calls=true + dalton_finalized_at
    res_zero = await finalize_run(
        run_id=run_zero,
        thread_id=th_id,
        outcome=RunOutcome(status="error", no_model_calls=True),
        usage_writer=None,
        conn=pg_conn,
    )
    assert res_zero.applied is True
    assert res_zero.run["usage_settled_at"] is not None
    resp_zero = await _fetch_resp(pg_conn, run_zero)
    assert resp_zero["metadata"]["dalton_no_model_calls"] is True
    assert "dalton_finalized_at" in resp_zero["metadata"]


async def test_pg_proven_no_model_calls_settles_without_reserved_floor(pg_conn, monkeypatch):
    """R3 Item 1: `dalton_no_model_calls: true` rows settle at actual tool/subagent usage
    (never `reserved_usd`), in both `reconcile_reservations_sql` and
    `settle_abandoned_runs_without_ptc_sql`."""
    _patch_non_sql_finalize_hooks(monkeypatch)
    user_id = "u-pg-zero"
    ws_id = str(uuid.uuid4())
    th_id = str(uuid.uuid4())
    old = datetime.now(UTC) - timedelta(minutes=40)

    run_zero_empty = str(uuid.uuid4())
    run_zero_tool = str(uuid.uuid4())
    run_zero_fallback = str(uuid.uuid4())

    for rid in (run_zero_empty, run_zero_tool):
        await _insert_response(pg_conn, run_id=rid, thread_id=th_id, user_id=user_id)
        await _insert_reservation(
            pg_conn, user_id=user_id, run_ref=rid, thread_id=th_id, workspace_id=ws_id, reserved_usd=Decimal("1.000000")
        )
    await pg_conn.commit()

    await finalize_run(
        run_id=run_zero_empty,
        thread_id=th_id,
        outcome=RunOutcome(status="cancelled", no_model_calls=True),
        usage_writer=None,
        conn=pg_conn,
    )

    async def _write_tool(c: Any, _status: str) -> None:
        await _insert_usage(
            c,
            response_id=run_zero_tool,
            user_id=user_id,
            thread_id=th_id,
            workspace_id=ws_id,
            msg_type="ptc",
            token_usage=None,
            is_byok=False,
            total_credits=Decimal("50"),
            infra_credits=Decimal("50"),
        )

    await finalize_run(
        run_id=run_zero_tool,
        thread_id=th_id,
        outcome=RunOutcome(status="cancelled", no_model_calls=True),
        usage_writer=_write_tool,
        conn=pg_conn,
    )

    # Also test direct fallback SQL (RecoveryScanner path) on an aged zero-call row
    await _insert_response(
        pg_conn,
        run_id=run_zero_fallback,
        thread_id=th_id,
        user_id=user_id,
        status="error",
        usage_settled_at=old,
        created_at=old,
        metadata={
            "dalton_finalized_at": old.isoformat().replace("+00:00", "Z"),
            "dalton_no_model_calls": True,
        },
    )
    await _insert_reservation(
        pg_conn,
        user_id=user_id,
        run_ref=run_zero_fallback,
        thread_id=th_id,
        workspace_id=ws_id,
        reserved_usd=Decimal("1.000000"),
    )
    await pg_conn.commit()

    settled_fb = await budget_db.settle_abandoned_runs_without_ptc_sql(
        pg_conn, Decimal("1000"), user_id=user_id
    )
    assert settled_fb == 1
    r_fb = await _fetch_resv(pg_conn, run_zero_fallback)
    assert r_fb["state"] == "settled"
    assert r_fb["settle_source"] == "usage"
    assert r_fb["settled_usd"] == Decimal("0.000000")

    await budget_db.reconcile_reservations_sql(pg_conn, user_id, Decimal("1000"))
    await pg_conn.commit()

    r_empty = await _fetch_resv(pg_conn, run_zero_empty)
    assert r_empty["state"] == "settled"
    assert r_empty["settle_source"] == "usage"
    assert r_empty["settled_usd"] == Decimal("0.000000")

    r_tool = await _fetch_resv(pg_conn, run_zero_tool)
    assert r_tool["state"] == "settled"
    assert r_tool["settle_source"] == "usage"
    assert r_tool["settled_usd"] == Decimal("0.050000")


async def test_pg_resumed_subagent_chain_merges_usage_check(pg_conn):
    """R3 Item 2: Same-parent `Task(action="resume")` chain (`tr1 -> tr2`) with a single
    merged `task` usage row stamped with `tr2` settles at actual usage; a sibling subagent
    without any usage row on its chain still retains `reserved_usd`."""
    user_id = "u-pg-resume"
    ws_id = str(uuid.uuid4())
    th_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    run_resumed_ok = str(uuid.uuid4())
    run_resumed_missing_sibling = str(uuid.uuid4())

    for rid in (run_resumed_ok, run_resumed_missing_sibling):
        await _insert_response(
            pg_conn,
            run_id=rid,
            thread_id=th_id,
            user_id=user_id,
            status="completed",
            usage_settled_at=now,
            metadata={"dalton_finalized_at": now.isoformat().replace("+00:00", "Z")},
        )
        await _insert_reservation(
            pg_conn, user_id=user_id, run_ref=rid, thread_id=th_id, workspace_id=ws_id, reserved_usd=Decimal("1.000000")
        )
        await _insert_usage(
            pg_conn,
            response_id=rid,
            user_id=user_id,
            thread_id=th_id,
            workspace_id=ws_id,
            msg_type="ptc",
            token_usage={
                "per_call_costs": [
                    {"model_name": "gpt-6.1-terra", "billing_type": "byok", "cost": 0.05}
                ]
            },
        )

    # Case 1: tr1 -> tr2 (resumed in same parent turn), single merged usage row with tr2
    tr1, tr2 = str(uuid.uuid4()), str(uuid.uuid4())
    async with pg_conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO subagent_runs (
                task_run_id, thread_id, task_id, parent_run_id, predecessor_run_id,
                cause, status, finalized_at, usage_settled_at, user_id
            )
            VALUES
                (%s, %s, 'taskA', %s, NULL, 'init', 'completed', %s, %s, %s),
                (%s, %s, 'taskA', %s, %s, 'resume', 'completed', %s, %s, %s)
            """,
            (
                tr1, th_id, run_resumed_ok, now, now, user_id,
                tr2, th_id, run_resumed_ok, tr1, now, now, user_id,
            ),
        )
    await _insert_usage(
        pg_conn,
        response_id=run_resumed_ok,
        user_id=user_id,
        thread_id=th_id,
        workspace_id=ws_id,
        msg_type="task",
        token_usage={
            "task_run_id": tr2,
            "per_call_costs": [
                {"model_name": "gpt-6.1-terra", "billing_type": "byok", "cost": 0.07}
            ],
        },
    )

    # Case 2: tr3 -> tr4 has merged usage, but unrelated sibling tr5 has no usage row
    tr3, tr4, tr5 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    async with pg_conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO subagent_runs (
                task_run_id, thread_id, task_id, parent_run_id, predecessor_run_id,
                cause, status, finalized_at, usage_settled_at, user_id
            )
            VALUES
                (%s, %s, 'taskB', %s, NULL, 'init', 'completed', %s, %s, %s),
                (%s, %s, 'taskB', %s, %s, 'resume', 'completed', %s, %s, %s),
                (%s, %s, 'taskC', %s, NULL, 'init', 'error', %s, %s, %s)
            """,
            (
                tr3, th_id, run_resumed_missing_sibling, now, now, user_id,
                tr4, th_id, run_resumed_missing_sibling, tr3, now, now, user_id,
                tr5, th_id, run_resumed_missing_sibling, now, now, user_id,
            ),
        )
    await _insert_usage(
        pg_conn,
        response_id=run_resumed_missing_sibling,
        user_id=user_id,
        thread_id=th_id,
        workspace_id=ws_id,
        msg_type="task",
        token_usage={
            "task_run_id": tr4,
            "per_call_costs": [
                {"model_name": "gpt-6.1-terra", "billing_type": "byok", "cost": 0.07}
            ],
        },
    )

    await budget_db.reconcile_reservations_sql(pg_conn, user_id, Decimal("1000"))
    await pg_conn.commit()

    r_ok = await _fetch_resv(pg_conn, run_resumed_ok)
    assert r_ok["state"] == "settled"
    assert r_ok["settle_source"] == "usage"
    assert r_ok["settled_usd"] == Decimal("0.120000")

    r_miss = await _fetch_resv(pg_conn, run_resumed_missing_sibling)
    assert r_miss["state"] == "settled"
    assert r_miss["settle_source"] == "conservative"
    assert r_miss["settled_usd"] == Decimal("1.000000")


async def test_pg_reconcile_reservations_pricing_failed_and_workflow_regression(pg_conn):
    """Real Postgres execution of `reconcile_reservations_sql` (Step 1 + Step 2):
    - priced BYOK turn settles at actual usage;
    - pricing_failed BYOK turn keeps `reserved_usd` conservative floor in Step 1 and Step 2;
    - #478 finished workflow driver with complete child usage settles by actual usage."""
    user_id = "u-pg-reconcile"
    ws_id = str(uuid.uuid4())
    th_id = str(uuid.uuid4())
    old = datetime.now(UTC) - timedelta(minutes=45)

    run_priced = str(uuid.uuid4())
    run_pf = str(uuid.uuid4())
    run_wf_ok = str(uuid.uuid4())
    run_wf_pf = str(uuid.uuid4())

    for rid, settled_ts in (
        (run_priced, datetime.now(UTC)),
        (run_pf, datetime.now(UTC)),
        (run_wf_ok, old),
        (run_wf_pf, old),
    ):
        await _insert_response(
            pg_conn,
            run_id=rid,
            thread_id=th_id,
            user_id=user_id,
            status="completed",
            usage_settled_at=settled_ts,
            created_at=old,
        )
        await _insert_reservation(
            pg_conn,
            user_id=user_id,
            run_ref=rid,
            thread_id=th_id,
            workspace_id=ws_id,
            reserved_usd=Decimal("1.000000"),
        )

    await _insert_usage(
        pg_conn,
        response_id=run_priced,
        user_id=user_id,
        thread_id=th_id,
        workspace_id=ws_id,
        msg_type="ptc",
        token_usage={
            "per_call_costs": [
                {"model_name": "gpt-6.1-terra", "billing_type": "byok", "cost": 0.123456}
            ]
        },
    )
    await _insert_usage(
        pg_conn,
        response_id=run_pf,
        user_id=user_id,
        thread_id=th_id,
        workspace_id=ws_id,
        msg_type="ptc",
        token_usage={
            "pricing_failed": True,
            "pricing_failed_models": ["mystery-model"],
            "per_call_costs": [
                {
                    "model_name": "mystery-model",
                    "billing_type": "byok",
                    "cost": 0.0,
                    "pricing_failed": True,
                }
            ],
        },
    )

    # Workflow #478 OK vs workflow with pricing_failed child
    for run_wf, child_failed in ((run_wf_ok, False), (run_wf_pf, True)):
        drv_id = str(uuid.uuid4())
        child_id = str(uuid.uuid4())
        async with pg_conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO subagent_runs (
                    task_run_id, thread_id, task_id, parent_run_id,
                    cause, status, replay_facts, finalized_at, user_id
                )
                VALUES (%s, %s, %s, %s, 'init', 'completed', '{"workflow_driver": true}'::jsonb, %s, %s)
                """,
                (drv_id, th_id, f"wf-{drv_id[:6]}", run_wf, old, user_id),
            )
            await cur.execute(
                """
                INSERT INTO subagent_runs (
                    task_run_id, thread_id, task_id, parent_run_id,
                    cause, status, replay_facts, finalized_at, usage_settled_at, user_id
                )
                VALUES (%s, %s, %s, NULL, 'init', 'completed', %s::jsonb, %s, %s, %s)
                """,
                (
                    child_id,
                    th_id,
                    f"child-{child_id[:6]}",
                    json.dumps({"workflow_driver_run_id": drv_id}),
                    old,
                    old,
                    user_id,
                ),
            )
        await _insert_usage(
            pg_conn,
            response_id=run_wf,
            user_id=user_id,
            thread_id=th_id,
            workspace_id=ws_id,
            msg_type="ptc",
            token_usage={
                "per_call_costs": [
                    {"model_name": "claude-opus-5-5-oauth", "billing_type": "oauth", "cost": 0.0}
                ]
            },
        )
        await _insert_usage(
            pg_conn,
            response_id=run_wf,
            user_id=user_id,
            thread_id=th_id,
            workspace_id=ws_id,
            msg_type="task",
            token_usage={
                "task_run_id": child_id,
                **({"pricing_failed": True} if child_failed else {}),
                "per_call_costs": [
                    {
                        "model_name": "mystery" if child_failed else "gpt-6.1-terra",
                        "billing_type": "byok",
                        "cost": 0.0 if child_failed else 0.08,
                        **({"pricing_failed": True} if child_failed else {}),
                    }
                ],
            },
        )

    await budget_db.reconcile_reservations_sql(pg_conn, user_id, Decimal("1000"))
    await pg_conn.commit()

    r_priced = await _fetch_resv(pg_conn, run_priced)
    assert r_priced["state"] == "settled"
    assert r_priced["settle_source"] == "usage"
    assert r_priced["settled_usd"] == Decimal("0.123456")

    r_pf = await _fetch_resv(pg_conn, run_pf)
    assert r_pf["state"] == "settled"
    assert r_pf["settle_source"] == "conservative"
    assert r_pf["settled_usd"] == Decimal("1.000000")

    r_wf_ok = await _fetch_resv(pg_conn, run_wf_ok)
    assert r_wf_ok["state"] == "settled"
    assert r_wf_ok["settle_source"] == "usage"
    assert r_wf_ok["settled_usd"] == Decimal("0.080000")

    r_wf_pf = await _fetch_resv(pg_conn, run_wf_pf)
    assert r_wf_pf["state"] == "settled"
    assert r_wf_pf["settle_source"] == "conservative"
    assert r_wf_pf["settled_usd"] == Decimal("1.000000")


async def test_pg_bounded_fallback_independent_grace_and_historical_scope(pg_conn, monkeypatch):
    """Real Postgres execution of bounded fallback settlement + R3 Items 3 & 4:
    - inside 30m grace: parent stays unstamped and reservation stays open/released;
    - past 30m grace (platform `in_flight_credits > 0` AND BYOK `in_flight_credits = 0`):
      parent gets `usage_settled_at`, reservation settles conservatively at `reserved_usd`;
    - Item 3: once parent is fallback-stamped, an already-aged child settles immediately
      without a second 30m wait, while a child finalized only 5m ago still waits its own 30m;
    - Item 4: pre-deploy historical rows (zero in-flight, no `dalton_finalized_at`, no
      reservation) are never stamped by `settle_abandoned` or `settle_abandoned_runs_without_ptc_sql`."""
    user_id = "u-pg-fallback"
    ws_id = str(uuid.uuid4())
    th_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    recent = now - timedelta(minutes=5)
    old = now - timedelta(minutes=35)
    ancient = now - timedelta(days=30)

    run_recent = str(uuid.uuid4())
    run_plat_old = str(uuid.uuid4())
    run_byok_old = str(uuid.uuid4())
    run_historical = str(uuid.uuid4())

    await _insert_response(
        pg_conn,
        run_id=run_recent,
        thread_id=th_id,
        user_id=user_id,
        status="error",
        in_flight_credits=Decimal("0"),
        created_at=recent,
        metadata={"dalton_finalized_at": recent.isoformat().replace("+00:00", "Z")},
    )
    await _insert_reservation(
        pg_conn, user_id=user_id, run_ref=run_recent, thread_id=th_id, workspace_id=ws_id, reserved_usd=Decimal("0.800000")
    )

    await _insert_response(
        pg_conn,
        run_id=run_plat_old,
        thread_id=th_id,
        user_id=user_id,
        status="error",
        in_flight_credits=Decimal("4.25"),
        created_at=old,
        metadata={"dalton_finalized_at": old.isoformat().replace("+00:00", "Z")},
    )
    await _insert_reservation(
        pg_conn, user_id=user_id, run_ref=run_plat_old, thread_id=th_id, workspace_id=ws_id, reserved_usd=Decimal("0.900000"), state="open"
    )
    # Tool-only ptc row without per_call_costs must STILL stay conservative.
    await _insert_usage(
        pg_conn,
        response_id=run_plat_old,
        user_id=user_id,
        thread_id=th_id,
        workspace_id=ws_id,
        msg_type="ptc",
        token_usage=None,
        is_byok=False,
        total_credits=Decimal("10"),
        infra_credits=Decimal("10"),
    )

    await _insert_response(
        pg_conn,
        run_id=run_byok_old,
        thread_id=th_id,
        user_id=user_id,
        status="error",
        in_flight_credits=Decimal("0"),
        created_at=old,
        metadata={"dalton_finalized_at": old.isoformat().replace("+00:00", "Z")},
    )
    await _insert_reservation(
        pg_conn, user_id=user_id, run_ref=run_byok_old, thread_id=th_id, workspace_id=ws_id, reserved_usd=Decimal("0.650000"), state="released"
    )

    # Pre-deploy historical row: zero in_flight_credits, no dalton_finalized_at, no reservation.
    await _insert_response(
        pg_conn,
        run_id=run_historical,
        thread_id=th_id,
        user_id=None,
        status="completed",
        in_flight_credits=Decimal("0"),
        created_at=ancient,
        metadata={},
    )

    child_aged = str(uuid.uuid4())
    child_recent = str(uuid.uuid4())
    child_historical = str(uuid.uuid4())
    async with pg_conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO subagent_runs (
                task_run_id, thread_id, task_id, parent_run_id,
                cause, status, finalized_at, in_flight_credits, user_id
            )
            VALUES
                (%s, %s, 'child-aged', %s, 'init', 'error', %s, 0, %s),
                (%s, %s, 'child-recent', %s, 'init', 'error', %s, 0, %s),
                (%s, %s, 'child-hist', %s, 'init', 'completed', %s, 0, NULL)
            """,
            (
                child_aged, th_id, run_byok_old, old, user_id,
                child_recent, th_id, run_byok_old, recent, user_id,
                child_historical, th_id, run_historical, ancient,
            ),
        )
    await pg_conn.commit()

    @asynccontextmanager
    async def _use_pg_conn():
        yield pg_conn

    monkeypatch.setattr("src.server.database.pool.get_db_connection", _use_pg_conn)

    # Before parent is stamped, child tasks cannot be settled by abandoned sweep.
    assert await credit_ledger_db.settle_abandoned("task") == 0

    # Run reconcile_reservations_sql (includes Step 2b fallback settlement).
    await budget_db.reconcile_reservations_sql(pg_conn, user_id, Decimal("1000"))
    await pg_conn.commit()

    # Recent row stays unstamped and released; historical row stays completely untouched.
    assert (await _fetch_resp(pg_conn, run_recent))["usage_settled_at"] is None
    assert (await _fetch_resv(pg_conn, run_recent))["state"] == "released"
    assert (await _fetch_resp(pg_conn, run_historical))["usage_settled_at"] is None

    # Both old platform and BYOK parents are stamped and conservatively settled at reserved_usd.
    assert (await _fetch_resp(pg_conn, run_plat_old))["usage_settled_at"] is not None
    r_plat = await _fetch_resv(pg_conn, run_plat_old)
    assert r_plat["state"] == "settled"
    assert r_plat["settle_source"] == "conservative"
    assert r_plat["settled_usd"] == Decimal("0.900000")

    assert (await _fetch_resp(pg_conn, run_byok_old))["usage_settled_at"] is not None
    # run_byok_old still has child_recent inside 30m grace, so its reservation stays released until child_recent ages.
    assert (await _fetch_resv(pg_conn, run_byok_old))["state"] == "released"

    # Item 3: child_aged settles IMMEDIATELY once parent is fallback-stamped (no second 30m wait),
    # while child_recent still waits out its OWN 30m grace and child_historical is never touched.
    assert await credit_ledger_db.settle_abandoned("task") == 1
    assert await credit_ledger_db.settle_abandoned("run") == 0
    async with pg_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT task_run_id::text AS id, usage_settled_at FROM subagent_runs WHERE task_run_id = ANY(%s)",
            ([child_aged, child_recent, child_historical],),
        )
        by_id = {r["id"]: r["usage_settled_at"] for r in await cur.fetchall()}
    assert by_id[child_aged] is not None
    assert by_id[child_recent] is None
    assert by_id[child_historical] is None
    assert (await _fetch_resp(pg_conn, run_historical))["usage_settled_at"] is None


async def test_pg_fetch_unreserved_usage_today_with_pricing_failed_floor(pg_conn):
    """Real Postgres execution of `fetch_unreserved_usage_today` + R3 Item 6:
    - per-call pricing_failed calls each top up to the conservative floor;
    - legacy pricing_failed rows without `per_call_costs` take `GREATEST(floor, recorded_usd)`
      instead of summing floor + recorded_usd."""
    user_id = "u-pg-unresv"
    ws_id = str(uuid.uuid4())
    th_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    floor = Decimal("0.250000")

    # 1. Mixed row: 1 priced ($0.10) + 2 pricing_failed ($0 each) -> 0.10 + 2*0.25 = 0.60
    await _insert_usage(
        pg_conn,
        response_id=str(uuid.uuid4()),
        user_id=user_id,
        thread_id=th_id,
        workspace_id=ws_id,
        token_usage={
            "pricing_failed": True,
            "pricing_failed_models": ["mystery"],
            "per_call_costs": [
                {"model_name": "gpt-6.1-terra", "billing_type": "byok", "cost": 0.10},
                {"model_name": "mystery", "billing_type": "byok", "cost": 0.0, "pricing_failed": True},
                {"model_name": "mystery", "billing_type": "byok", "cost": 0.0, "pricing_failed": True},
            ],
        },
    )
    # 2. Legacy row without per_call_costs, recorded $0.10 (< floor $0.25) -> max(0.25, 0.10) = 0.25
    await _insert_usage(
        pg_conn,
        response_id=str(uuid.uuid4()),
        user_id=user_id,
        thread_id=th_id,
        workspace_id=ws_id,
        is_byok=False,
        total_credits=Decimal("100"),  # $0.10 at rate=1000
        token_usage={"pricing_failed": True, "by_model": {"old-mystery": {"total_cost": 0.10}}},
    )
    # 3. Legacy row without per_call_costs, recorded $0.40 (> floor $0.25) -> max(0.25, 0.40) = 0.40
    await _insert_usage(
        pg_conn,
        response_id=str(uuid.uuid4()),
        user_id=user_id,
        thread_id=th_id,
        workspace_id=ws_id,
        is_byok=False,
        total_credits=Decimal("400"),  # $0.40 at rate=1000
        token_usage={"pricing_failed": True, "by_model": {"old-mystery": {"total_cost": 0.40}}},
    )
    await pg_conn.commit()

    got = await budget_db.fetch_unreserved_usage_today(
        pg_conn,
        user_id,
        now - timedelta(hours=1),
        now + timedelta(hours=1),
        Decimal("1000"),
        conservative_floor_usd=floor,
    )
    assert got == Decimal("0.10") + (floor * 2) + floor + Decimal("0.40")


async def test_pg_r4_legacy_grace_workflow_child_scope_and_upstream_only_tables(pg_conn, monkeypatch):
    """Real Postgres verification of R4 Items 3, 4, and 5:
    - Item 4: Historical workflow internal child (no parent `dalton_finalized_at`, no reservation)
      is never stamped by `settle_abandoned("task")`, while a post-deploy/reserved workflow child is.
    - Item 5: A legacy parent without `dalton_finalized_at` whose `usage_settled_at` was just stamped
      does not force an already-aged child (`finalized_at` > 30m ago) to wait a second 30m window
      in either `credit_ledger.settle_abandoned("task")` or `budget.reconcile_reservations_sql`.
    - Item 3: Dropping `dalton_budget_reservations` (simulating upstream-only migrations) lets both
      `credit_ledger.settle_abandoned` and `settle_abandoned_runs_without_ptc_sql` succeed cleanly."""
    user_id = "u-pg-r4"
    ws_id = str(uuid.uuid4())
    th_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    old = now - timedelta(minutes=35)
    recent = now - timedelta(minutes=10)
    ancient = now - timedelta(days=30)

    # 1. Legacy parent (no dalton_finalized_at) fallback-stamped NOW, with aged child & recent child
    run_legacy = str(uuid.uuid4())
    await _insert_response(
        pg_conn,
        run_id=run_legacy,
        thread_id=th_id,
        user_id=user_id,
        status="error",
        in_flight_credits=Decimal("0"),
        usage_settled_at=now,
        created_at=old,
        metadata={},
    )
    await _insert_reservation(
        pg_conn,
        user_id=user_id,
        run_ref=run_legacy,
        thread_id=th_id,
        workspace_id=ws_id,
        reserved_usd=Decimal("0.750000"),
        state="released",
    )
    legacy_child_aged = str(uuid.uuid4())
    legacy_child_recent = str(uuid.uuid4())

    # 2. Historical workflow parent + driver + internal child (no dalton_finalized_at, no reservation)
    run_wf_hist = str(uuid.uuid4())
    await _insert_response(
        pg_conn,
        run_id=run_wf_hist,
        thread_id=th_id,
        user_id=None,
        status="completed",
        in_flight_credits=Decimal("0"),
        usage_settled_at=ancient,
        created_at=ancient,
        metadata={},
    )
    drv_hist = str(uuid.uuid4())
    wf_child_hist = str(uuid.uuid4())

    # 3. Post-deploy workflow parent + driver + internal child (has dalton_finalized_at > 30m)
    run_wf_post = str(uuid.uuid4())
    await _insert_response(
        pg_conn,
        run_id=run_wf_post,
        thread_id=th_id,
        user_id=user_id,
        status="completed",
        in_flight_credits=Decimal("0"),
        usage_settled_at=old,
        created_at=old,
        metadata={"dalton_finalized_at": old.isoformat().replace("+00:00", "Z")},
    )
    drv_post = str(uuid.uuid4())
    wf_child_post = str(uuid.uuid4())

    async with pg_conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO subagent_runs (
                task_run_id, thread_id, task_id, parent_run_id,
                cause, status, replay_facts, finalized_at, in_flight_credits, user_id
            )
            VALUES
                (%s, %s, 'leg-aged', %s, 'init', 'error', NULL, %s, 0, %s),
                (%s, %s, 'leg-recent', %s, 'init', 'error', NULL, %s, 0, %s),
                (%s, %s, 'drv-hist', %s, 'init', 'completed', '{"workflow_driver": true}'::jsonb, %s, 0, NULL),
                (%s, %s, 'wf-ch-hist', NULL, 'init', 'completed', %s::jsonb, %s, 0, NULL),
                (%s, %s, 'drv-post', %s, 'init', 'completed', '{"workflow_driver": true}'::jsonb, %s, 0, %s),
                (%s, %s, 'wf-ch-post', NULL, 'init', 'completed', %s::jsonb, %s, 0, %s)
            """,
            (
                legacy_child_aged, th_id, run_legacy, old, user_id,
                legacy_child_recent, th_id, run_legacy, recent, user_id,
                drv_hist, th_id, run_wf_hist, ancient,
                wf_child_hist, th_id, json.dumps({"workflow_driver_run_id": drv_hist}), ancient,
                drv_post, th_id, run_wf_post, old, user_id,
                wf_child_post, th_id, json.dumps({"workflow_driver_run_id": drv_post}), old, user_id,
            ),
        )
    await pg_conn.commit()

    @asynccontextmanager
    async def _use_pg_conn():
        yield pg_conn

    monkeypatch.setattr("src.server.database.pool.get_db_connection", _use_pg_conn)

    # Item 4 & 5: legacy_child_aged + wf_child_post settle immediately (2 rows);
    # legacy_child_recent waits on its own finalized_at; wf_child_hist is untouched!
    assert await credit_ledger_db.settle_abandoned("task") == 2
    async with pg_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT task_run_id::text AS id, usage_settled_at FROM subagent_runs WHERE task_run_id = ANY(%s)",
            ([legacy_child_aged, legacy_child_recent, wf_child_hist, wf_child_post],),
        )
        by_id = {r["id"]: r["usage_settled_at"] for r in await cur.fetchall()}
    assert by_id[legacy_child_aged] is not None
    assert by_id[wf_child_post] is not None
    assert by_id[legacy_child_recent] is None
    assert by_id[wf_child_hist] is None

    # Item 5 in reconcile Step 2: age legacy_child_recent past 30m (still unstamped) while parent
    # usage_settled_at is STILL `now` (0m ago) -> reconcile Step 2 settles immediately on child's clock!
    async with pg_conn.cursor() as cur:
        await cur.execute(
            "UPDATE subagent_runs SET finalized_at = %s WHERE task_run_id = %s",
            (old, legacy_child_recent),
        )
    await pg_conn.commit()
    await budget_db.reconcile_reservations_sql(pg_conn, user_id, Decimal("1000"))
    await pg_conn.commit()
    r_leg = await _fetch_resv(pg_conn, run_legacy)
    assert r_leg["state"] == "settled"
    assert r_leg["settle_source"] == "conservative"
    assert r_leg["settled_usd"] == Decimal("0.750000")

    # Item 3: simulate upstream-only deployment by dropping dalton_budget_reservations
    async with pg_conn.cursor() as cur:
        await cur.execute("DROP TABLE dalton_budget_reservations")
    await pg_conn.commit()

    async with pg_conn.cursor() as cur:
        await cur.execute(
            "SELECT to_regclass(quote_ident(current_schema()) || '.dalton_budget_reservations'), "
            "to_regclass('dalton_budget_reservations')"
        )
        reg_qualified, reg_unqualified = await cur.fetchone()
    assert reg_qualified is None
    assert reg_unqualified is None

    assert await credit_ledger_db.settle_abandoned("run") == 0
    # Upstream sweep never touches zero-credit subagent rows (`in_flight_credits > 0` required).
    assert await credit_ledger_db.settle_abandoned("task") == 0
    async with pg_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT usage_settled_at FROM subagent_runs WHERE task_run_id = %s",
            (legacy_child_recent,),
        )
        leg_row = await cur.fetchone()
    assert leg_row is not None
    assert leg_row["usage_settled_at"] is None
    assert await budget_db.settle_abandoned_runs_without_ptc_sql(pg_conn, Decimal("1000")) == 0


async def test_pg_upstream_only_tables_exact_upstream_timing(pg_conn, monkeypatch):
    """Upstream-only deployment (no `dalton_budget_reservations` table):
    `credit_ledger.settle_abandoned("run")` and `settle_abandoned("task")` follow
    the exact upstream SQL timing and filters:
    - positive-credit terminal responses with `user_id IS NOT NULL` settle immediately
      (no 30m grace), while zero-credit or owner-less responses stay unstamped;
    - positive-credit terminal task runs with `user_id IS NOT NULL` wait out both
      their own 30m `finalized_at` grace and, when a parent row exists, the parent
      being non-in_progress with `usage_settled_at` older than 30m (even if
      `dalton_finalized_at` is older), while zero-credit tasks stay unstamped."""
    user_id = "u-pg-upstream-only"
    th_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    recent = now - timedelta(minutes=10)
    old = now - timedelta(minutes=35)

    # Drop the Dalton table to simulate an upstream-only deployment.
    async with pg_conn.cursor() as cur:
        await cur.execute("DROP TABLE dalton_budget_reservations")
    await pg_conn.commit()

    async with pg_conn.cursor() as cur:
        await cur.execute(
            "SELECT to_regclass(quote_ident(current_schema()) || '.dalton_budget_reservations')"
        )
        assert (await cur.fetchone())[0] is None

    # Main turns (`settle_abandoned("run")`)
    run_pos_recent = str(uuid.uuid4())
    run_zero_old = str(uuid.uuid4())
    run_no_user_old = str(uuid.uuid4())

    await _insert_response(
        pg_conn,
        run_id=run_pos_recent,
        thread_id=th_id,
        user_id=user_id,
        status="error",
        in_flight_credits=Decimal("5.5"),
        usage_settled_at=None,
        created_at=recent,
        metadata={"dalton_finalized_at": recent.isoformat().replace("+00:00", "Z")},
    )
    await _insert_response(
        pg_conn,
        run_id=run_zero_old,
        thread_id=th_id,
        user_id=user_id,
        status="error",
        in_flight_credits=Decimal("0"),
        usage_settled_at=None,
        created_at=old,
        metadata={"dalton_finalized_at": old.isoformat().replace("+00:00", "Z")},
    )
    await _insert_response(
        pg_conn,
        run_id=run_no_user_old,
        thread_id=th_id,
        user_id=None,
        status="error",
        in_flight_credits=Decimal("5.5"),
        usage_settled_at=None,
        created_at=old,
        metadata={},
    )

    # Parent turns for task sweep (`settle_abandoned("task")`)
    parent_settled_recent = str(uuid.uuid4())
    parent_settled_old = str(uuid.uuid4())
    parent_unsettled = str(uuid.uuid4())

    await _insert_response(
        pg_conn,
        run_id=parent_settled_recent,
        thread_id=th_id,
        user_id=user_id,
        status="completed",
        in_flight_credits=Decimal("0"),
        usage_settled_at=recent,
        created_at=old,
        metadata={"dalton_finalized_at": old.isoformat().replace("+00:00", "Z")},
    )
    await _insert_response(
        pg_conn,
        run_id=parent_settled_old,
        thread_id=th_id,
        user_id=user_id,
        status="completed",
        in_flight_credits=Decimal("0"),
        usage_settled_at=old,
        created_at=old,
        metadata={},
    )
    await _insert_response(
        pg_conn,
        run_id=parent_unsettled,
        thread_id=th_id,
        user_id=user_id,
        status="completed",
        in_flight_credits=Decimal("0"),
        usage_settled_at=None,
        created_at=old,
        metadata={},
    )

    child_parent_recent = str(uuid.uuid4())
    child_parent_old_aged = str(uuid.uuid4())
    child_parent_old_recent = str(uuid.uuid4())
    child_parent_unsettled = str(uuid.uuid4())
    child_zero_aged = str(uuid.uuid4())
    child_orphan_aged = str(uuid.uuid4())

    async with pg_conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO subagent_runs (
                task_run_id, thread_id, task_id, parent_run_id,
                cause, status, finalized_at, in_flight_credits, user_id
            )
            VALUES
                (%s, %s, 'ch-par-recent', %s, 'init', 'error', %s, 2.5, %s),
                (%s, %s, 'ch-par-old-aged', %s, 'init', 'error', %s, 2.5, %s),
                (%s, %s, 'ch-par-old-recent', %s, 'init', 'error', %s, 2.5, %s),
                (%s, %s, 'ch-par-unsettled', %s, 'init', 'error', %s, 2.5, %s),
                (%s, %s, 'ch-zero-aged', %s, 'init', 'error', %s, 0, %s),
                (%s, %s, 'ch-orphan-aged', NULL, 'init', 'error', %s, 2.5, %s)
            """,
            (
                child_parent_recent, th_id, parent_settled_recent, old, user_id,
                child_parent_old_aged, th_id, parent_settled_old, old, user_id,
                child_parent_old_recent, th_id, parent_settled_old, recent, user_id,
                child_parent_unsettled, th_id, parent_unsettled, old, user_id,
                child_zero_aged, th_id, parent_settled_old, old, user_id,
                child_orphan_aged, th_id, old, user_id,
            ),
        )
    await pg_conn.commit()

    @asynccontextmanager
    async def _use_pg_conn():
        yield pg_conn

    monkeypatch.setattr("src.server.database.pool.get_db_connection", _use_pg_conn)

    # Upstream run sweep: settles positive-credit recent response immediately (1 row).
    assert await credit_ledger_db.settle_abandoned("run") == 1
    assert (await _fetch_resp(pg_conn, run_pos_recent))["usage_settled_at"] is not None
    assert (await _fetch_resp(pg_conn, run_zero_old))["usage_settled_at"] is None
    assert (await _fetch_resp(pg_conn, run_no_user_old))["usage_settled_at"] is None

    # Upstream task sweep: settles only child_parent_old_aged and child_orphan_aged (2 rows).
    assert await credit_ledger_db.settle_abandoned("task") == 2
    async with pg_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT task_run_id::text AS id, usage_settled_at FROM subagent_runs WHERE task_run_id = ANY(%s)",
            (
                [
                    child_parent_recent,
                    child_parent_old_aged,
                    child_parent_old_recent,
                    child_parent_unsettled,
                    child_zero_aged,
                    child_orphan_aged,
                ],
            ),
        )
        by_id = {r["id"]: r["usage_settled_at"] for r in await cur.fetchall()}

    assert by_id[child_parent_old_aged] is not None
    assert by_id[child_orphan_aged] is not None
    # Upstream parent grace uses `r.usage_settled_at > NOW() - INTERVAL '30 minutes'`, NOT dalton_finalized_at.
    assert by_id[child_parent_recent] is None
    assert by_id[child_parent_old_recent] is None
    assert by_id[child_parent_unsettled] is None
    assert by_id[child_zero_aged] is None
