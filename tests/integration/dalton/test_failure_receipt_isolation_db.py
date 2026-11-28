"""Real PostgreSQL + dual-worker fault-injection integration tests for Batch 4 (§3, R2):
1. Formal ``automation_settlement.settle`` same-tx enqueue & rollback atomicity:
   - Enqueue SQL failure inside ``leave_finish`` rolls back settlement (execution stays ``running``,
     zero rows in ``dalton_failure_notices``); re-settling succeeds and enqueues strictly once.
   - Crash right after settle commit (before ``_after_settling`` runs) leaves the receipt durable
     in PostgreSQL and a second worker drains & delivers via ``process_due_failure_notices``.
2. Lease expiration -> ``reconcile_uncertain_failure_notice_targets`` -> re-claim -> old token
   denied by ``authorize_and_claim_failure_notice`` without duplicate send.
3. Per-target claim under time budget via ``process_due_failure_notices``: uninitiated targets
   remain ``pending`` with ``attempt_count = 0`` and ``claim_token IS NULL``.
4. Dual-worker concurrent consecutive failures compete atomically on partial unique index
   ``uq_dalton_failure_notices_dedup_key`` (strictly 1 ``retrying`` + 1 ``suppressed``).
5. Concurrent ``automation_finish`` writing ``delivery_result`` + ``sync_terminal_undelivered_execution``
   guard preserves existing ``delivery_result`` and ``result_excerpt`` verbatim on failed executions.
6. Concurrent Weixin failure-notice retry vs ``unbind_weixin_account``: zero PostgreSQL deadlock.
7. Real PostgreSQL Alembic ``d061 <-> d062`` upgrade/downgrade round-trip.

All connections use ``autocommit=True`` and ``prepare_threshold=None`` matching
``src.server.database.pool._configure_postgres_connection``.
Skipped automatically when ``DALTON_TEST_PG_DSN`` is unset (run against a ``*_test`` DB).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
import psycopg
from psycopg.rows import dict_row
import pytest

from src.dalton.database import failure_notice_claims as claims_mod
from src.dalton.database import weixin_multi_account as wx_db
from src.dalton.notify import dispatch as dispatch_mod
from src.dalton.notify import failure_receipts as fr_mod
from src.server.database import automation_executions as exec_db
from src.server.database import encryption as enc_mod
from src.server.database import pool as pool_mod
from src.server.services import automation_outcomes as outcomes_mod
from src.server.services import automation_settlement as settle_mod
from src.server.services import webhook_client as wh_mod
from tests.integration.dalton._db_guard import extract_effective_test_db_name

REPO_ROOT = Path(__file__).resolve().parents[3]
PG_DSN = os.getenv("DALTON_TEST_PG_DSN", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_DSN,
        reason="DALTON_TEST_PG_DSN is not set; skipping real PostgreSQL failure-notice integration tests",
    ),
]


@pytest.fixture(scope="session", autouse=True)
def dalton_schema():
    """Override directory conftest autouse fixture; managed explicitly via ``migrated_dsn``."""
    yield


@pytest.fixture(autouse=True)
def cleanup_dalton_tables():
    """Override directory conftest autouse fixture when DALTON_TEST_PG_DSN is unset."""
    yield


@pytest.fixture(autouse=True)
def test_byok_encryption_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Provide a deterministic test-only BYOK encryption key without reading the host env."""
    key = "test-only-fnr-integration-encryption-key-not-for-prod"
    monkeypatch.setenv("BYOK_ENCRYPTION_KEY", key)
    return key


def _to_sa_url(dsn: str) -> str:
    if dsn.startswith("postgresql+psycopg://"):
        return dsn
    if dsn.startswith("postgresql://"):
        return "postgresql+psycopg://" + dsn[len("postgresql://") :]
    if dsn.startswith("postgres://"):
        return "postgresql+psycopg://" + dsn[len("postgres://") :]
    return dsn


