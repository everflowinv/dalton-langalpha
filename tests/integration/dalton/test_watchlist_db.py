"""Real PostgreSQL migration & concurrency integration tests for Dalton d056 (personal watchlist).

Covers:
1. Empty-database full upgrade to head in an isolated temporary database.
2. Existing-data upgrade (`d055` -> `d056`) -> safe `downgrade` (`d056` -> `d055`)
   -> re-`upgrade` (`d055` -> `d056`) verifying `downgraded_silent`/`downgraded_from_tier='radar'`
   restores rows back to `radar` on re-upgrade, all in an isolated temporary database.
3. Two concurrent `reconcile_job` callers racing on the same pending outbox job against
   real PostgreSQL (verifying atomic exclusive claim via `claim_reconcile_job`).

Skipped automatically when ``DALTON_TEST_PG_DSN`` is not set (run outside sandbox by Lead).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parents[3]
PG_DSN = os.getenv("DALTON_TEST_PG_DSN", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_DSN,
        reason="DALTON_TEST_PG_DSN is not set; skipping real PostgreSQL d056 watchlist integration tests",
    ),
]


def _replace_dsn_dbname(dsn: str, new_db: str) -> str:
    parsed = urlparse(dsn)
    return urlunparse(parsed._replace(path=f"/{new_db}"))


@contextmanager
def _isolated_pg_database(base_dsn: str) -> Iterator[str]:
    """Create a fresh temporary PostgreSQL database for one test and drop it unconditionally."""
    db_name = f"dalton_wl_{uuid.uuid4().hex[:12]}"
    admin_dsn = base_dsn
    with psycopg.connect(admin_dsn, autocommit=True) as admin_conn:
        with admin_conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
    iso_dsn = _replace_dsn_dbname(base_dsn, db_name)
    try:
        yield iso_dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin_conn:
            with admin_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = %s AND pid <> pg_backend_pid()
                    """,
                    (db_name,),
                )
                cur.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name))
                )


def _run_alembic(args: list[str], *, env: dict[str, str], dalton: bool = True) -> str:
    cmd = [sys.executable, "-m", "alembic"]
    if dalton:
        cmd.extend(["-c", "src/dalton/alembic.ini"])
    cmd.extend(args)
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Alembic command failed: {' '.join(cmd)}\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    return proc.stdout


def _alembic_env_for_dsn(dsn: str) -> dict[str, str]:
    parsed = urlparse(dsn)
    env = os.environ.copy()
    env["DATABASE_URL"] = dsn
    if parsed.hostname:
        env["DB_HOST"] = parsed.hostname
    if parsed.port:
        env["DB_PORT"] = str(parsed.port)
    if parsed.username:
        env["DB_USER"] = parsed.username
    if parsed.password is not None:
        env["DB_PASSWORD"] = parsed.password
    if parsed.path and parsed.path.lstrip("/"):
        env["DB_NAME"] = parsed.path.lstrip("/")
    return env


@asynccontextmanager
async def _isolated_pool(iso_dsn: str, *, max_size: int = 4):
    """Open an AsyncConnectionPool for ``iso_dsn`` and wire it into ``pool.get_or_create_pool``."""
    from psycopg_pool import AsyncConnectionPool
    import src.server.database.pool as pool_mod

    pool = AsyncConnectionPool(
        conninfo=iso_dsn,
        min_size=1,
        max_size=max_size,
        configure=pool_mod._configure_postgres_connection,
        open=False,
    )
    await pool.open()
    orig_get_pool = pool_mod.get_or_create_pool
    orig_cache = pool_mod._conversation_db_pool_cache
    pool_mod.get_or_create_pool = create_autospec(orig_get_pool, return_value=pool)
    pool_mod._conversation_db_pool_cache = {iso_dsn: pool}
    try:
        yield pool
    finally:
        pool_mod.get_or_create_pool = orig_get_pool
        pool_mod._conversation_db_pool_cache = orig_cache
        await pool.close()


