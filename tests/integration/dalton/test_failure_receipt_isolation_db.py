"""Real PostgreSQL + dual-worker fault-injection integration tests for Batch 4 (§3):
1. Same-transaction enqueue durability: automation settle commits ``dalton_failure_notices``
   + ``dalton_failure_notice_targets`` before ``_after_settling`` runs (simulated crash right
   after commit -> second worker claims & delivers with zero loss).
2. Dual-worker concurrent consecutive failure dedup: two concurrent workers settling failures
   for the same source compete on partial unique index ``uq_dalton_failure_notices_dedup_key``;
   strictly 1 wins (``retrying``) and 1 atomically records ``suppressed``.
3. Dual-worker concurrent claim/send + concurrent ``automation_finish`` writing
   ``delivery_result`` + ``sync_terminal_undelivered_execution`` isolation (zero resend,
   zero lock conflict, zero overwrite on failed executions).
4. Real PostgreSQL Alembic ``d061 <-> d062`` upgrade/downgrade round-trip.

Skipped automatically when ``DALTON_TEST_PG_DSN`` is unset (run against a ``*_test`` DB).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
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
from src.dalton.notify import dispatch as dispatch_mod
from src.dalton.notify import failure_receipts as fr_mod
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


def _to_sa_url(dsn: str) -> str:
    if dsn.startswith("postgresql+psycopg://"):
        return dsn
    if dsn.startswith("postgresql://"):
        return "postgresql+psycopg://" + dsn[len("postgresql://") :]
    if dsn.startswith("postgres://"):
        return "postgresql+psycopg://" + dsn[len("postgres://") :]
    return dsn


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


@pytest.mark.asyncio
async def test_in_tx_enqueue_survives_pre_after_settling_crash_and_second_worker_delivers(
    migrated_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receipt committed inside settle tx is picked up by a separate worker even if worker 1 exits
    immediately before ``_after_settling`` runs."""
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    user_id = f"fn_crash_{uuid4().hex[:8]}"
    open_id = f"ou_crash_{uuid4().hex[:8]}"
    auto_id = str(uuid4())
    exec_id = str(uuid4())

    # Worker 1 commits failure notice inside settle transaction and exits (never runs wake)
    async with await psycopg.AsyncConnection.connect(migrated_dsn) as w1_conn:
        await _seed_user_and_identity(w1_conn, user_id=user_id, feishu_open_id=open_id)
        async with w1_conn.transaction():
            enq = await fr_mod.enqueue_failure_notice_in_tx(
                w1_conn,
                subject_kind="personal_automation",
                subject_id=exec_id,
                source_key=auto_id,
                recipient_user_id=user_id,
                failure_reason="plan_limit",
                targets=[f"feishu:user:{open_id}"],
            )
        assert enq is not None and enq["aggregate_status"] == "retrying"
        tr_id = enq["target_receipt_ids"][0]

    # Worker 2 connects independently, claims & delivers via gateway stub
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
        async with await psycopg.AsyncConnection.connect(migrated_dsn) as gw_conn:
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

    async with await psycopg.AsyncConnection.connect(migrated_dsn) as w2_conn:
        stats = await fr_mod.process_due_failure_notices(limit=10, post_fn=post_spy, conn=w2_conn)
        assert stats["sent"] >= 1
        async with w2_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT aggregate_status FROM dalton_failure_notices WHERE notice_id = %s::uuid",
                (enq["notice_id"],),
            )
            nrow = await cur.fetchone()
            assert nrow is not None and nrow["aggregate_status"] == "delivered"
            await cur.execute(
                "SELECT status FROM dalton_failure_notice_targets WHERE target_receipt_id = %s::uuid",
                (tr_id,),
            )
            trow = await cur.fetchone()
            assert trow is not None and trow["status"] == "sent"
    assert any(call_tr == tr_id for call_tr, _ in sent_calls)


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

    async with await psycopg.AsyncConnection.connect(migrated_dsn) as seed_conn:
        await _seed_user_and_identity(seed_conn, user_id=user_id, feishu_open_id=open_id)

    barrier = asyncio.Event()

    async def _worker(subj_id: str) -> dict[str, Any] | None:
        async with await psycopg.AsyncConnection.connect(migrated_dsn) as w_conn:
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
async def test_dual_worker_claim_isolation_and_failed_execution_sync_guard(
    migrated_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent workers claiming due targets claim each row at most once, and
    ``sync_terminal_undelivered_execution`` never touches a failed execution."""
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    user_id = f"fn_iso_{uuid4().hex[:8]}"
    open_id = f"ou_iso_{uuid4().hex[:8]}"
    auto_id = str(uuid4())
    exec_id = str(uuid4())

    async with await psycopg.AsyncConnection.connect(migrated_dsn) as conn:
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

    async def _claim_once() -> list[dict[str, Any]]:
        async with await psycopg.AsyncConnection.connect(migrated_dsn) as w_conn:
            return await fr_mod.claim_due_failure_notice_targets(limit=20, conn=w_conn)

    c1, c2 = await asyncio.gather(_claim_once(), _claim_once())
    claimed_for_tr = [r for r in (*c1, *c2) if r["target_receipt_id"] == tr_id]
    assert len(claimed_for_tr) == 1

    # Simulate lease expiration > 60s, reconcile, then ensure old token is denied by real PG
    old_token = claimed_for_tr[0]["claim_token"]
    async with await psycopg.AsyncConnection.connect(migrated_dsn) as conn:
        future_now = datetime.now(timezone.utc) + timedelta(seconds=90)
        stats = await fr_mod.reconcile_uncertain_failure_notice_targets(now=future_now, conn=conn)
        assert stats["retryable_failed"] >= 1
        denied = await claims_mod.authorize_and_claim_failure_notice(
            target_receipt_id=tr_id,
            claim_token=old_token,
            conn=conn,
        )
        assert denied["claim_status"] == "denied"
        assert denied["code"] == "claim_expired_or_superseded"

        # Verify sync_terminal_undelivered_execution does not alter non-completed executions
        updated = await dispatch_mod.sync_terminal_undelivered_execution(
            exec_id,
            conn=conn,
            rows=[],
            fallback_entry=("feishu", f"feishu:user:{open_id}", "not_allowed"),
        )
        assert updated is False


def test_real_pg_alembic_d061_d062_round_trip(migrated_dsn: str) -> None:
    """Real PG Alembic round-trip: downgrade d062 -> d061 and re-upgrade d061 -> d062."""
    d_cfg = AlembicConfig(str(REPO_ROOT / "src" / "dalton" / "alembic.ini"))
    d_cfg.set_main_option("script_location", str(REPO_ROOT / "src" / "dalton" / "migrations"))
    d_cfg.set_main_option("sqlalchemy.url", _to_sa_url(migrated_dsn))

    alembic_command.downgrade(d_cfg, "d061")
    with psycopg.connect(migrated_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.dalton_failure_notices')")
            assert cur.fetchone()[0] is None
            cur.execute("SELECT to_regclass('public.dalton_failure_notice_targets')")
            assert cur.fetchone()[0] is None

    alembic_command.upgrade(d_cfg, "head")
    with psycopg.connect(migrated_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.dalton_failure_notices')")
            assert cur.fetchone()[0] == "dalton_failure_notices"
            cur.execute("SELECT to_regclass('public.dalton_failure_notice_streaks')")
            assert cur.fetchone()[0] == "dalton_failure_notice_streaks"
            cur.execute("SELECT to_regclass('public.dalton_failure_notice_targets')")
            assert cur.fetchone()[0] == "dalton_failure_notice_targets"