async def _connect_autocommit(dsn: str) -> psycopg.AsyncConnection[Any]:
    """Open a real PG connection matching production pool settings (``autocommit=True``)."""
    conn = await psycopg.AsyncConnection.connect(
        dsn,
        autocommit=True,
        prepare_threshold=None,
    )
    return conn


def _bind_pool_to_dsn(monkeypatch: pytest.MonkeyPatch, dsn: str) -> None:
    """Route ``get_db_connection()`` to isolated ``dsn`` with production autocommit settings."""

    @asynccontextmanager
    async def _get_conn(existing: Any = None):
        if existing is not None:
            yield existing
            return
        async with await _connect_autocommit(dsn) as c:
            yield c

    monkeypatch.setattr(pool_mod, "get_db_connection", _get_conn)
    monkeypatch.setattr(exec_db, "get_db_connection", _get_conn)
    monkeypatch.setattr(fr_mod, "get_db_connection", _get_conn)
    monkeypatch.setattr(claims_mod, "get_db_connection", _get_conn)
    monkeypatch.setattr(dispatch_mod, "get_db_connection", _get_conn)
    monkeypatch.setattr(settle_mod, "get_db_connection", _get_conn)
    monkeypatch.setattr(wx_db, "get_db_connection", _get_conn)


@pytest.fixture(scope="module")
def migrated_dsn() -> str:
    db_name = extract_effective_test_db_name(PG_DSN)
    assert db_name.endswith("_test"), f"Refusing to run against non-test DB {db_name!r}"

    sa_url = _to_sa_url(PG_DSN)
    up_cfg = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    up_cfg.set_main_option("sqlalchemy.url", sa_url)
    alembic_command.upgrade(up_cfg, "head")

    d_cfg = AlembicConfig(str(REPO_ROOT / "src" / "dalton" / "alembic.ini"))
    d_cfg.set_main_option("script_location", str(REPO_ROOT / "src" / "dalton" / "migrations"))
    d_cfg.set_main_option("sqlalchemy.url", sa_url)
    alembic_command.upgrade(d_cfg, "head")
    return PG_DSN


async def _seed_user_and_identity(
    conn: psycopg.AsyncConnection[Any],
    *,
    user_id: str,
    feishu_open_id: str,
) -> None:
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO users (user_id, email, name, created_at, updated_at)
                VALUES (%s, %s, %s, NOW(), NOW())
                ON CONFLICT (user_id) DO NOTHING
                """,
                (user_id, f"{user_id}@test.local", f"Test {user_id}"),
            )
            await cur.execute(
                """
                INSERT INTO dalton_channel_identities (
                    user_id, platform, platform_user_id, created_at, updated_at
                )
                VALUES (%s, 'feishu', %s, NOW(), NOW())
                ON CONFLICT (platform, platform_user_id) DO NOTHING
                """,
                (user_id, feishu_open_id),
            )


async def _seed_automation_and_execution(
    conn: psycopg.AsyncConnection[Any],
    *,
    user_id: str,
    automation_id: str,
    execution_id: str,
    status: str = "running",
    delivery_result: list[dict[str, Any]] | None = None,
    result_excerpt: str | None = None,
) -> dict[str, Any]:
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO automations (
                    automation_id, user_id, name, trigger_type, instruction,
                    metadata, delivery_config, status
                )
                VALUES (
                    %s::uuid, %s, 'FNR Integration Task', 'cron', 'Check portfolio',
                    '{}'::jsonb, '{}'::jsonb, 'active'
                )
                ON CONFLICT (automation_id) DO NOTHING
                """,
                (automation_id, user_id),
            )
            await cur.execute(
                """
                INSERT INTO automation_executions (
                    automation_execution_id, automation_id, status,
                    delivery_result, result_excerpt,
                    scheduled_at, started_at
                )
                VALUES (%s::uuid, %s::uuid, %s, %s::jsonb, %s, NOW(), NOW())
                """,
                (
                    execution_id,
                    automation_id,
                    status,
                    json.dumps(delivery_result) if delivery_result is not None else None,
                    result_excerpt,
                ),
            )
    return {
        "automation_id": automation_id,
        "user_id": user_id,
        "name": "FNR Integration Task",
        "trigger_type": "cron",
    }