def test_d056_single_head_and_fresh_upgrade_schema() -> None:
    """Verify Dalton migration chain has a single head `d056` and all watchlist tables/constraints exist."""
    with _isolated_pg_database(PG_DSN) as iso_dsn:
        env = _alembic_env_for_dsn(iso_dsn)

        heads_out = _run_alembic(["heads"], env=env, dalton=True)
        head_lines = [ln.strip() for ln in heads_out.splitlines() if ln.strip()]
        assert len(head_lines) == 1, f"Expected single Dalton head, got: {head_lines}"
        assert head_lines[0].startswith("d056"), f"Expected head d056, got: {head_lines}"

        _run_alembic(["upgrade", "head"], env=env, dalton=False)
        _run_alembic(["upgrade", "head"], env=env, dalton=True)

        with psycopg.connect(iso_dsn, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name IN (
                          'dalton_watchlist_tier_policies',
                          'dalton_watchlist_reconcile_jobs',
                          'dalton_watchlist_confirm_snapshots',
                          'dalton_watchlist_user_state',
                          'dalton_watchlist_views',
                          'dalton_watchlist_custom_columns'
                      )
                    """
                )
                tables = {r[0] for r in cur.fetchall()}
                assert tables == {
                    "dalton_watchlist_tier_policies",
                    "dalton_watchlist_reconcile_jobs",
                    "dalton_watchlist_confirm_snapshots",
                    "dalton_watchlist_user_state",
                    "dalton_watchlist_views",
                    "dalton_watchlist_custom_columns",
                }

                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'dalton_coverage'
                      AND column_name IN ('tags', 'sort_order', 'is_pinned', 'is_private', 'task_overrides')
                    """
                )
                cols = {r[0] for r in cur.fetchall()}
                assert cols == {"tags", "sort_order", "is_pinned", "is_private", "task_overrides"}

                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'dalton_watchlist_reconcile_jobs'
                      AND column_name IN ('job_context', 'claim_token', 'claim_expires_at', 'roster_revision')
                    """
                )
                assert {r[0] for r in cur.fetchall()} == {
                    "job_context",
                    "claim_token",
                    "claim_expires_at",
                    "roster_revision",
                }

                # Verify invalid tier is rejected by CHECK constraint
                bad_uid = f"d056-bad-tier-{uuid.uuid4().hex[:8]}"
                with pytest.raises(psycopg.errors.CheckViolation):
                    cur.execute(
                        """
                        INSERT INTO dalton_coverage (user_id, ticker, tier)
                        VALUES (%s, 'BAD1', 'invalid_tier')
                        """,
                        (bad_uid,),
                    )
                conn.rollback()


def test_d056_upgrade_backfill_and_downgrade_with_radar_samples() -> None:
    """Verify d055 -> d056 backfill (OPENAI, manual ws, rule groups, legacy tags), radar-safe downgrade,
    and re-upgrade restoring `downgraded_silent`/`downgraded_from_tier='radar'` back to `radar`."""
    with _isolated_pg_database(PG_DSN) as iso_dsn:
        env = _alembic_env_for_dsn(iso_dsn)
        _run_alembic(["upgrade", "head"], env=env, dalton=False)
        _run_alembic(["upgrade", "d055"], env=env, dalton=True)

        test_uid = f"d056-sample-{uuid.uuid4().hex[:8]}"
        group_id = str(uuid.uuid4())
        radar_group_id = str(uuid.uuid4())
        manual_ws_id = str(uuid.uuid4())

        with psycopg.connect(iso_dsn, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO dalton_coverage_groups (group_id, user_id, label, criteria, default_tier)
                    VALUES (%s::uuid, %s, 'US Tech', '{}'::jsonb, 'watch')
                    """,
                    (group_id, test_uid),
                )
                cur.execute(
                    """
                    INSERT INTO dalton_coverage (user_id, ticker, name, tier, market, extra, source)
                    VALUES (%s, 'OPENAI', 'OpenAI', 'core', 'US', '{}'::jsonb, 'seed')
                    """,
                    (test_uid,),
                )
                cur.execute(
                    """
                    INSERT INTO dalton_coverage (user_id, ticker, name, tier, market, extra, source)
                    VALUES (%s, 'AAPL', 'Apple', 'core', 'US', %s::jsonb, 'chat')
                    """,
                    (
                        test_uid,
                        json.dumps(
                            {
                                "_manual_workspace_id": manual_ws_id,
                                "note": "keep-me",
                                "tags": ["mega", "hardware"],
                            }
                        ),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO dalton_coverage (user_id, ticker, name, tier, market, extra, source, group_id)
                    VALUES (%s, 'MSFT', 'Microsoft', 'watch', 'US', %s::jsonb, 'group', %s::uuid)
                    """,
                    (test_uid, json.dumps({"group_ids": [group_id]}), group_id),
                )
            conn.commit()

        # Upgrade d055 -> d056
        _run_alembic(["upgrade", "d056"], env=env, dalton=True)

        with psycopg.connect(iso_dsn, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tier, is_private FROM dalton_coverage WHERE user_id = %s AND ticker = 'OPENAI'",
                    (test_uid,),
                )
                assert cur.fetchone() == ("core", True)

                cur.execute(
                    "SELECT tier, is_private, tags, extra FROM dalton_coverage WHERE user_id = %s AND ticker = 'AAPL'",
                    (test_uid,),
                )
                aapl_tier, aapl_priv, aapl_tags, aapl_extra = cur.fetchone()
                assert aapl_tier == "core"
                assert aapl_priv is False
                assert aapl_tags == ["mega", "hardware"]
                assert aapl_extra["_manual_workspace_id"] == manual_ws_id
                assert aapl_extra["note"] == "keep-me"

                cur.execute(
                    "SELECT tier, group_id::text FROM dalton_coverage WHERE user_id = %s AND ticker = 'MSFT'",
                    (test_uid,),
                )
                assert cur.fetchone() == ("watch", group_id)

                # Insert radar ticker and radar rule group
                cur.execute(
                    """
                    INSERT INTO dalton_coverage_groups (group_id, user_id, label, criteria, default_tier)
                    VALUES (%s::uuid, %s, 'AI Radar', '{}'::jsonb, 'radar')
                    """,
                    (radar_group_id, test_uid),
                )
                cur.execute(
                    """
                    INSERT INTO dalton_coverage (user_id, ticker, name, tier, market, tags, is_pinned, source, group_id)
                    VALUES (%s, 'NET', 'Cloudflare', 'radar', 'US', '["edge"]'::jsonb, TRUE, 'chat', %s::uuid)
                    """,
                    (test_uid, radar_group_id),
                )

                cur.execute(
                    """
                    INSERT INTO dalton_watchlist_tier_policies (
                        user_id, tier, watch_enabled, watch_cron,
                        delta_enabled, delta_cron, delta_timezone,
                        move_enabled, move_threshold_pct, earnings_stages
                    )
                    VALUES (%s, 'radar', FALSE, '30 7 * * 1-5', FALSE, '0 11 * * 6', 'Asia/Shanghai', FALSE, 6.00, '[]'::jsonb)
                    """,
                    (test_uid,),
                )
                cur.execute(
                    """
                    INSERT INTO dalton_watchlist_reconcile_jobs (
                        job_id, user_id, trigger_action, pending_tickers, job_context
                    )
                    VALUES (%s::uuid, %s, 'add', '["NET"]'::jsonb, '{"removed_workspaces":{}}'::jsonb)
                    """,
                    (str(uuid.uuid4()), test_uid),
                )
            conn.commit()

        # Downgrade d056 -> d055 with radar rows present
        _run_alembic(["downgrade", "d055"], env=env, dalton=True)

        with psycopg.connect(iso_dsn, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tier, extra FROM dalton_coverage WHERE user_id = %s AND ticker = 'NET'",
                    (test_uid,),
                )
                net_tier, net_extra = cur.fetchone()
                assert net_tier == "watch"
                assert net_extra.get("downgraded_from_tier") == "radar"
                assert net_extra.get("downgraded_silent") is True

                cur.execute(
                    "SELECT default_tier, criteria FROM dalton_coverage_groups WHERE group_id = %s::uuid",
                    (radar_group_id,),
                )
                dg_tier, dg_crit = cur.fetchone()
                assert dg_tier == "watch"
                assert (dg_crit or {}).get("_downgraded_default_tier") == "radar"

                cur.execute(
                    "SELECT ticker, tier FROM dalton_coverage WHERE user_id = %s ORDER BY ticker",
                    (test_uid,),
                )
                assert cur.fetchall() == [
                    ("AAPL", "core"),
                    ("MSFT", "watch"),
                    ("NET", "watch"),
                    ("OPENAI", "core"),
                ]

                with pytest.raises(psycopg.errors.CheckViolation):
                    cur.execute(
                        "INSERT INTO dalton_coverage (user_id, ticker, tier) VALUES (%s, 'CRWD', 'radar')",
                        (test_uid,),
                    )
                conn.rollback()

        # Re-upgrade d055 -> d056: NET must be restored to 'radar' and downgrade markers cleaned
        _run_alembic(["upgrade", "d056"], env=env, dalton=True)

        with psycopg.connect(iso_dsn, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tier, extra FROM dalton_coverage WHERE user_id = %s AND ticker = 'NET'",
                    (test_uid,),
                )
                restored_tier, restored_extra = cur.fetchone()
                assert restored_tier == "radar"
                assert "downgraded_from_tier" not in (restored_extra or {})
                assert "downgraded_silent" not in (restored_extra or {})

                cur.execute(
                    "SELECT is_private FROM dalton_coverage WHERE user_id = %s AND ticker = 'OPENAI'",
                    (test_uid,),
                )
                assert cur.fetchone() == (True,)

                # r4 Decision 7: radar rule group default_tier restored & _downgraded_default_tier cleaned
                cur.execute(
                    "SELECT default_tier, criteria FROM dalton_coverage_groups WHERE group_id = %s::uuid",
                    (radar_group_id,),
                )
                rg_tier, rg_crit = cur.fetchone()
                assert rg_tier == "radar"
                assert "_downgraded_default_tier" not in (rg_crit or {})

                # r4 Decision 7: re-upgrade enqueues a pending migration_restore_radar reconcile job for NET
                cur.execute(
                    """
                    SELECT job_id::text, trigger_action, pending_tickers, status
                    FROM dalton_watchlist_reconcile_jobs
                    WHERE user_id = %s AND trigger_action = 'migration_restore_radar'
                    """,
                    (test_uid,),
                )
                mig_job = cur.fetchone()
                assert mig_job is not None
                mig_job_id = mig_job[0]
                assert mig_job[1] == "migration_restore_radar"
                assert mig_job[2] == ["NET"]
                assert mig_job[3] == "pending"

                # r5: seed an active automation for NET and verify executing the restore job pauses it
                net_ws_id = str(uuid.uuid4())
                net_auto_id = str(uuid.uuid4())
                cur.execute(
                    """
                    INSERT INTO users (user_id, email, name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    (test_uid, f"{test_uid}@example.com", "Roundtrip User"),
                )
                cur.execute(
                    """
                    INSERT INTO workspaces (workspace_id, user_id, name, status)
                    VALUES (%s::uuid, %s, 'NET', 'running')
                    """,
                    (net_ws_id, test_uid),
                )
                cur.execute(
                    """
                    INSERT INTO automations (
                        automation_id, user_id, name, trigger_type, cron_expression,
                        timezone, instruction, next_run_at, workspace_id, status, metadata
                    )
                    VALUES (
                        %s::uuid, %s, 'dalton-net-watch', 'cron', '0 9 * * 1-5',
                        'America/New_York', 'Watch NET', NOW() + INTERVAL '1 day',
                        %s::uuid, 'active', '{"ticker": "NET", "dalton_managed": true}'::jsonb
                    )
                    """,
                    (net_auto_id, test_uid, net_ws_id),
                )
            conn.commit()

        # Execute the migration_restore_radar outbox job against the real isolated DB and verify task is paused
        import src.dalton.watchlist.reconciler as reconciler_mod

        async def _consume_restore_job() -> None:
            orig_sync = reconciler_mod.sync_user_coverage_to_home
            reconciler_mod.sync_user_coverage_to_home = create_autospec(
                orig_sync, return_value=True
            )
            try:
                async with _isolated_pool(iso_dsn, max_size=2):
                    res = await reconciler_mod.TierPolicyReconciler().reconcile_job(
                        test_uid, mig_job_id, sync_home=True
                    )
                    assert res["status"] == "completed"
            finally:
                reconciler_mod.sync_user_coverage_to_home = orig_sync

        asyncio.run(_consume_restore_job())

        with psycopg.connect(iso_dsn, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, next_run_at FROM automations WHERE automation_id = %s::uuid",
                    (net_auto_id,),
                )
                st_after, next_after = cur.fetchone()
                assert st_after == "paused"
                assert next_after is None


@pytest.mark.asyncio
async def test_concurrent_reconcile_job_exclusive_claim_real_pg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 7: Two concurrent `reconcile_job` calls racing on the same PG outbox row:
    only ONE worker claims and reconciles the company; the other sees `claimed=False`."""
    from src.dalton.watchlist import policies as policies_mod
    from src.dalton.watchlist import reconciler as reconciler_mod

    with _isolated_pg_database(PG_DSN) as iso_dsn:
        env = _alembic_env_for_dsn(iso_dsn)
        _run_alembic(["upgrade", "head"], env=env, dalton=False)
        _run_alembic(["upgrade", "head"], env=env, dalton=True)

        @asynccontextmanager
        async def _connect_iso(existing_conn: Any = None):
            if existing_conn is not None:
                yield existing_conn
                return
            async with await psycopg.AsyncConnection.connect(iso_dsn) as aconn:
                yield aconn

        monkeypatch.setattr(policies_mod, "get_db_connection", _connect_iso)

        uid = f"u-race-{uuid.uuid4().hex[:8]}"
        job = await policies_mod.insert_reconcile_job(
            uid,
            trigger_action="set_tier",
            target_policy_versions={"core": 1},
            pending_tickers=["NVDA"],
        )
        jid = str(job["job_id"])

        reconcile_calls: list[str] = []

        async def _slow_reconcile_company(
            self: Any,
            user_id: str,
            ticker: str,
            **kwargs: Any,
        ) -> dict[str, Any]:
            _ = (self, user_id, kwargs)
            reconcile_calls.append(ticker)
            await asyncio.sleep(0.15)
            return {"ticker": ticker, "ok": True, "actions": []}

        monkeypatch.setattr(
            reconciler_mod.TierPolicyReconciler,
            "reconcile_company",
            create_autospec(
                reconciler_mod.TierPolicyReconciler.reconcile_company,
                side_effect=_slow_reconcile_company,
            ),
        )
        monkeypatch.setattr(
            reconciler_mod,
            "sync_user_coverage_to_home",
            create_autospec(reconciler_mod.sync_user_coverage_to_home, return_value=True),
        )

        rec = reconciler_mod.TierPolicyReconciler()
        res_a, res_b = await asyncio.gather(
            rec.reconcile_job(uid, jid, sync_home=True),
            rec.reconcile_job(uid, jid, sync_home=True),
        )

        claimed_results = [r for r in (res_a, res_b) if r.get("claimed") is True]
        skipped_results = [r for r in (res_a, res_b) if r.get("claimed") is False]
        assert len(claimed_results) == 1
        assert len(skipped_results) == 1
        assert claimed_results[0]["status"] == "completed"
        assert skipped_results[0]["reason"] == "already_claimed"
        assert reconcile_calls == ["NVDA"]


@pytest.mark.asyncio
async def test_r4_startup_vs_worker_lease_and_first_failure_interleaving_real_pg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 Decision 2 (Real PG): startup scan never steals an active lease; first-ticker failure
    writes status='partial' while retaining claim_token so competing workers cannot interleave."""
    from src.dalton.watchlist import policies as policies_mod
    from src.dalton.watchlist import reconciler as reconciler_mod

    with _isolated_pg_database(PG_DSN) as iso_dsn:
        env = _alembic_env_for_dsn(iso_dsn)
        _run_alembic(["upgrade", "head"], env=env, dalton=False)
        _run_alembic(["upgrade", "head"], env=env, dalton=True)

        @asynccontextmanager
        async def _connect_iso(existing_conn: Any = None):
            if existing_conn is not None:
                yield existing_conn
                return
            async with await psycopg.AsyncConnection.connect(iso_dsn) as aconn:
                yield aconn

        monkeypatch.setattr(policies_mod, "get_db_connection", _connect_iso)

        uid = f"u-lease-{uuid.uuid4().hex[:8]}"
        job = await policies_mod.insert_reconcile_job(
            uid,
            trigger_action="set_tier",
            target_policy_versions={"core": 1},
            pending_tickers=["FAIL1", "OK2"],
            roster_revision=1,
        )
        jid = str(job["job_id"])

        first_failed_evt = asyncio.Event()
        allow_second_evt = asyncio.Event()
        calls: list[str] = []

        async def _interleaved_reconcile(self: Any, user_id: str, ticker: str, **kwargs: Any) -> dict[str, Any]:
            _ = (self, user_id, kwargs)
            calls.append(ticker)
            if ticker == "FAIL1":
                raise RuntimeError("transient first-ticker failure")
            first_failed_evt.set()
            await allow_second_evt.wait()
            return {"ticker": ticker, "ok": True, "actions": []}

        monkeypatch.setattr(
            reconciler_mod.TierPolicyReconciler,
            "reconcile_company",
            create_autospec(
                reconciler_mod.TierPolicyReconciler.reconcile_company,
                side_effect=_interleaved_reconcile,
            ),
        )
        monkeypatch.setattr(
            reconciler_mod,
            "sync_user_coverage_to_home",
            create_autospec(reconciler_mod.sync_user_coverage_to_home, return_value=True),
        )

        rec = reconciler_mod.TierPolicyReconciler()
        worker_task = asyncio.create_task(rec.reconcile_job(uid, jid, sync_home=True))
        await asyncio.wait_for(first_failed_evt.wait(), timeout=5.0)

        # At this exact point, FAIL1 failed -> row has status='partial' BUT claim_token is STILL active!
        # Neither startup scan nor another worker may claim it!
        startup_claimed = await policies_mod.claim_unfinished_reconcile_jobs_for_startup(limit=10)
        assert startup_claimed == []
        competing = await policies_mod.claim_reconcile_job(uid, jid)
        assert competing is None

        allow_second_evt.set()
        worker_res = await worker_task
        assert worker_res["status"] == "partial"
        assert worker_res["completed_tickers"] == ["OK2"]
        assert calls == ["FAIL1", "OK2"]

        # Once worker finished, claim_token is released so a retry CAN claim it
        after = await policies_mod.get_reconcile_job(uid, jid)
        assert after is not None and after["claim_token"] is None
        reclaimed = await policies_mod.claim_reconcile_job(uid, jid)
        assert reclaimed is not None and reclaimed["claim_token"] is not None


@pytest.mark.asyncio
async def test_r4_coverage_units_enabled_real_pg_tx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 Codex Low #19 / Grok r3: real PostgreSQL transaction with coverage_units enabled."""
    import src.dalton.database.coverage_units as cu_db_mod
    import src.dalton.settings as dalton_settings_mod
    import src.dalton.watchlist.reconciler as reconciler_mod

    with _isolated_pg_database(PG_DSN) as iso_dsn:
        env = _alembic_env_for_dsn(iso_dsn)
        _run_alembic(["upgrade", "head"], env=env, dalton=False)
        _run_alembic(["upgrade", "head"], env=env, dalton=True)
        monkeypatch.setattr(dalton_settings_mod, "is_coverage_units_enabled", lambda: True)

        uid = f"wl-cu-user-{uuid.uuid4().hex[:8]}"
        ws_id = str(uuid.uuid4())
        async with _isolated_pool(iso_dsn, max_size=2) as pool:
            async with pool.connection() as conn:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            INSERT INTO users (user_id, email, name)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (user_id) DO NOTHING
                            """,
                            (uid, f"{uid}@example.com", "CU Test User"),
                        )
                        await cur.execute(
                            """
                            INSERT INTO workspaces (workspace_id, user_id, name, status)
                            VALUES (%s::uuid, %s, %s, 'running')
                            """,
                            (ws_id, uid, "Custom NVIDIA Research"),
                        )
                    await cu_db_mod.insert_coverage_unit(
                        kind="company",
                        owner_user_id=uid,
                        slug="nvda",
                        display_name="NVIDIA",
                        ticker="NVDA",
                        lead_analyst_user_id=uid,
                        workspace_id=ws_id,
                        conn=conn,
                    )
                    resolved = await reconciler_mod.resolve_company_workspace_id(
                        uid, "NVDA", conn=conn
                    )
                    assert resolved == ws_id


@pytest.mark.asyncio
async def test_r5_expired_lease_takeover_blocks_old_worker_side_effects_real_pg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r5 (Real PG): Once a lease expires and Worker B takes over, Worker A aborts BEFORE executing side effects."""
    from src.dalton.watchlist import policies as policies_mod
    from src.dalton.watchlist import reconciler as reconciler_mod

    with _isolated_pg_database(PG_DSN) as iso_dsn:
        env = _alembic_env_for_dsn(iso_dsn)
        _run_alembic(["upgrade", "head"], env=env, dalton=False)
        _run_alembic(["upgrade", "head"], env=env, dalton=True)

        @asynccontextmanager
        async def _connect_iso(existing_conn: Any = None):
            if existing_conn is not None:
                yield existing_conn
                return
            async with await psycopg.AsyncConnection.connect(iso_dsn) as aconn:
                yield aconn

        monkeypatch.setattr(policies_mod, "get_db_connection", _connect_iso)

        uid = f"u-exp-{uuid.uuid4().hex[:8]}"
        job = await policies_mod.insert_reconcile_job(
            uid,
            trigger_action="set_tier",
            target_policy_versions={"core": 1},
            pending_tickers=["NVDA"],
            roster_revision=1,
        )
        jid = str(job["job_id"])

        # Worker A claims job with 1-second lease
        claimed_a = await policies_mod.claim_reconcile_job(uid, jid, lease_seconds=1)
        assert claimed_a is not None
        # Expire lease deterministically in PG and let Worker B take over
        async with await psycopg.AsyncConnection.connect(iso_dsn) as aconn:
            async with aconn.transaction():
                async with aconn.cursor() as cur:
                    await cur.execute(
                        "UPDATE dalton_watchlist_reconcile_jobs SET claim_expires_at = NOW() - INTERVAL '5 seconds' WHERE job_id = %s::uuid",
                        (jid,),
                    )
        claimed_b = await policies_mod.claim_reconcile_job(uid, jid, lease_seconds=60)
        assert claimed_b is not None
        assert claimed_b["claim_token"] != claimed_a["claim_token"]

        side_effects: list[str] = []

        async def _track_side_effect(self: Any, user_id: str, ticker: str, **kwargs: Any) -> dict[str, Any]:
            _ = (self, user_id, kwargs)
            side_effects.append(ticker)
            return {"ticker": ticker, "ok": True, "actions": []}

        monkeypatch.setattr(
            reconciler_mod.TierPolicyReconciler,
            "reconcile_company",
            create_autospec(
                reconciler_mod.TierPolicyReconciler.reconcile_company,
                side_effect=_track_side_effect,
            ),
        )
        monkeypatch.setattr(
            reconciler_mod,
            "sync_user_coverage_to_home",
            create_autospec(reconciler_mod.sync_user_coverage_to_home, return_value=True),
        )

        # Old Worker A resumes with its stale preclaimed job -> MUST abort BEFORE reconcile_company!
        res_a = await reconciler_mod.TierPolicyReconciler().reconcile_job(
            uid, jid, sync_home=True, _preclaimed_job=claimed_a
        )
        assert res_a["reason"] == "lease_lost"
        assert side_effects == []

        # Worker B runs with valid lease -> succeeds
        res_b = await reconciler_mod.TierPolicyReconciler().reconcile_job(
            uid, jid, sync_home=True, _preclaimed_job=claimed_b
        )
        assert res_b["status"] == "completed"
        assert side_effects == ["NVDA"]


async def _wait_for_advisory_lock_waiter(iso_dsn: str, *, timeout: float = 5.0) -> None:
    """Poll ``pg_locks`` until another backend is waiting on an advisory lock."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        async with await psycopg.AsyncConnection.connect(iso_dsn, autocommit=True) as obs:
            async with obs.cursor() as cur:
                await cur.execute(
                    """
                    SELECT count(*)
                    FROM pg_locks
                    WHERE locktype = 'advisory' AND NOT granted
                    """
                )
                row = await cur.fetchone()
                if row and int(row[0]) > 0:
                    return
        await asyncio.sleep(0.02)
    raise AssertionError("Timed out waiting for worker to block on PostgreSQL advisory lock")


@pytest.mark.asyncio
async def test_r6_lease_lost_while_blocked_on_locks_skips_db_and_yaml_writes_real_pg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r6 Item 5 (Real PG): Worker A renews lease, then blocks on automation lock / YAML lock;
    during the wait its lease expires and Worker B takes over. Once unblocked, Worker A MUST NOT
    write automations or Home YAML."""
    from src.dalton.coverage import service as cov_svc
    from src.dalton.watchlist import policies as policies_mod
    from src.dalton.watchlist import reconciler as reconciler_mod
    from src.server.database import automation as auto_db

    with _isolated_pg_database(PG_DSN) as iso_dsn:
        env = _alembic_env_for_dsn(iso_dsn)
        _run_alembic(["upgrade", "head"], env=env, dalton=False)
        _run_alembic(["upgrade", "head"], env=env, dalton=True)

        uid = f"u-stall-{uuid.uuid4().hex[:8]}"
        ws_id = str(uuid.uuid4())
        auto_id = str(uuid.uuid4())

        async with _isolated_pool(iso_dsn, max_size=4) as pool:
            async with pool.connection() as conn:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "INSERT INTO users (user_id, email, name) VALUES (%s, %s, %s)",
                            (uid, f"{uid}@example.com", "Stall User"),
                        )
                        await cur.execute(
                            "INSERT INTO workspaces (workspace_id, user_id, name, status) VALUES (%s::uuid, %s, 'NVDA', 'running')",
                            (ws_id, uid),
                        )
                        await cur.execute(
                            "INSERT INTO dalton_coverage (user_id, ticker, name, tier, market, extra, source) "
                            "VALUES (%s, 'NVDA', 'NVIDIA', 'radar', 'US', '{}'::jsonb, 'ui')",
                            (uid,),
                        )
                        await cur.execute(
                            """
                            INSERT INTO automations (
                                automation_id, user_id, name, trigger_type, cron_expression,
                                timezone, instruction, next_run_at, workspace_id, status, metadata
                            )
                            VALUES (
                                %s::uuid, %s, 'dalton-nvda-watch', 'cron', '0 9 * * 1-5',
                                'America/New_York', 'Watch NVDA', NOW() + INTERVAL '1 day',
                                %s::uuid, 'active', '{"ticker": "NVDA", "dalton_managed": true}'::jsonb
                            )
                            """,
                            (auto_id, uid, ws_id),
                        )

            # Part A: Stalled on automation advisory lock while lease expires and Worker B takes over
            job_a = await policies_mod.insert_reconcile_job(
                uid,
                trigger_action="set_tier",
                target_policy_versions={"radar": 1},
                pending_tickers=["NVDA"],
                roster_revision=1,
            )
            jid_a = str(job_a["job_id"])
            claimed_a = await policies_mod.claim_reconcile_job(uid, jid_a, lease_seconds=60)
            assert claimed_a is not None

            rec = reconciler_mod.TierPolicyReconciler()
            async with await psycopg.AsyncConnection.connect(iso_dsn, autocommit=False) as blocker:
                await auto_db.lock_user_automations(uid, conn=blocker)

                worker_a_task = asyncio.create_task(
                    rec.reconcile_job(uid, jid_a, sync_home=False, _preclaimed_job=claimed_a)
                )
                await _wait_for_advisory_lock_waiter(iso_dsn)

                # Lease expires while Worker A is waiting on the automation lock; Worker B takes over
                async with await psycopg.AsyncConnection.connect(iso_dsn, autocommit=True) as admin:
                    async with admin.cursor() as cur:
                        await cur.execute(
                            "UPDATE dalton_watchlist_reconcile_jobs "
                            "SET claim_expires_at = NOW() - INTERVAL '5 seconds' "
                            "WHERE job_id = %s::uuid",
                            (jid_a,),
                        )
                claimed_b = await policies_mod.claim_reconcile_job(uid, jid_a, lease_seconds=60)
                assert claimed_b is not None
                assert claimed_b["claim_token"] != claimed_a["claim_token"]

                # Release the automation lock -> Worker A wakes up, re-checks lease inside tx, MUST NOT write
                await blocker.rollback()

            res_a = await asyncio.wait_for(worker_a_task, timeout=10.0)
            assert res_a["reason"] == "lease_lost"
            assert res_a["claimed"] is False

            async with pool.connection() as check_conn:
                async with check_conn.cursor() as cur:
                    await cur.execute(
                        "SELECT status, next_run_at FROM automations WHERE automation_id = %s::uuid",
                        (auto_id,),
                    )
                    st_row, next_row = await cur.fetchone()
                    assert st_row == "active"
                    assert next_row is not None

            # Part B: Stalled on Home YAML advisory lock while lease expires and Worker B takes over
            job_y = await policies_mod.insert_reconcile_job(
                uid,
                trigger_action="update_tags",
                target_policy_versions={"radar": 1},
                pending_tickers=[],
                roster_revision=2,
            )
            jid_y = str(job_y["job_id"])
            claimed_ya = await policies_mod.claim_reconcile_job(uid, jid_y, lease_seconds=60)
            assert claimed_ya is not None

            yaml_writes: list[str] = []

            class _SpyBackend:
                async def awrite_text(self, rel_path: str, content: str) -> bool:
                    _ = rel_path
                    yaml_writes.append(content)
                    return True

            monkeypatch.setattr(
                cov_svc,
                "_resolve_home_sandbox_backend",
                create_autospec(cov_svc._resolve_home_sandbox_backend, return_value=_SpyBackend()),
            )

            lock_key = f"dalton_home_yaml_sync:{uid}"
            async with await psycopg.AsyncConnection.connect(iso_dsn, autocommit=True) as y_blocker:
                async with y_blocker.cursor() as cur:
                    await cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (lock_key,))
                try:
                    worker_ya_task = asyncio.create_task(
                        rec.reconcile_job(uid, jid_y, sync_home=True, _preclaimed_job=claimed_ya)
                    )
                    await _wait_for_advisory_lock_waiter(iso_dsn)

                    async with await psycopg.AsyncConnection.connect(iso_dsn, autocommit=True) as admin:
                        async with admin.cursor() as cur:
                            await cur.execute(
                                "UPDATE dalton_watchlist_reconcile_jobs "
                                "SET claim_expires_at = NOW() - INTERVAL '5 seconds' "
                                "WHERE job_id = %s::uuid",
                                (jid_y,),
                            )
                    claimed_yb = await policies_mod.claim_reconcile_job(uid, jid_y, lease_seconds=60)
                    assert claimed_yb is not None
                    assert claimed_yb["claim_token"] != claimed_ya["claim_token"]
                finally:
                    async with y_blocker.cursor() as cur:
                        await cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock_key,))

            res_ya = await asyncio.wait_for(worker_ya_task, timeout=10.0)
            assert res_ya["reason"] == "lease_lost"
            assert yaml_writes == []


