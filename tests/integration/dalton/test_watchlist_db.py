"""Real PostgreSQL migration integration tests for Dalton d056 (personal watchlist).

Covers:
1. Empty-database full upgrade to head (verifies single linear head, new tables,
   columns, indexes, and 3-tier CHECK constraints).
2. Existing-data upgrade (`d055` -> `d056`) and safe `downgrade` (`d056` -> `d055`)
   -> re-`upgrade` (`d055` -> `d056`) with real samples: unlisted `SPCX`,
   manual workspace binding (`_manual_workspace_id`), rule groups, and `radar`
   rows/groups.

Skipped automatically when ``DALTON_TEST_PG_DSN`` is not set (run outside sandbox by Lead).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PG_DSN = os.getenv("DALTON_TEST_PG_DSN", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_DSN,
        reason="DALTON_TEST_PG_DSN is not set; skipping real PostgreSQL d056 watchlist migration test",
    ),
]


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
    from urllib.parse import urlparse

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


def test_d056_single_head_and_fresh_upgrade_schema() -> None:
    """Verify Dalton migration chain has a single head `d056` and all watchlist tables/constraints exist."""
    env = _alembic_env_for_dsn(PG_DSN)

    heads_out = _run_alembic(["heads"], env=env, dalton=True)
    head_lines = [ln.strip() for ln in heads_out.splitlines() if ln.strip()]
    assert len(head_lines) == 1, f"Expected single Dalton head, got: {head_lines}"
    assert head_lines[0].startswith("d056"), f"Expected head d056, got: {head_lines}"

    _run_alembic(["upgrade", "head"], env=env, dalton=False)
    _run_alembic(["upgrade", "head"], env=env, dalton=True)

    with psycopg.connect(PG_DSN, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN (
                      'dalton_watchlist_tier_policies',
                      'dalton_watchlist_reconcile_jobs',
                      'dalton_watchlist_views',
                      'dalton_watchlist_custom_columns'
                  )
                """
            )
            tables = {r[0] for r in cur.fetchall()}
            assert tables == {
                "dalton_watchlist_tier_policies",
                "dalton_watchlist_reconcile_jobs",
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
    """Verify d055 -> d056 backfill (SPCX, manual ws, rule groups, legacy tags) and radar-safe downgrade."""
    env = _alembic_env_for_dsn(PG_DSN)
    _run_alembic(["upgrade", "head"], env=env, dalton=False)
    _run_alembic(["upgrade", "d055"], env=env, dalton=True)

    test_uid = f"d056-sample-{uuid.uuid4().hex[:8]}"
    group_id = str(uuid.uuid4())
    radar_group_id = str(uuid.uuid4())
    manual_ws_id = str(uuid.uuid4())

    # Seed pre-d056 rows at d055: SPCX (core), AAPL (core + _manual_workspace_id + extra.tags), MSFT (watch + group)
    with psycopg.connect(PG_DSN, autocommit=False) as conn:
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
                VALUES (%s, 'SPCX', 'SpaceX', 'core', 'US', '{}'::jsonb, 'seed')
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

    try:
        # Upgrade d055 -> d056
        _run_alembic(["upgrade", "d056"], env=env, dalton=True)

        with psycopg.connect(PG_DSN, autocommit=False) as conn:
            with conn.cursor() as cur:
                # Verify SPCX backfilled is_private=TRUE and core tier preserved
                cur.execute(
                    "SELECT tier, is_private FROM dalton_coverage WHERE user_id = %s AND ticker = 'SPCX'",
                    (test_uid,),
                )
                assert cur.fetchone() == ("core", True)

                # Verify AAPL preserved _manual_workspace_id, note, core tier, and backfilled tags
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

                # Verify MSFT preserved watch tier and group_id
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

                # Insert an outbox job and tier policy row to ensure downgrade drops tables cleanly
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
                        job_id, user_id, trigger_action, pending_tickers
                    )
                    VALUES (%s::uuid, %s, 'add', '["NET"]'::jsonb)
                    """,
                    (str(uuid.uuid4()), test_uid),
                )
            conn.commit()

        # Downgrade d056 -> d055 with radar rows present
        _run_alembic(["downgrade", "d055"], env=env, dalton=True)

        with psycopg.connect(PG_DSN, autocommit=False) as conn:
            with conn.cursor() as cur:
                # NET converted to 'watch' with downgraded_from_tier='radar' preserved in extra
                cur.execute(
                    "SELECT tier, extra FROM dalton_coverage WHERE user_id = %s AND ticker = 'NET'",
                    (test_uid,),
                )
                net_tier, net_extra = cur.fetchone()
                assert net_tier == "watch"
                assert net_extra.get("downgraded_from_tier") == "radar"
                assert net_extra.get("downgraded_silent") is True

                # Radar rule group converted to 'watch'
                cur.execute(
                    "SELECT default_tier FROM dalton_coverage_groups WHERE group_id = %s::uuid",
                    (radar_group_id,),
                )
                assert cur.fetchone() == ("watch",)

                # Existing core/watch samples remained intact
                cur.execute(
                    "SELECT ticker, tier FROM dalton_coverage WHERE user_id = %s ORDER BY ticker",
                    (test_uid,),
                )
                assert cur.fetchall() == [
                    ("AAPL", "core"),
                    ("MSFT", "watch"),
                    ("NET", "watch"),
                    ("SPCX", "core"),
                ]

                # 2-tier CHECK constraint rejects 'radar' again after downgrade
                with pytest.raises(psycopg.errors.CheckViolation):
                    cur.execute(
                        "INSERT INTO dalton_coverage (user_id, ticker, tier) VALUES (%s, 'CRWD', 'radar')",
                        (test_uid,),
                    )
                conn.rollback()

        # Re-upgrade d055 -> d056 cleanly
        _run_alembic(["upgrade", "d056"], env=env, dalton=True)

        with psycopg.connect(PG_DSN, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT is_private FROM dalton_coverage WHERE user_id = %s AND ticker = 'SPCX'",
                    (test_uid,),
                )
                assert cur.fetchone() == (True,)
    finally:
        try:
            _run_alembic(["upgrade", "head"], env=env, dalton=True)
        except AssertionError:
            pass
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM dalton_coverage WHERE user_id = %s", (test_uid,))
                cur.execute("DELETE FROM dalton_coverage_groups WHERE user_id = %s", (test_uid,))
                for tbl in (
                    "dalton_watchlist_tier_policies",
                    "dalton_watchlist_reconcile_jobs",
                    "dalton_watchlist_views",
                    "dalton_watchlist_custom_columns",
                ):
                    cur.execute(f"DELETE FROM {tbl} WHERE user_id = %s", (test_uid,))