@pytest.mark.asyncio
async def test_formal_settle_same_tx_enqueue_rollback_and_crash_recovery(
    migrated_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Formal ``automation_settlement.settle`` path:
    1) Enqueue SQL error rolls back the outer settle tx (execution stays ``running``, zero notice rows).
    2) Re-settling commits both atomically and creates strictly 1 notice.
    3) Simulated process crash before ``_after_settling`` leaves the receipt in PG so worker 2 delivers it.
    """
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    _bind_pool_to_dsn(monkeypatch, migrated_dsn)

    user_id = f"fn_formal_{uuid4().hex[:8]}"
    open_id = f"ou_formal_{uuid4().hex[:8]}"
    auto_id = str(uuid4())
    exec_id = str(uuid4())

    async with await _connect_autocommit(migrated_dsn) as seed_conn:
        await _seed_user_and_identity(seed_conn, user_id=user_id, feishu_open_id=open_id)
        automation = await _seed_automation_and_execution(
            seed_conn,
            user_id=user_id,
            automation_id=auto_id,
            execution_id=exec_id,
            status="running",
        )

    wh_double = create_autospec(wh_mod.WebhookClient, instance=True)
    wh_double.fire_event.return_value = None
    monkeypatch.setattr(settle_mod, "WebhookClient", lambda: wh_double)

    # Phase 1: Inject SQL failure inside enqueue_failure_notice_in_tx -> outer settle tx MUST roll back
    real_enqueue = fr_mod.enqueue_failure_notice_in_tx
    fail_once = {"triggered": False}

    async def _fail_first_enqueue(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        if not fail_once["triggered"]:
            fail_once["triggered"] = True
            raise psycopg.OperationalError("simulated lock timeout during enqueue")
        return await real_enqueue(*args, **kwargs)

    monkeypatch.setattr(
        fr_mod,
        "enqueue_failure_notice_in_tx",
        create_autospec(real_enqueue, side_effect=_fail_first_enqueue),
    )

    with pytest.raises(psycopg.OperationalError, match="simulated lock timeout"):
        await settle_mod.settle(
            automation,
            exec_id,
            outcomes_mod.Outcome.SUBSCRIPTION_LIMITED,
            error="Quota exhausted",
        )

    async with await _connect_autocommit(migrated_dsn) as check_conn:
        async with check_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status FROM automation_executions WHERE automation_execution_id = %s::uuid",
                (exec_id,),
            )
            erow = await cur.fetchone()
            assert erow is not None and erow["status"] == "running"
            await cur.execute(
                "SELECT COUNT(*)::int AS cnt FROM dalton_failure_notices WHERE subject_id = %s",
                (exec_id,),
            )
            nrow = await cur.fetchone()
            assert nrow is not None and nrow["cnt"] == 0

    # Phase 2: Re-settle, but simulate worker 1 crashing right before _after_settling runs
    async def _crash_after_settling(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated worker crash before _after_settling")

    monkeypatch.setattr(
        settle_mod,
        "_after_settling",
        create_autospec(settle_mod._after_settling, side_effect=_crash_after_settling),
    )
    with pytest.raises(RuntimeError, match="simulated worker crash"):
        await settle_mod.settle(
            automation,
            exec_id,
            outcomes_mod.Outcome.SUBSCRIPTION_LIMITED,
            error="Quota exhausted",
        )

    # Execution is now 'failed' AND strictly 1 durable notice + 1 target exist in PG
    async with await _connect_autocommit(migrated_dsn) as check_conn:
        async with check_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status FROM automation_executions WHERE automation_execution_id = %s::uuid",
                (exec_id,),
            )
            erow2 = await cur.fetchone()
            assert erow2 is not None and erow2["status"] == "failed"
            await cur.execute(
                """
                SELECT notice_id::text AS notice_id, aggregate_status
                FROM dalton_failure_notices
                WHERE subject_id = %s
                """,
                (exec_id,),
            )
            notices = list(await cur.fetchall() or [])
            assert len(notices) == 1
            notice_id = notices[0]["notice_id"]
            assert notices[0]["aggregate_status"] == "retrying"
            await cur.execute(
                """
                SELECT target_receipt_id::text AS target_receipt_id
                FROM dalton_failure_notice_targets
                WHERE notice_id = %s::uuid
                """,
                (notice_id,),
            )
            trows = list(await cur.fetchall() or [])
            assert len(trows) == 1
            tr_id = trows[0]["target_receipt_id"]

    # Phase 3: Independent Worker 2 runs formal process_due_failure_notices (autocommit=True)
    sent_calls: list[tuple[str, str]] = []

    async def _fake_post(
        target_receipt_id: str,
        claim_token: str,
        *,
        target: str,
        **_kw: Any,
    ) -> dict[str, Any]:
        _ = target
        sent_calls.append((target_receipt_id, claim_token))
        async with await _connect_autocommit(migrated_dsn) as gw_conn:
            claim = await claims_mod.authorize_and_claim_failure_notice(
                target_receipt_id=target_receipt_id,
                claim_token=claim_token,
                conn=gw_conn,
            )
            assert claim["claim_status"] == "claimed"
            async with gw_conn.transaction():
                async with gw_conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE dalton_channel_deliveries
                        SET status = 'sent', sent_chunks = 1, updated_at = NOW()
                        WHERE delivery_key = %s
                        """,
                        (fr_mod.failure_notice_delivery_key(target_receipt_id),),
                    )
        return {"ok": True, "status": "sent", "items_sent": 1, "items_total": 1}

    post_spy = create_autospec(fr_mod.post_failure_notice_gateway_notify, side_effect=_fake_post)
    async with await _connect_autocommit(migrated_dsn) as w2_conn:
        stats = await fr_mod.process_due_failure_notices(limit=10, post_fn=post_spy, conn=w2_conn)
        assert stats["sent"] >= 1
        async with w2_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT aggregate_status FROM dalton_failure_notices WHERE notice_id = %s::uuid",
                (notice_id,),
            )
            assert (await cur.fetchone())["aggregate_status"] == "delivered"
            await cur.execute(
                "SELECT status FROM dalton_failure_notice_targets WHERE target_receipt_id = %s::uuid",
                (tr_id,),
            )
            assert (await cur.fetchone())["status"] == "sent"
    assert any(tid == tr_id for tid, _ in sent_calls)