@pytest.mark.asyncio
async def test_r6_home_yaml_sync_succeeds_with_pool_max_size_one_real_pg() -> None:
    """r6 Item 6 (Real PG): ``sync_user_coverage_to_home`` reuses its lock connection and
    succeeds cleanly even when the database connection pool has ``max_size=1``."""
    from src.dalton.coverage import service as cov_svc
    from src.dalton.watchlist import policies as policies_mod

    with _isolated_pg_database(PG_DSN) as iso_dsn:
        env = _alembic_env_for_dsn(iso_dsn)
        _run_alembic(["upgrade", "head"], env=env, dalton=False)
        _run_alembic(["upgrade", "head"], env=env, dalton=True)

        uid = f"u-pool1-{uuid.uuid4().hex[:8]}"
        written: list[tuple[str, str]] = []

        class _CaptureBackend:
            async def awrite_text(self, rel_path: str, content: str) -> bool:
                written.append((rel_path, content))
                return True

        async with _isolated_pool(iso_dsn, max_size=1) as pool:
            async with pool.connection() as conn:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "INSERT INTO dalton_coverage (user_id, ticker, name, tier, market, extra, source) "
                            "VALUES (%s, 'NVDA', 'NVIDIA', 'core', 'US', '{}'::jsonb, 'ui')",
                            (uid,),
                        )
                    rev = await policies_mod.bump_user_roster_revision(uid, conn=conn)
                    assert rev == 1

            ok = await cov_svc.sync_user_coverage_to_home(
                uid,
                backend=_CaptureBackend(),
                roster_revision=1,
            )
            assert ok is True
            assert len(written) == 1
            assert written[0][0] == "dalton/coverage.yaml"
            assert "NVDA" in written[0][1]

            state_after = await policies_mod.get_user_roster_state(uid)
            assert state_after["last_yaml_synced_revision"] == 1