@pytest.mark.asyncio
async def test_lease_expiry_reconcile_reclaim_denies_late_token_without_duplicate_send(
    migrated_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After lease expiry + reconciliation + re-claim, the late old claim_token is denied by PG
    while the new claim_token sends cleanly once."""
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    user_id = f"fn_lease_{uuid4().hex[:8]}"
    open_id = f"ou_lease_{uuid4().hex[:8]}"
    auto_id = str(uuid4())
    exec_id = str(uuid4())

    async with await _connect_autocommit(migrated_dsn) as conn:
        await _seed_user_and_identity(conn, user_id=user_id, feishu_open_id=open_id)
        async with conn.transaction():
            enq = await fr_mod.enqueue_failure_notice_in_tx(
                conn,
                subject_kind="personal_automation",
                subject_id=exec_id,
                source_key=auto_id,
                recipient_user_id=user_id,
                failure_reason="claude_plan",
                targets=[f"feishu:user:{open_id}"],
            )
        assert enq is not None
        tr_id = enq["target_receipt_ids"][0]

    # Worker 1 claims attempt 1, then stalls past the 60s lease without reaching gateway
    async with await _connect_autocommit(migrated_dsn) as w1_conn:
        c1 = await fr_mod.claim_due_failure_notice_targets(limit=20, conn=w1_conn)
    row1 = next(r for r in c1 if r["target_receipt_id"] == tr_id)
    old_token = row1["claim_token"]

    # Reconciler runs after >60s -> clears old token and moves target to retryable_failed
    async with await _connect_autocommit(migrated_dsn) as rec_conn:
        future_now = datetime.now(timezone.utc) + timedelta(seconds=90)
        stats = await fr_mod.reconcile_uncertain_failure_notice_targets(now=future_now, conn=rec_conn)
        assert stats["retryable_failed"] >= 1
        # Make retry immediately due so Worker 2 can re-claim
        async with rec_conn.transaction():
            async with rec_conn.cursor() as cur:
                await cur.execute(
                    "UPDATE dalton_failure_notice_targets SET next_retry_at = NOW() WHERE target_receipt_id = %s::uuid",
                    (tr_id,),
                )

    # Worker 2 re-claims with a fresh token
    async with await _connect_autocommit(migrated_dsn) as w2_conn:
        c2 = await fr_mod.claim_due_failure_notice_targets(limit=20, conn=w2_conn)
    row2 = next(r for r in c2 if r["target_receipt_id"] == tr_id)
    new_token = row2["claim_token"]
    assert new_token != old_token
    assert row2["attempt_count"] == 2

    # Late request carrying old_token is rejected by real PG
    async with await _connect_autocommit(migrated_dsn) as gw_conn:
        denied = await claims_mod.authorize_and_claim_failure_notice(
            target_receipt_id=tr_id,
            claim_token=old_token,
            conn=gw_conn,
        )
        assert denied["claim_status"] == "denied"
        assert denied["code"] == "claim_expired_or_superseded"

        # Fresh token claims and completes once
        claimed = await claims_mod.authorize_and_claim_failure_notice(
            target_receipt_id=tr_id,
            claim_token=new_token,
            conn=gw_conn,
        )
        assert claimed["claim_status"] == "claimed"
        async with gw_conn.transaction():
            async with gw_conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_channel_deliveries
                    SET status = 'sent', sent_chunks = 1, updated_at = NOW()
                    WHERE delivery_key = %s
                    """,
                    (fr_mod.failure_notice_delivery_key(tr_id),),
                )
        # Re-checking delivery_key returns already_completed (zero duplicate send)
        replay = await claims_mod.authorize_and_claim_failure_notice(
            target_receipt_id=tr_id,
            claim_token=new_token,
            conn=gw_conn,
        )
        assert replay["claim_status"] == "already_completed"


@pytest.mark.asyncio
async def test_process_due_failure_notices_claims_one_target_at_a_time_under_budget(
    migrated_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When time budget expires after target 1, remaining targets stay ``pending`` with ``attempt_count=0``."""
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    user_id = f"fn_pertgt_{uuid4().hex[:8]}"
    open_id = f"ou_pertgt_{uuid4().hex[:8]}"
    auto_id = str(uuid4())
    exec_id = str(uuid4())

    async with await _connect_autocommit(migrated_dsn) as conn:
        await _seed_user_and_identity(conn, user_id=user_id, feishu_open_id=open_id)
        async with conn.transaction():
            enq = await fr_mod.enqueue_failure_notice_in_tx(
                conn,
                subject_kind="personal_automation",
                subject_id=exec_id,
                source_key=auto_id,
                recipient_user_id=user_id,
                failure_reason="plan_limit",
                targets=[
                    f"feishu:user:{open_id}",
                    "discord:user:987654321098765432",
                ],
            )
        assert enq is not None and len(enq["target_receipt_ids"]) == 2
        tr_ids = list(enq["target_receipt_ids"])

    clock = {"now": 500.0}
    monkeypatch.setattr(fr_mod.time, "monotonic", lambda: clock["now"])
    dispatched: list[str] = []

    async def _slow_post(
        target_receipt_id: str,
        claim_token: str,
        *,
        target: str,
        **_kw: Any,
    ) -> dict[str, Any]:
        _ = claim_token, target
        dispatched.append(target_receipt_id)
        clock["now"] += 10.0  # exhaust 5s cycle budget
        return {"ok": True, "status": "sent", "items_sent": 1, "items_total": 1}

    post_spy = create_autospec(fr_mod.post_failure_notice_gateway_notify, side_effect=_slow_post)
    async with await _connect_autocommit(migrated_dsn) as w_conn:
        stats = await fr_mod.process_due_failure_notices(
            limit=10,
            max_duration_s=5.0,
            post_fn=post_spy,
            conn=w_conn,
        )
        assert stats["claimed"] == 1
        assert len(dispatched) == 1
        other_id = next(tid for tid in tr_ids if tid != dispatched[0])
        async with w_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT status, attempt_count, claim_token
                FROM dalton_failure_notice_targets
                WHERE target_receipt_id = %s::uuid
                """,
                (other_id,),
            )
            other = await cur.fetchone()
            assert other is not None
            assert other["status"] == "pending"
            assert other["attempt_count"] == 0
            assert other["claim_token"] is None