@pytest.mark.asyncio
async def test_r7_home_yaml_publish_pool_max_size_one_without_injected_backend_real_pg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r7 Item 1 (Real PG): With ``max_size=1`` and NO injected ``backend``,
    ``sync_user_coverage_to_home`` resolves Home workspace (``get_or_create_home_workspace``)
    and ``workspace_is_moving`` BEFORE taking the YAML advisory lock, so it succeeds without
    PoolTimeout."""
    from types import SimpleNamespace
    from src.dalton.coverage import service as cov_svc
    from src.dalton.watchlist import policies as policies_mod
    from src.server.database import workspace as ws_db_mod
    from src.server.services import workspace_manager as wm_mod

    with _isolated_pg_database(PG_DSN) as iso_dsn:
        env = _alembic_env_for_dsn(iso_dsn)
        _run_alembic(["upgrade", "head"], env=env, dalton=False)
        _run_alembic(["upgrade", "head"], env=env, dalton=True)

        uid = f"u-pool1-r7-{uuid.uuid4().hex[:8]}"
        written: list[tuple[str, str]] = []

        class _FakeSandbox:
            working_dir = "/workspace"
            _work_dir = "/workspace"
            config = SimpleNamespace(
                filesystem=SimpleNamespace(
                    working_directory="/workspace",
                    allowed_directories=["/workspace"],
                )
            )

            async def awrite_file_text(self, path: str, content: str) -> bool:
                written.append((path, content))
                return True

        class _RealMovingCheckManager:
            async def get_session_for_workspace(
                self, workspace_id: str, *, user_id: str | None = None
            ) -> Any:
                _ = user_id
                # Exercises the real DB query in workspace_is_moving against the max_size=1 pool
                assert await ws_db_mod.workspace_is_moving(workspace_id) is False
                return SimpleNamespace(sandbox=_FakeSandbox())

        monkeypatch.setattr(
            wm_mod.WorkspaceManager,
            "get_instance",
            classmethod(lambda cls: _RealMovingCheckManager()),
        )

        async with _isolated_pool(iso_dsn, max_size=1) as pool:
            async with pool.connection() as conn:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "INSERT INTO users (user_id, email, name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                            (uid, f"{uid}@example.com", "Pool1 User"),
                        )
                        await cur.execute(
                            "INSERT INTO dalton_coverage (user_id, ticker, name, tier, market, extra, source) "
                            "VALUES (%s, 'NVDA', 'NVIDIA', 'core', 'US', '{}'::jsonb, 'ui')",
                            (uid,),
                        )
                    rev = await policies_mod.bump_user_roster_revision(uid, conn=conn)
                    assert rev == 1

            ok = await cov_svc.sync_user_coverage_to_home(uid, roster_revision=1)
            assert ok is True
            assert len(written) == 1
            assert written[0][0].endswith("dalton/coverage.yaml")
            assert "NVDA" in written[0][1]

            state_after = await policies_mod.get_user_roster_state(uid)
            assert state_after["last_yaml_synced_revision"] == 1


@pytest.mark.asyncio
async def test_r7_reconciler_fallback_query_savepoint_commits_pause_real_pg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r7 Item 3 (Real PG): When both the primary execution query and the fallback
    ``list_executions(..., conn=sql_conn)`` raise real PostgreSQL SQL errors on the held
    transaction connection, savepoints isolate both failures so the outer pause transaction
    still commits cleanly."""
    from src.dalton.watchlist import reconciler as reconciler_mod
    from src.server.database import automation_executions as exec_db_mod

    with _isolated_pg_database(PG_DSN) as iso_dsn:
        env = _alembic_env_for_dsn(iso_dsn)
        _run_alembic(["upgrade", "head"], env=env, dalton=False)
        _run_alembic(["upgrade", "head"], env=env, dalton=True)

        uid = f"u-sp-r7-{uuid.uuid4().hex[:8]}"
        ws_id = str(uuid.uuid4())
        auto_id = str(uuid.uuid4())

        async with _isolated_pool(iso_dsn, max_size=2) as pool:
            async with pool.connection() as conn:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "INSERT INTO users (user_id, email, name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                            (uid, f"{uid}@example.com", "Savepoint User"),
                        )
                        await cur.execute(
                            "INSERT INTO workspaces (workspace_id, user_id, name, status) "
                            "VALUES (%s::uuid, %s, 'NVDA', 'running')",
                            (ws_id, uid),
                        )
                        await cur.execute(
                            """
                            INSERT INTO automations (
                                automation_id, user_id, name, trigger_type,
                                timezone, instruction, next_run_at, last_run_at,
                                workspace_id, status, metadata
                            )
                            VALUES (
                                %s::uuid, %s, 'dalton-nvda-earn-fy26q4-t0', 'once',
                                'America/New_York', 'Earnings flash',
                                NOW() + INTERVAL '2 days', NOW(),
                                %s::uuid, 'active', '{"ticker": "NVDA", "dalton_managed": true}'::jsonb
                            )
                            """,
                            (auto_id, uid, ws_id),
                        )

            # Shadow public.automation_executions via pg_temp ONLY while is_unstarted_earn_once
            # runs, so BOTH its primary SQL query AND the real fallback
            # exec_db.list_executions(..., conn=sql_conn) hit real PostgreSQL UndefinedColumn
            # errors inside their savepoints, while list_all_user_automations (before) and
            # update_automation / skip_unstarted_executions_for_automations (after) still see
            # the real public.automation_executions table.
            orig_is_unstarted = reconciler_mod.is_unstarted_earn_once
            orig_list_exec = exec_db_mod.list_executions
            fallback_errors: list[type[BaseException]] = []

            async def _spy_list_executions(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await orig_list_exec(*args, **kwargs)
                except Exception as exc:
                    fallback_errors.append(type(exc))
                    raise

            fallback_spy = create_autospec(orig_list_exec, side_effect=_spy_list_executions)

            async def _shadow_executions_during_check(
                row: Any, *, user_id: str | None = None, conn: Any = None
            ) -> bool:
                assert conn is not None
                async with conn.cursor() as cur:
                    await cur.execute(
                        "CREATE TEMP VIEW automation_executions AS SELECT 1 AS broken_col"
                    )
                try:
                    return await orig_is_unstarted(row, user_id=user_id, conn=conn)
                finally:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "DROP VIEW IF EXISTS pg_temp.automation_executions"
                        )

            monkeypatch.setattr(
                reconciler_mod,
                "is_unstarted_earn_once",
                _shadow_executions_during_check,
            )
            monkeypatch.setattr(
                reconciler_mod.exec_db,
                "list_executions",
                fallback_spy,
            )

            res = await reconciler_mod.TierPolicyReconciler().reconcile_company(
                uid, "NVDA", removed_workspace_id=ws_id
            )
            assert res["ok"] is True
            assert res.get("skipped") is not True
            assert fallback_spy.await_count == 1
            assert fallback_spy.await_args.kwargs.get("conn") is not None
            assert fallback_errors == [psycopg.errors.UndefinedColumn]

            # Verify from a fresh connection that the pause ACTUALLY COMMITTED to Postgres
            async with pool.connection() as verify_conn:
                async with verify_conn.cursor() as cur:
                    await cur.execute(
                        "SELECT status, next_run_at FROM automations WHERE automation_id = %s::uuid",
                        (auto_id,),
                    )
                    row = await cur.fetchone()
                    assert row is not None
                    assert row[0] == "paused"
                    assert row[1] is None