@pytest.mark.asyncio
async def test_dual_worker_concurrent_failures_dedup_atomically_via_partial_unique_index(
    migrated_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent workers settling failures for the same source yield strictly 1 retrying + 1 suppressed."""
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    user_id = f"fn_race_{uuid4().hex[:8]}"
    open_id = f"ou_race_{uuid4().hex[:8]}"
    source_id = str(uuid4())
    exec_a, exec_b = str(uuid4()), str(uuid4())

    async with await _connect_autocommit(migrated_dsn) as seed_conn:
        await _seed_user_and_identity(seed_conn, user_id=user_id, feishu_open_id=open_id)

    barrier = asyncio.Event()

    async def _worker(subj_id: str) -> dict[str, Any] | None:
        async with await _connect_autocommit(migrated_dsn) as w_conn:
            await barrier.wait()
            async with w_conn.transaction():
                return await fr_mod.enqueue_failure_notice_in_tx(
                    w_conn,
                    subject_kind="personal_automation",
                    subject_id=subj_id,
                    source_key=source_id,
                    recipient_user_id=user_id,
                    failure_reason="usage_limit",
                    targets=[f"feishu:user:{open_id}"],
                )

    t1 = asyncio.create_task(_worker(exec_a))
    t2 = asyncio.create_task(_worker(exec_b))
    await asyncio.sleep(0.05)
    barrier.set()
    res_a, res_b = await asyncio.gather(t1, t2)
    assert res_a is not None and res_b is not None
    statuses = sorted([res_a["aggregate_status"], res_b["aggregate_status"]])
    assert statuses == ["retrying", "suppressed"]
    total_targets = len(res_a["target_receipt_ids"]) + len(res_b["target_receipt_ids"])
    assert total_targets == 1


@pytest.mark.asyncio
async def test_concurrent_automation_finish_and_sync_guard_preserves_delivery_result_and_excerpt(
    migrated_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent ``automation_finish`` ``record_delivery`` + failure-notice dispatch +
    ``sync_terminal_undelivered_execution`` never overwrites ``delivery_result`` or ``result_excerpt``
    on a failed execution."""
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    _bind_pool_to_dsn(monkeypatch, migrated_dsn)

    user_id = f"fn_fin_{uuid4().hex[:8]}"
    open_id = f"ou_fin_{uuid4().hex[:8]}"
    auto_id = str(uuid4())
    exec_id = str(uuid4())
    original_excerpt = "Original failure excerpt: quota reached"
    finish_delivery_record = [
        {
            "method": "feishu:@me",
            "address": f"feishu:user:{open_id}",
            "name": "Alice DM",
            "success": True,
            "via": "notice",
            "error": None,
        }
    ]

    async with await _connect_autocommit(migrated_dsn) as conn:
        await _seed_user_and_identity(conn, user_id=user_id, feishu_open_id=open_id)
        await _seed_automation_and_execution(
            conn,
            user_id=user_id,
            automation_id=auto_id,
            execution_id=exec_id,
            status="failed",
            delivery_result=None,
            result_excerpt=original_excerpt,
        )
        async with conn.transaction():
            enq = await fr_mod.enqueue_failure_notice_in_tx(
                conn,
                subject_kind="personal_automation",
                subject_id=exec_id,
                source_key=auto_id,
                recipient_user_id=user_id,
                failure_reason="plan_limit",
                targets=[f"feishu:user:{open_id}"],
            )
        assert enq is not None

    async def _concurrent_automation_finish_write() -> None:
        async with await _connect_autocommit(migrated_dsn) as c:
            await exec_db.record_delivery(exec_id, finish_delivery_record, conn=c)

    async def _concurrent_sync_undelivered() -> bool:
        async with await _connect_autocommit(migrated_dsn) as c:
            return await dispatch_mod.sync_terminal_undelivered_execution(
                exec_id,
                conn=c,
                rows=[],
                fallback_entry=("feishu", f"feishu:user:{open_id}", "not_allowed"),
            )

    _, updated = await asyncio.gather(
        _concurrent_automation_finish_write(),
        _concurrent_sync_undelivered(),
    )
    assert updated is False

    # Re-run sync_terminal_undelivered_execution AFTER record_delivery committed too
    async with await _connect_autocommit(migrated_dsn) as check_conn:
        updated_after = await dispatch_mod.sync_terminal_undelivered_execution(
            exec_id,
            conn=check_conn,
            rows=[],
            fallback_entry=("feishu", f"feishu:user:{open_id}", "not_allowed"),
        )
        assert updated_after is False
        async with check_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT delivery_result, result_excerpt
                FROM automation_executions
                WHERE automation_execution_id = %s::uuid
                """,
                (exec_id,),
            )
            row = await cur.fetchone()
            assert row is not None
            assert row["delivery_result"] == finish_delivery_record
            assert row["result_excerpt"] == original_excerpt


@pytest.mark.asyncio
async def test_concurrent_weixin_failure_notice_retry_and_unbind_zero_deadlock(
    migrated_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent Weixin failure-notice retry claim + ``unbind_weixin_account`` complete cleanly
    with zero PostgreSQL deadlock (both acquire ``dalton_channel_weixin_accounts`` before
    ``dalton_channel_deliveries``)."""
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    _bind_pool_to_dsn(monkeypatch, migrated_dsn)

    user_id = f"fn_wx_{uuid4().hex[:8]}"
    wx_uid = f"wx_{uuid4().hex[:8]}@im.wechat"
    bot_id = f"bot_{uuid4().hex[:8]}@im.wechat"
    account_id = str(uuid4())
    auto_id = str(uuid4())
    exec_id = str(uuid4())
    enc_key = enc_mod.get_encryption_key()

    async with await _connect_autocommit(migrated_dsn) as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO users (user_id, email, name, created_at, updated_at)
                    VALUES (%s, %s, %s, NOW(), NOW())
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    (user_id, f"{user_id}@test.local", f"Test {user_id}"),
                )
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_weixin_accounts (
                        account_id, user_id, ilink_bot_id, ilink_user_id,
                        encrypted_bot_token, status, bot_generation
                    )
                    VALUES (%s::uuid, %s, %s, %s, pgp_sym_encrypt(%s, %s), 'active', 1)
                    """,
                    (account_id, user_id, bot_id, wx_uid, "wx-bot-tok-test", enc_key),
                )
                enq = await fr_mod.enqueue_failure_notice_in_tx(
                    conn,
                    subject_kind="personal_automation",
                    subject_id=exec_id,
                    source_key=auto_id,
                    recipient_user_id=user_id,
                    failure_reason="plan_limit",
                    targets=[f"weixin:user:{wx_uid}"],
                )
        assert enq is not None
        tr_id = enq["target_receipt_ids"][0]
        claimed = await fr_mod.claim_due_failure_notice_targets(limit=20, conn=conn)
        tok = next(r["claim_token"] for r in claimed if r["target_receipt_id"] == tr_id)

        # Pre-insert a failed delivery row referencing account_id (so DELETE triggers FK update)
        d_key = fr_mod.failure_notice_delivery_key(tr_id)
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_deliveries (
                        delivery_key, kind, user_id, thread_id, run_id, execution_id,
                        is_current, has_non_empty_text, platform, target_address,
                        status, total_chunks, sent_chunks, platform_message_ids,
                        files_status, weixin_account_id, weixin_bot_id,
                        weixin_bot_generation, weixin_recipient_id,
                        created_at, updated_at
                    )
                    VALUES (
                        %s, 'failure_notice', %s, NULL, %s, %s::uuid,
                        FALSE, TRUE, 'weixin', 'weixin:@me',
                        'failed', 1, 0, '[]'::jsonb,
                        '[]'::jsonb, %s::uuid, %s,
                        1, %s, NOW(), NOW()
                    )
                    """,
                    (d_key, user_id, tr_id, exec_id, account_id, bot_id, wx_uid),
                )

    barrier = asyncio.Event()

    async def _run_claim_retry() -> dict[str, Any]:
        async with await _connect_autocommit(migrated_dsn) as c:
            await barrier.wait()
            return await claims_mod.authorize_and_claim_failure_notice(
                target_receipt_id=tr_id,
                claim_token=tok,
                conn=c,
            )

    async def _run_unbind() -> dict[str, Any]:
        async with await _connect_autocommit(migrated_dsn) as c:
            await barrier.wait()
            return await wx_db.unbind_weixin_account(
                account_id=account_id,
                operator_user_id=user_id,
                action="member_unbind",
                conn=c,
            )

    t_claim = asyncio.create_task(_run_claim_retry())
    t_unbind = asyncio.create_task(_run_unbind())
    await asyncio.sleep(0.05)
    barrier.set()
    claim_res, unbind_res = await asyncio.gather(t_claim, t_unbind)
    # Neither may raise psycopg.errors.DeadlockDetected
    assert isinstance(unbind_res, dict) and unbind_res.get("ok") is True
    assert isinstance(claim_res, dict)
    assert claim_res.get("claim_status") in ("claimed", "denied")


def test_real_pg_alembic_d061_d062_round_trip(migrated_dsn: str) -> None:
    """Real PG Alembic round-trip: downgrade d062 -> d061 and re-upgrade d061 -> d062."""
    d_cfg = AlembicConfig(str(REPO_ROOT / "src" / "dalton" / "alembic.ini"))
    d_cfg.set_main_option("script_location", str(REPO_ROOT / "src" / "dalton" / "migrations"))
    d_cfg.set_main_option("sqlalchemy.url", _to_sa_url(migrated_dsn))

    alembic_command.downgrade(d_cfg, "d061")
    with psycopg.connect(migrated_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.dalton_failure_notices')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.dalton_failure_notice_targets')")
            assert cur.fetchone()[0] is None

    alembic_command.upgrade(d_cfg, "head")
    with psycopg.connect(migrated_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.dalton_failure_notices')")
            assert cur.fetchone()[0] == "dalton_failure_notices"
            cur.execute("SELECT to_regclass('public.dalton_failure_notice_streaks')")
            assert cur.fetchone()[0] == "dalton_failure_notice_streaks"
            cur.execute("SELECT to_regclass('public.dalton_failure_notice_targets')")
            assert cur.fetchone()[0] == "dalton_failure_notice_targets"
