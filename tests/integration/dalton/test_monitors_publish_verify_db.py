"""Real PostgreSQL + dual-worker fault-injection integration tests for Batch 2 (§1):
- Pre-promotion verification (entry/JS/CSS + candidate public probe) & kill right before promote;
- Dual-worker takeover right before promote (in-tx claim_epoch fence);
- Public share revocation right before promote (in-tx share_links.revision / acl_revision fence);
- Cross-run supersede right before promote (in-tx superseded fence + frozen_dir isolation);
- Multi-panel all_or_nothing single-transaction atomic rollback on second-panel failure;
- Dual-axis realtime monitor data health + same as_of backfill with larger attempt_seq;
- Real Alembic d058 <-> d061 upgrade/downgrade round-trip.

Skipped automatically when ``DALTON_TEST_PG_DSN`` is unset (run by Lead against a ``*_test`` DB).
Migration note: ``d061_monitor_publish_verify.py`` temporarily sets ``down_revision = "d058"``
while D4 (PR #521, ``d059``–``d060``) is pending; Lead will re-parent ``d061`` after ``d060`` on merge.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import psycopg
from psycopg.rows import dict_row
import pytest

from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.data_jobs.container_runner import build_path_map
from src.dalton.data_jobs.freeze import freeze_monitor_publish_artifacts
from src.dalton.data_jobs.scheduler import (
    claim_monitor_publish_run_tx,
    process_monitor_publish_for_run,
    replay_pending_monitor_publishes,
    settle_monitor_publish_run_tx,
)
from src.dalton.monitors import (
    data_job_registry,
    db as monitor_db,
    grants as monitor_grants,
    verify as monitor_verify,
)
from src.dalton.monitors.data_job_registry import MonitorDataJobBinding
from src.dalton.monitors.router import router as monitors_router
from src.server.database import artifacts as artifacts_db
from src.server.database.artifacts import VersionFile
from src.server.database.artifacts import records as artifacts_records_db
from src.server.database.artifacts.versions import LeftOutChanged, SharingChanged
from src.server.database import pool as db_pool_mod
from tests.integration.dalton._db_guard import extract_effective_test_db_name

REPO_ROOT = Path(__file__).resolve().parents[3]
PG_DSN = os.getenv("DALTON_TEST_PG_DSN", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_DSN,
        reason="DALTON_TEST_PG_DSN is not set; skipping real PostgreSQL Batch 2 monitor publish tests",
    ),
]


@pytest.fixture(scope="session", autouse=True)
def dalton_schema():
    """Override directory conftest autouse fixture; managed explicitly via ``migrated_monitor_dsn``."""
    yield


@pytest.fixture(autouse=True)
def cleanup_dalton_tables():
    """Override directory conftest autouse fixture so no DB connection runs when DALTON_TEST_PG_DSN is unset."""
    yield


def _to_sa_url(dsn: str) -> str:
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql+psycopg://", 1)
    return dsn


def _dalton_alembic_cfg(dsn: str) -> AlembicConfig:
    sa_url = _to_sa_url(dsn)
    dalton_cfg = AlembicConfig(str(REPO_ROOT / "src" / "dalton" / "alembic.ini"))
    dalton_cfg.set_main_option(
        "script_location", str(REPO_ROOT / "src" / "dalton" / "migrations")
    )
    dalton_cfg.set_main_option("sqlalchemy.url", sa_url.replace("%", "%%"))
    return dalton_cfg


@pytest.fixture(scope="session")
def migrated_monitor_dsn() -> str:
    dsn = os.getenv("DALTON_TEST_PG_DSN", "").strip()
    if not dsn:
        pytest.skip("DALTON_TEST_PG_DSN is not set")
    extract_effective_test_db_name(dsn, source="DALTON_TEST_PG_DSN")

    def _run_migrations() -> None:
        sa_url = _to_sa_url(dsn)
        upstream_cfg = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
        upstream_cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        upstream_cfg.set_main_option("sqlalchemy.url", sa_url.replace("%", "%%"))
        alembic_command.upgrade(upstream_cfg, "head")
        alembic_command.upgrade(_dalton_alembic_cfg(dsn), "head")

    # Run in a clean thread so upstream 001_initial_schema's asyncio.run() never collides
    # with an active pytest-asyncio event loop in the same session.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_run_migrations).result()
    return dsn


@pytest.fixture
def bind_db_pool(migrated_monitor_dsn: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """Bind ``get_db_connection`` across all loaded ``src.*`` modules to ``DALTON_TEST_PG_DSN``."""
    # Ensure core DB modules that import get_db_connection at module scope are loaded before sweeping.
    import src.dalton.access_tiers  # noqa: F401
    import src.dalton.auth.roles  # noqa: F401
    import src.dalton.company_identity  # noqa: F401
    import src.server.database.artifacts.records  # noqa: F401
    import src.server.database.artifacts.versions  # noqa: F401
    import src.server.database.server_keys  # noqa: F401
    import src.server.database.workspace  # noqa: F401
    import src.server.database.workspace_file_blobs  # noqa: F401

    @asynccontextmanager
    async def _real_get_db_connection(conn: Any = None):
        if conn is not None:
            yield conn
            return
        async with await psycopg.AsyncConnection.connect(
            migrated_monitor_dsn, autocommit=True
        ) as c:
            yield c

    orig_gdc = db_pool_mod.get_db_connection
    for name, module in list(sys.modules.items()):
        if name.startswith(("src.", "ptc_agent.")) and (
            getattr(module, "get_db_connection", None) is orig_gdc
        ):
            monkeypatch.setattr(module, "get_db_connection", _real_get_db_connection)
    monkeypatch.setattr(db_pool_mod, "get_db_connection", _real_get_db_connection)
    monkeypatch.setenv("DALTON_CAP_DATA_JOBS", "1")
    return migrated_monitor_dsn


def _vf(path: str, content: str, content_type: str = "text/html; charset=utf-8") -> VersionFile:
    raw = content.encode("utf-8")
    return VersionFile(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_len=len(raw),
        content_type=content_type,
        content=raw,
    )


async def _ensure_test_data_task_def(conn: Any, job_key: str) -> None:
    """Insert an isolated test ``dalton_data_task_defs`` row for a unique ``job_key``."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO dalton_data_task_defs (
                job_key, task_code, title, batch_no, runner, cron_expr, timezone, command_argv
            )
            VALUES (
                %s, %s, %s, 1, 'container', '0 0 * * *', 'UTC', '["true"]'::jsonb
            )
            ON CONFLICT (job_key) DO NOTHING
            """,
            (job_key, job_key, f"Integration Test {job_key}"),
        )


async def _cleanup_test_rows(
    dsn: str,
    *,
    run_ids: Sequence[str] = (),
    job_keys: Sequence[str] = (),
    monitor_ids: Sequence[str] = (),
    artifact_ids: Sequence[str] = (),
    user_ids: Sequence[str] = (),
) -> None:
    """Clean up DB rows created by a test so sibling tests never collide on slugs or job_keys."""
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as clean_conn:
        if run_ids:
            await clean_conn.execute(
                "DELETE FROM dalton_data_task_runs WHERE run_id = ANY(%s::uuid[])",
                (list(run_ids),),
            )
        if job_keys:
            await clean_conn.execute(
                "DELETE FROM dalton_data_task_runs WHERE job_key = ANY(%s::text[])",
                (list(job_keys),),
            )
            await clean_conn.execute(
                "DELETE FROM dalton_data_task_defs WHERE job_key = ANY(%s::text[])",
                (list(job_keys),),
            )
        if monitor_ids:
            await clean_conn.execute(
                "DELETE FROM dalton_monitors WHERE monitor_id = ANY(%s::uuid[])",
                (list(monitor_ids),),
            )
        if artifact_ids:
            await clean_conn.execute(
                "DELETE FROM share_links WHERE artifact_id = ANY(%s::uuid[])",
                (list(artifact_ids),),
            )
        if user_ids:
            await clean_conn.execute(
                "DELETE FROM dalton_user_roles WHERE user_id = ANY(%s::text[])",
                (list(user_ids),),
            )
            await clean_conn.execute(
                "DELETE FROM users WHERE user_id = ANY(%s::text[])",
                (list(user_ids),),
            )


@pytest.mark.asyncio
async def test_db_fault_injection_kill_before_promote_keeps_public_v1_and_readback_failure_never_switches(
    bind_db_pool: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fault injection 1: worker killed right before Stage 3 promote (and pre-promote readback failure)
    keeps anonymous public detail & file routes serving v1 with zero partial switch."""
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    monkeypatch.setattr(monitor_grants._MONITOR_CODEC, "_key", None)
    monkeypatch.setattr(monitor_verify._CANDIDATE_PROBE_CODEC, "_key", None)

    ws_id = await monitor_db.ensure_company_monitor_workspace()
    slug = f"kill-before-promote-{uuid4().hex[:8]}"
    job_key = slug
    entry_path = f"{slug}/index.html"
    js_path = f"{slug}/app.js"
    artifact = await artifacts_db.ensure_artifact(ws_id, entry_path)
    mon_id: str | None = None

    v1_html = '<!doctype html><html><body>Stable Version 1</body></html>'
    v2_html = '<!doctype html><html><head><script src="app.js"></script></head><body>Candidate V2</body></html>'
    v1_file = _vf(entry_path, v1_html)
    v2_file = _vf(entry_path, v2_html)
    v2_js = _vf(js_path, "console.log('v2');", "application/javascript")

    try:
        # Promote v1 publicly
        st1 = await monitor_db.stage_monitor_candidate_version_tx(
            artifact_id=artifact.artifact_id,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            files=[v1_file],
            title="Kill Fault Injection",
            source_kind="data_job",
            visibility="public",
        )
        pr1 = await monitor_db.promote_verified_monitor_version_tx(
            artifact_id=artifact.artifact_id,
            candidate_seq=st1.candidate_seq,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            paths=[entry_path],
            slug=slug,
            monitor_owner_id=COMPANY_SYSTEM_USER_ID,
            title="Kill Fault Injection",
            category="overview",
            sort_order=0,
            visibility="public",
            keep_updated=True,
            source_kind="data_job",
            source_ref=job_key,
            member_user_ids=(),
        )
        assert pr1.monitor is not None
        mon_id = pr1.monitor.monitor_id
        assert pr1.monitor.published_seq == 1
        assert pr1.monitor.last_verified_seq == 1

        # Case A: Stage v2 WITHOUT its referenced app.js -> Stage 2 MUST reject BEFORE promote!
        st2_broken = await monitor_db.stage_monitor_candidate_version_tx(
            artifact_id=artifact.artifact_id,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            files=[v2_file],
            title="Kill Fault Injection",
            monitor_id=pr1.monitor.monitor_id,
            source_kind="data_job",
            visibility="public",
            is_update_flow=True,
        )
        with pytest.raises(monitor_verify.MonitorVerificationError, match="app.js"):
            await monitor_verify.verify_candidate_monitor_version(
                artifact_id=artifact.artifact_id,
                candidate_seq=st2_broken.candidate_seq,
                entry_path=entry_path,
                snap_files=[v2_file],
                snap_entry_sha256=v2_file.sha256,
                visibility="public",
                slug=slug,
            )

        # Case B: Stage complete v3 (HTML + app.js), pass full Stage 2 pre-promote readback,
        # then simulate hard kill right before promote_verified_monitor_version_tx!
        st3 = await monitor_db.stage_monitor_candidate_version_tx(
            artifact_id=artifact.artifact_id,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            files=[v2_file, v2_js],
            title="Kill Fault Injection",
            monitor_id=pr1.monitor.monitor_id,
            source_kind="data_job",
            visibility="public",
            is_update_flow=True,
        )
        verified3 = await monitor_verify.verify_candidate_monitor_version(
            artifact_id=artifact.artifact_id,
            candidate_seq=st3.candidate_seq,
            entry_path=entry_path,
            snap_files=[v2_file, v2_js],
            snap_entry_sha256=v2_file.sha256,
            visibility="public",
            slug=slug,
        )
        assert verified3.public_probed is True
        # Public visitors load /v/{seq}/, so <script src="app.js"> resolves to visitor path "app.js".
        assert "app.js" in verified3.probed_assets

        # Worker dies before calling promote -> real anonymous public routes STILL serve v1, v3 is 404!
        app = FastAPI()
        app.include_router(monitors_router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            detail_resp = await client.get(f"/api/v1/dalton/monitors/public/{slug}")
            assert detail_resp.status_code == 200
            assert detail_resp.headers["cache-control"] == "no-cache"
            assert detail_resp.json()["monitor"]["last_verified_seq"] == 1

            v1_root_resp = await client.get(f"/api/v1/dalton/monitors/public/{slug}/v/1/")
            assert v1_root_resp.status_code == 200
            assert "Stable Version 1" in v1_root_resp.text

            v1_resp = await client.get(f"/api/v1/dalton/monitors/public/{slug}/v/1/{entry_path}")
            assert v1_resp.status_code == 200
            assert "Stable Version 1" in v1_resp.text

            v3_anon = await client.get(
                f"/api/v1/dalton/monitors/public/{slug}/v/{st3.candidate_seq}/"
            )
            assert v3_anon.status_code == 404
    finally:
        await _cleanup_test_rows(
            bind_db_pool,
            monitor_ids=[mon_id] if mon_id else (),
            artifact_ids=[artifact.artifact_id],
        )


@pytest.mark.asyncio
async def test_db_fault_injection_takeover_before_promote_fences_stale_worker_from_switching_monitor(
    bind_db_pool: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fault injection 2: Worker 1 pauses right before promote; lease expires and Worker 2 takes over
    (epoch=2). Stale Worker 1 attempting promote_verified_monitor_version_tx(expected_claim_epoch=1)
    is fenced out INSIDE the promote tx with zero visitor pointer changes."""
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    ws_id = await monitor_db.ensure_company_monitor_workspace()
    slug = f"takeover-fence-{uuid4().hex[:8]}"
    job_key = slug
    entry_path = f"{slug}/index.html"
    artifact = await artifacts_db.ensure_artifact(ws_id, entry_path)
    run_id = str(uuid4())
    slot_utc = datetime.now(UTC)

    # Seed v1 monitor
    st1 = await monitor_db.stage_monitor_candidate_version_tx(
        artifact_id=artifact.artifact_id,
        workspace_owner_id=COMPANY_SYSTEM_USER_ID,
        files=[_vf(entry_path, "<html>v1</html>")],
        title="Takeover Fence Test",
        source_kind="data_job",
        visibility="company",
    )
    pr1 = await monitor_db.promote_verified_monitor_version_tx(
        artifact_id=artifact.artifact_id,
        candidate_seq=st1.candidate_seq,
        workspace_owner_id=COMPANY_SYSTEM_USER_ID,
        paths=[entry_path],
        slug=slug,
        monitor_owner_id=COMPANY_SYSTEM_USER_ID,
        title="Takeover Fence Test",
        category="cn",
        sort_order=0,
        visibility="company",
        keep_updated=True,
        source_kind="data_job",
        source_ref=job_key,
        member_user_ids=(),
    )
    assert pr1.monitor is not None
    mon_id = pr1.monitor.monitor_id

    async with await psycopg.AsyncConnection.connect(bind_db_pool, autocommit=True) as conn_setup:
        async with conn_setup.transaction():
            await _ensure_test_data_task_def(conn_setup, job_key)
            async with conn_setup.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO dalton_data_task_runs (
                        run_id, job_key, trigger_source, scheduled_slot_utc, deadline_utc,
                        status, completed_at, monitor_publish_status, monitor_publish_next_retry_at,
                        monitor_publish_detail
                    )
                    VALUES (
                        %s::uuid, %s, 'manual', %s, %s,
                        'succeeded', NOW(), 'pending', NOW(),
                        %s::jsonb
                    )
                    """,
                    (
                        run_id,
                        job_key,
                        slot_utc,
                        slot_utc + timedelta(minutes=30),
                        json.dumps({"panels": {slug: {"status": "pending"}}}),
                    ),
                )

    conn_w1 = await psycopg.AsyncConnection.connect(bind_db_pool, autocommit=True)
    conn_w2 = await psycopg.AsyncConnection.connect(bind_db_pool, autocommit=True)
    try:
        res_w1, res_w2 = await asyncio.gather(
            claim_monitor_publish_run_tx(conn_w1, run_id=run_id, claimed_by="worker-1"),
            claim_monitor_publish_run_tx(conn_w2, run_id=run_id, claimed_by="worker-2"),
        )
        winners = [r for r in (res_w1, res_w2) if r]
        assert len(winners) == 1
        assert winners[0][0].claim_epoch == 1

        # Worker 1 stages candidate v2 and then stalls right before promote!
        st2 = await monitor_db.stage_monitor_candidate_version_tx(
            artifact_id=artifact.artifact_id,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            files=[_vf(entry_path, "<html>v2-from-w1</html>")],
            title="Takeover Fence Test",
            monitor_id=pr1.monitor.monitor_id,
            source_kind="data_job",
            visibility="company",
            run_id=run_id,
            is_update_flow=True,
        )

        # Lease expires -> Worker 2 takes over with claim_epoch=2!
        async with conn_w2.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_data_task_runs
                   SET monitor_publish_lease_expires_at = NOW() - INTERVAL '5 seconds'
                 WHERE run_id = %s::uuid
                """,
                (run_id,),
            )
        takeover = await claim_monitor_publish_run_tx(
            conn_w2, run_id=run_id, claimed_by="worker-2"
        )
        assert len(takeover) == 1 and takeover[0].claim_epoch == 2

        # Stale Worker 1 wakes up and tries to promote with epoch=1 -> FENCED OUT inside promote tx!
        stale_promote = await monitor_db.promote_verified_monitor_version_tx(
            artifact_id=artifact.artifact_id,
            candidate_seq=st2.candidate_seq,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            paths=[entry_path],
            monitor_id=pr1.monitor.monitor_id,
            run_id=run_id,
            source_kind="data_job",
            is_update_flow=True,
            expected_claim_epoch=1,
            check_superseded=True,
        )
        assert stale_promote.fenced_out is True
        assert stale_promote.promoted is False

        # Monitor is STILL on v1 (stale worker 1 could NOT switch it!)
        live_after_stale = await monitor_db.get_monitor_by_id(pr1.monitor.monitor_id)
        assert live_after_stale is not None
        assert live_after_stale.published_seq == 1
        assert live_after_stale.last_verified_seq == 1

        # Worker 2 promotes cleanly with epoch=2 and settles!
        w2_promote = await monitor_db.promote_verified_monitor_version_tx(
            artifact_id=artifact.artifact_id,
            candidate_seq=st2.candidate_seq,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            paths=[entry_path],
            monitor_id=pr1.monitor.monitor_id,
            run_id=run_id,
            source_kind="data_job",
            is_update_flow=True,
            expected_claim_epoch=2,
            check_superseded=True,
        )
        assert w2_promote.promoted is True
        assert w2_promote.monitor is not None
        assert w2_promote.monitor.published_seq == st2.candidate_seq

        assert await settle_monitor_publish_run_tx(
            conn_w1, run_id=run_id, claim_epoch=1, next_status="failed", detail={"stale": True}
        ) is False
        assert await settle_monitor_publish_run_tx(
            conn_w2, run_id=run_id, claim_epoch=2, next_status="published", detail={"ok": True}
        ) is True
    finally:
        await conn_w1.close()
        await conn_w2.close()
        await _cleanup_test_rows(
            bind_db_pool,
            run_ids=[run_id],
            job_keys=[job_key],
            monitor_ids=[mon_id],
            artifact_ids=[artifact.artifact_id],
        )


@pytest.mark.asyncio
async def test_db_fault_injection_revoke_public_before_promote_aborts_stale_publish(
    bind_db_pool: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fault injection 3: admin revokes public sharing right before Stage 3 promote.
    Stale publish with stale share_revision/acl_revision MUST raise SharingChanged and never re-expose public."""
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    ws_id = await monitor_db.ensure_company_monitor_workspace()
    slug = f"revoke-fence-{uuid4().hex[:8]}"
    job_key = slug
    entry_path = f"{slug}/index.html"
    artifact = await artifacts_db.ensure_artifact(ws_id, entry_path)
    mon_id: str | None = None

    try:
        # Publish v1 as public
        st1 = await monitor_db.stage_monitor_candidate_version_tx(
            artifact_id=artifact.artifact_id,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            files=[_vf(entry_path, "<html>Public V1</html>")],
            title="Revoke Fence Test",
            source_kind="data_job",
            visibility="public",
        )
        pr1 = await monitor_db.promote_verified_monitor_version_tx(
            artifact_id=artifact.artifact_id,
            candidate_seq=st1.candidate_seq,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            paths=[entry_path],
            slug=slug,
            monitor_owner_id=COMPANY_SYSTEM_USER_ID,
            title="Revoke Fence Test",
            category="overview",
            sort_order=0,
            visibility="public",
            keep_updated=True,
            source_kind="data_job",
            source_ref=job_key,
            member_user_ids=(),
        )
        assert pr1.monitor is not None
        mon_id = pr1.monitor.monitor_id
        assert pr1.monitor.visibility == "public"

        # Stage v2 capturing pre-revoke share_revision and acl_revision
        st2 = await monitor_db.stage_monitor_candidate_version_tx(
            artifact_id=artifact.artifact_id,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            files=[_vf(entry_path, "<html>Public V2</html>")],
            title="Revoke Fence Test",
            monitor_id=pr1.monitor.monitor_id,
            source_kind="data_job",
            visibility="public",
            is_update_flow=False,
        )
        assert st2.share_revision is not None
        assert st2.monitor_acl_revision is not None

        # Concurrent revocation right before Stage 3 promote!
        await artifacts_records_db.unshare_artifact_and_downgrade_public_monitor(
            user_id=COMPANY_SYSTEM_USER_ID,
            artifact_id=artifact.artifact_id,
        )

        # Late promote trying to re-apply visibility='public' MUST be rejected with SharingChanged!
        with pytest.raises(SharingChanged):
            await monitor_db.promote_verified_monitor_version_tx(
                artifact_id=artifact.artifact_id,
                candidate_seq=st2.candidate_seq,
                workspace_owner_id=COMPANY_SYSTEM_USER_ID,
                paths=[entry_path],
                monitor_id=pr1.monitor.monitor_id,
                visibility="public",
                title="Revoke Fence Test",
                category="overview",
                sort_order=0,
                keep_updated=True,
                source_kind="data_job",
                update_metadata=True,
                expected_share_revision=st2.share_revision,
                expected_share_audience=st2.share_audience,
                expected_acl_revision=st2.monitor_acl_revision,
            )

        # Verify monitor stays downgraded ('own'), still on v1, and public route returns 404!
        after = await monitor_db.get_monitor_by_id(pr1.monitor.monitor_id)
        assert after is not None
        assert after.visibility == "own"
        assert after.published_seq == 1

        app = FastAPI()
        app.include_router(monitors_router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            resp = await client.get(f"/api/v1/dalton/monitors/public/{slug}")
            assert resp.status_code == 404
    finally:
        await _cleanup_test_rows(
            bind_db_pool,
            monitor_ids=[mon_id] if mon_id else (),
            artifact_ids=[artifact.artifact_id],
        )


@pytest.mark.asyncio
async def test_db_fault_injection_cross_run_supersede_inside_promote_tx_and_frozen_dir(
    bind_db_pool: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fault injection 4: Run 1 claims & stages, pauses right before promote; newer Run 2 enters
    'publishing' (NOT yet 'published'). Run 1's promote tx defers (does not advance pointers and
    does not terminally supersede); once Run 2 reaches terminal 'published', replay converges Run 1
    cleanly to 'superseded'."""
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    suffix = uuid4().hex[:8]
    slug = f"supersede-tx-{suffix}"
    job_key = slug
    rel_dir = f"research-outputs/{job_key}"
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    data_root = path_map.backend_data_root
    panel_dir = data_root / rel_dir
    panel_dir.mkdir(parents=True)

    run1_id = str(uuid4())
    run2_id = str(uuid4())
    slot1 = datetime(2026, 11, 27, 6, 0, 0, tzinfo=UTC)
    slot2 = datetime(2026, 11, 27, 7, 0, 0, tzinfo=UTC)

    binding_override = MonitorDataJobBinding(
        job_key=job_key,
        slug=slug,
        title="Supersede In-Tx Test",
        category="cn",
        sort_order=0,
        visibility="company",
        rel_dir=rel_dir,
    )
    with patch.dict(data_job_registry.MONITOR_DATA_JOB_REGISTRY, {job_key: binding_override}):
        (panel_dir / "index.html").write_text("<html>Run 1</html>", encoding="utf-8")
        frozen1 = freeze_monitor_publish_artifacts(
            data_root=data_root, job_key=job_key, run_id=run1_id
        )
        assert frozen1 is not None

        (panel_dir / "index.html").write_text("<html>Run 2</html>", encoding="utf-8")
        frozen2 = freeze_monitor_publish_artifacts(
            data_root=data_root, job_key=job_key, run_id=run2_id
        )
        assert frozen2 is not None
        assert "Run 1" in (frozen1 / rel_dir / "index.html").read_text(encoding="utf-8")

    ws_id = await monitor_db.ensure_company_monitor_workspace()
    entry_path = f"{slug}/index.html"
    artifact = await artifacts_db.ensure_artifact(ws_id, entry_path)
    mon_id: str | None = None
    try:
        st0 = await monitor_db.stage_monitor_candidate_version_tx(
            artifact_id=artifact.artifact_id,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            files=[_vf(entry_path, "<html>Baseline V1</html>")],
            title="Supersede In-Tx Test",
            source_kind="data_job",
            visibility="company",
        )
        pr0 = await monitor_db.promote_verified_monitor_version_tx(
            artifact_id=artifact.artifact_id,
            candidate_seq=st0.candidate_seq,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            paths=[entry_path],
            slug=slug,
            monitor_owner_id=COMPANY_SYSTEM_USER_ID,
            title="Supersede In-Tx Test",
            category="cn",
            sort_order=0,
            visibility="company",
            keep_updated=True,
            source_kind="data_job",
            source_ref=job_key,
            member_user_ids=(),
        )
        assert pr0.monitor is not None
        mon_id = pr0.monitor.monitor_id

        async with await psycopg.AsyncConnection.connect(bind_db_pool, autocommit=True) as conn:
            async with conn.transaction():
                await _ensure_test_data_task_def(conn, job_key)
                async with conn.cursor() as cur:
                    # Both Run 1 (slot1) and Run 2 (slot2) are 'pending'
                    for rid, slot, fdir in ((run1_id, slot1, frozen1), (run2_id, slot2, frozen2)):
                        await cur.execute(
                            """
                            INSERT INTO dalton_data_task_runs (
                                run_id, job_key, trigger_source, scheduled_slot_utc, deadline_utc,
                                status, completed_at, monitor_publish_status, monitor_publish_next_retry_at,
                                monitor_publish_detail
                            )
                            VALUES (
                                %s::uuid, %s, 'manual', %s, %s,
                                'succeeded', %s, 'pending', NOW() - INTERVAL '1 second', %s::jsonb
                            )
                            """,
                            (
                                rid,
                                job_key,
                                slot,
                                slot + timedelta(minutes=30),
                                slot + timedelta(minutes=1),
                                json.dumps({"frozen_dir": str(fdir), "panels": {slug: {"status": "pending"}}}),
                            ),
                        )

            # Worker A claims Run 1 first, stages v2, and pauses right before promote!
            c1 = await claim_monitor_publish_run_tx(conn, run_id=run1_id, claimed_by="worker-a")
            assert len(c1) == 1
            st_r1 = await monitor_db.stage_monitor_candidate_version_tx(
                artifact_id=artifact.artifact_id,
                workspace_owner_id=COMPANY_SYSTEM_USER_ID,
                files=[_vf(entry_path, "<html>Run 1 Candidate</html>")],
                title="Supersede In-Tx Test",
                monitor_id=pr0.monitor.monitor_id,
                source_kind="data_job",
                visibility="company",
                run_id=run1_id,
                is_update_flow=True,
            )

            # Meanwhile Worker B claims newer Run 2 (now 'publishing', NOT yet 'published')!
            c2 = await claim_monitor_publish_run_tx(conn, run_id=run2_id, claimed_by="worker-b")
            assert len(c2) == 1

            # Now Run 1 enters promote_verified_monitor_version_tx -> in-tx fence MUST defer Run 1
            # without moving pointers and without marking it terminally superseded yet!
            r1_promote = await monitor_db.promote_verified_monitor_version_tx(
                artifact_id=artifact.artifact_id,
                candidate_seq=st_r1.candidate_seq,
                workspace_owner_id=COMPANY_SYSTEM_USER_ID,
                paths=[entry_path],
                monitor_id=pr0.monitor.monitor_id,
                run_id=run1_id,
                source_kind="data_job",
                is_update_flow=True,
                expected_claim_epoch=c1[0].claim_epoch,
                check_superseded=True,
            )
            assert r1_promote.deferred_to_newer is True
            assert r1_promote.superseded_by_newer is False
            assert r1_promote.promoted is False

            # Monitor stays on Baseline V1 until Run 2 promotes!
            mid_check = await monitor_db.get_monitor_by_id(pr0.monitor.monitor_id)
            assert mid_check is not None and mid_check.published_seq == 1

            # Once Run 2 reaches terminal 'published', Run 1 converges cleanly to 'superseded'
            assert await settle_monitor_publish_run_tx(
                conn,
                run_id=run2_id,
                claim_epoch=c2[0].claim_epoch,
                next_status="published",
                detail={"frozen_dir": str(frozen2)},
            ) is True
            await settle_monitor_publish_run_tx(
                conn,
                run_id=run1_id,
                claim_epoch=c1[0].claim_epoch,
                next_status="retrying",
                detail={"frozen_dir": str(frozen1)},
                next_retry_delay_s=-1,
            )
            with patch.dict(data_job_registry.MONITOR_DATA_JOB_REGISTRY, {job_key: binding_override}):
                await replay_pending_monitor_publishes(conn, path_map=path_map)
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT monitor_publish_status FROM dalton_data_task_runs WHERE run_id = %s::uuid",
                    (run1_id,),
                )
                r1_row = await cur.fetchone()
                assert r1_row is not None and r1_row["monitor_publish_status"] == "superseded"
    finally:
        await _cleanup_test_rows(
            bind_db_pool,
            run_ids=[run1_id, run2_id],
            job_keys=[job_key],
            monitor_ids=[mon_id] if mon_id else (),
            artifact_ids=[artifact.artifact_id],
        )


@pytest.mark.asyncio
async def test_db_all_or_nothing_batch_promote_rollback_on_second_panel_failure(
    bind_db_pool: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PG: all_or_nothing single-tx batch rolls back panel 1 when panel 2 fails a fence."""
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    ws_id = await monitor_db.ensure_company_monitor_workspace()
    suffix = uuid4().hex[:8]
    job_key = f"batch-rollback-{suffix}"
    s_a, s_b = f"batch-p1-{suffix}", f"batch-p2-{suffix}"
    e_a, e_b = f"{s_a}/index.html", f"{s_b}/index.html"
    art_a = await artifacts_db.ensure_artifact(ws_id, e_a)
    art_b = await artifacts_db.ensure_artifact(ws_id, e_b)
    # Order by artifact_id so Panel 1 is ALWAYS promoted first inside the single transaction
    # before Panel 2 trips Fence 2, deterministically exercising full Panel 1 rollback.
    if art_a.artifact_id <= art_b.artifact_id:
        s1, e1, a1 = s_a, e_a, art_a
        s2, e2, a2 = s_b, e_b, art_b
    else:
        s1, e1, a1 = s_b, e_b, art_b
        s2, e2, a2 = s_a, e_a, art_a
    mon_ids: list[str] = []

    try:
        # Seed v1 for both panels
        st1_v1 = await monitor_db.stage_monitor_candidate_version_tx(
            artifact_id=a1.artifact_id,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            files=[_vf(e1, "<html>P1 V1</html>")],
            title="Batch P1",
            source_kind="data_job",
            visibility="company",
        )
        st2_v1 = await monitor_db.stage_monitor_candidate_version_tx(
            artifact_id=a2.artifact_id,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            files=[_vf(e2, "<html>P2 V1</html>")],
            title="Batch P2",
            source_kind="data_job",
            visibility="company",
        )
        init_batch = await monitor_db.promote_verified_monitor_batch_tx(
            [
                monitor_db.PromoteMonitorItem(
                    artifact_id=a1.artifact_id,
                    candidate_seq=st1_v1.candidate_seq,
                    workspace_owner_id=COMPANY_SYSTEM_USER_ID,
                    paths=[e1],
                    slug=s1,
                    monitor_owner_id=COMPANY_SYSTEM_USER_ID,
                    title="Batch P1",
                    category="us",
                    sort_order=1,
                    visibility="company",
                    keep_updated=True,
                    source_kind="data_job",
                    source_ref=job_key,
                ),
                monitor_db.PromoteMonitorItem(
                    artifact_id=a2.artifact_id,
                    candidate_seq=st2_v1.candidate_seq,
                    workspace_owner_id=COMPANY_SYSTEM_USER_ID,
                    paths=[e2],
                    slug=s2,
                    monitor_owner_id=COMPANY_SYSTEM_USER_ID,
                    title="Batch P2",
                    category="cn",
                    sort_order=2,
                    visibility="company",
                    keep_updated=True,
                    source_kind="data_job",
                    source_ref=job_key,
                ),
            ]
        )
        assert init_batch.promoted is True
        m1, m2 = init_batch.results[0].monitor, init_batch.results[1].monitor
        assert m1 is not None and m2 is not None
        mon_ids.extend([m1.monitor_id, m2.monitor_id])

        # Stage v2 for both panels
        st1_v2 = await monitor_db.stage_monitor_candidate_version_tx(
            artifact_id=a1.artifact_id,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            files=[_vf(e1, "<html>P1 V2</html>")],
            title="Batch P1",
            monitor_id=m1.monitor_id,
            source_kind="data_job",
            visibility="company",
            is_update_flow=True,
        )
        st2_v2 = await monitor_db.stage_monitor_candidate_version_tx(
            artifact_id=a2.artifact_id,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            files=[_vf(e2, "<html>P2 V2</html>")],
            title="Batch P2",
            monitor_id=m2.monitor_id,
            source_kind="data_job",
            visibility="company",
            is_update_flow=True,
        )

        # Panel 1 is valid, Panel 2 fails Fence 2 (wrong expected_acl_revision) -> ENTIRE tx rolls back!
        with pytest.raises(SharingChanged):
            await monitor_db.promote_verified_monitor_batch_tx(
                [
                    monitor_db.PromoteMonitorItem(
                        artifact_id=a1.artifact_id,
                        candidate_seq=st1_v2.candidate_seq,
                        workspace_owner_id=COMPANY_SYSTEM_USER_ID,
                        paths=[e1],
                        monitor_id=m1.monitor_id,
                        source_kind="data_job",
                        is_update_flow=True,
                        expected_acl_revision=st1_v2.monitor_acl_revision,
                    ),
                    monitor_db.PromoteMonitorItem(
                        artifact_id=a2.artifact_id,
                        candidate_seq=st2_v2.candidate_seq,
                        workspace_owner_id=COMPANY_SYSTEM_USER_ID,
                        paths=[e2],
                        monitor_id=m2.monitor_id,
                        source_kind="data_job",
                        is_update_flow=True,
                        expected_acl_revision=(st2_v2.monitor_acl_revision or 0) + 999,
                    ),
                ]
            )

        # BOTH panels MUST still be at v1!
        m1_after = await monitor_db.get_monitor_by_id(m1.monitor_id)
        m2_after = await monitor_db.get_monitor_by_id(m2.monitor_id)
        assert m1_after is not None and m1_after.published_seq == 1
        assert m2_after is not None and m2_after.published_seq == 1
    finally:
        await _cleanup_test_rows(
            bind_db_pool,
            monitor_ids=mon_ids,
            artifact_ids=[a1.artifact_id, a2.artifact_id],
        )


@pytest.mark.asyncio
async def test_db_monitor_data_dual_axis_real_postgres(
    bind_db_pool: str,
) -> None:
    """Real PG constraints for nullable snapshot triple, 4-step sequence, and same as_of backfill."""
    ws_id = await monitor_db.ensure_company_monitor_workspace()
    slug = f"data-axis-{uuid4().hex[:8]}"
    job_key = slug
    art = await artifacts_db.ensure_artifact(ws_id, f"{slug}/index.html")
    mon_id: str | None = None
    try:
        mon = await monitor_db.upsert_monitor(
            slug=slug,
            artifact_id=art.artifact_id,
            owner_user_id=COMPANY_SYSTEM_USER_ID,
            title="Data Axis PG Test",
            category="cn",
            sort_order=0,
            visibility="company",
            keep_updated=True,
            source_kind="data_job",
            source_ref=job_key,
            member_user_ids=(),
        )
        mon_id = mon.monitor_id

        # First-boot failure with NULL payload/as_of/sha256 succeeds under CHECK constraint
        e0 = await monitor_db.upsert_monitor_data(
            mon.monitor_id, "snapshot", None, as_of=None, attempt_seq=1, is_stale=True, error="init fail"
        )
        assert e0.payload is None and e0.as_of is None and e0.is_stale is True

        t1 = datetime(2026, 11, 27, 7, 0, 0, tzinfo=UTC)
        p1 = {"index": 3400.0, "stale": False}
        e1 = await monitor_db.upsert_monitor_data(
            mon.monitor_id, "snapshot", p1, as_of=t1, attempt_seq=10, is_stale=False
        )
        assert e1.payload == p1 and e1.is_stale is False and e1.attempt_seq == 10

        e2 = await monitor_db.upsert_monitor_data(
            mon.monitor_id, "snapshot", None, as_of=None, attempt_seq=11, is_stale=True, error="net err"
        )
        assert e2.payload == p1 and e2.as_of == t1 and e2.is_stale is True and e2.attempt_seq == 11

        # Same as_of backfill with richer payload (different hash) & larger attempt_seq clears is_stale (no 409!)
        p1_backfill = {"index": 3400.0, "volume": 88888, "stale": False}
        e3 = await monitor_db.upsert_monitor_data(
            mon.monitor_id, "snapshot", p1_backfill, as_of=t1, attempt_seq=12, is_stale=False
        )
        assert e3.payload == p1_backfill and e3.is_stale is False and e3.last_error is None and e3.attempt_seq == 12

        e4 = await monitor_db.upsert_monitor_data(
            mon.monitor_id, "snapshot", None, as_of=None, attempt_seq=11, is_stale=True, error="late err"
        )
        assert e4.is_stale is False and e4.attempt_seq == 12
    finally:
        await _cleanup_test_rows(
            bind_db_pool,
            monitor_ids=[mon_id] if mon_id else (),
            artifact_ids=[art.artifact_id],
        )


@pytest.mark.asyncio
async def test_db_left_out_files_keeps_revision_and_blocks_in_flight_promote(
    bind_db_pool: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PG: set_left_out preserves share_links.revision and blocks in-flight Stage 3 promote under lock."""
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    ws_id = await monitor_db.ensure_company_monitor_workspace()
    slug = f"leftout-fence-{uuid4().hex[:8]}"
    entry_path = f"{slug}/index.html"
    secret_json = f"{slug}/data/secret.json"
    art = await artifacts_db.ensure_artifact(ws_id, entry_path)
    mon_id: str | None = None

    try:
        # Seed v1 monitor
        st1 = await monitor_db.stage_monitor_candidate_version_tx(
            artifact_id=art.artifact_id,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            files=[_vf(entry_path, "<html>v1</html>")],
            title="LeftOut Fence Test",
            source_kind="workspace",
            visibility="public",
        )
        pr1 = await monitor_db.promote_verified_monitor_version_tx(
            artifact_id=art.artifact_id,
            candidate_seq=st1.candidate_seq,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            paths=[entry_path],
            slug=slug,
            monitor_owner_id=COMPANY_SYSTEM_USER_ID,
            title="LeftOut Fence Test",
            category="overview",
            sort_order=0,
            visibility="public",
            keep_updated=True,
            source_kind="workspace",
            source_ref=f"{ws_id}:{entry_path}",
            member_user_ids=(),
        )
        assert pr1.monitor is not None
        mon_id = pr1.monitor.monitor_id

        # Stage v2 carrying data/secret.json
        st2 = await monitor_db.stage_monitor_candidate_version_tx(
            artifact_id=art.artifact_id,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            files=[
                _vf(entry_path, "<html>v2</html>"),
                _vf(secret_json, '{"secret": true}', "application/json"),
            ],
            title="LeftOut Fence Test",
            monitor_id=pr1.monitor.monitor_id,
            source_kind="workspace",
            visibility="public",
            is_update_flow=True,
        )
        assert st2.share_revision is not None
        assert st2.left_out_files == ()

        # Publisher excludes secret_json while Stage 2 is running -> revision MUST NOT advance (keeps open /a/{code} grants valid)!
        updated_art = await artifacts_records_db.set_left_out(
            user_id=COMPANY_SYSTEM_USER_ID,
            artifact_id=art.artifact_id,
            path=secret_json,
            left_out=True,
            cap=50,
        )
        assert secret_json in updated_art.left_out_files
        assert updated_art.revision == st2.share_revision

        # Repeat exclusion leaves revision unchanged too
        repeat_art = await artifacts_records_db.set_left_out(
            user_id=COMPANY_SYSTEM_USER_ID,
            artifact_id=art.artifact_id,
            path=secret_json,
            left_out=True,
            cap=50,
        )
        assert repeat_art.revision == st2.share_revision

        # Late promote with old left_out_files raises LeftOutChanged under artifact lock!
        with pytest.raises(LeftOutChanged):
            await monitor_db.promote_verified_monitor_version_tx(
                artifact_id=art.artifact_id,
                candidate_seq=st2.candidate_seq,
                workspace_owner_id=COMPANY_SYSTEM_USER_ID,
                paths=[entry_path, secret_json],
                monitor_id=pr1.monitor.monitor_id,
                source_kind="workspace",
                is_update_flow=True,
                expected_share_revision=st2.share_revision,
                expected_share_audience=st2.share_audience,
                expected_acl_revision=st2.monitor_acl_revision,
                expected_left_out_files=st2.left_out_files,
            )

        # Even without expected_left_out_files, candidate paths overlapping locked left_out_files raises LeftOutChanged!
        with pytest.raises(LeftOutChanged):
            await monitor_db.promote_verified_monitor_version_tx(
                artifact_id=art.artifact_id,
                candidate_seq=st2.candidate_seq,
                workspace_owner_id=COMPANY_SYSTEM_USER_ID,
                paths=[entry_path, secret_json],
                monitor_id=pr1.monitor.monitor_id,
                source_kind="workspace",
                is_update_flow=True,
                expected_share_revision=updated_art.revision,
                expected_share_audience=updated_art.audience,
                expected_acl_revision=st2.monitor_acl_revision,
            )

        still_v1 = await monitor_db.get_monitor_by_id(pr1.monitor.monitor_id)
        assert still_v1 is not None and still_v1.published_seq == 1
    finally:
        await _cleanup_test_rows(
            bind_db_pool,
            monitor_ids=[mon_id] if mon_id else (),
            artifact_ids=[art.artifact_id],
        )


@pytest.mark.asyncio
async def test_db_fault_injection_real_worker_sigkill_mid_publish_keeps_v1_and_scanner_recovers(
    bind_db_pool: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fault injection E2E: spawn a REAL child Python worker running process_monitor_publish_for_run,
    SIGKILL it mid-publish right before Stage 3 promote, confirm anonymous public routes keep serving
    v1 with zero partial switch, then expire lease and confirm replay scanner recovers cleanly to v2."""
    from src.config import settings as app_settings

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.setenv("HOST_MODE", "oss")
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setattr(monitor_grants._MONITOR_CODEC, "_key", None)
    monkeypatch.setattr(monitor_verify._CANDIDATE_PROBE_CODEC, "_key", None)

    ws_id = await monitor_db.ensure_company_monitor_workspace()
    suffix = uuid4().hex[:8]
    admin_uid = f"sigkill-admin-{suffix}"
    slug = f"sigkill-e2e-{suffix}"
    job_key = slug
    rel_dir = f"research-outputs/{job_key}"
    entry_path = f"{slug}/index.html"
    run_id = str(uuid4())
    slot_utc = datetime.now(UTC)

    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    data_root = path_map.backend_data_root
    panel_dir = data_root / rel_dir
    panel_dir.mkdir(parents=True)
    (panel_dir / "index.html").write_text(
        '<!doctype html><html><head><script src="app.js"></script></head><body>SIGKILL V2</body></html>',
        encoding="utf-8",
    )
    (panel_dir / "app.js").write_text("console.log('sigkill-v2');", encoding="utf-8")
    binding_override = MonitorDataJobBinding(
        job_key=job_key,
        slug=slug,
        title="SIGKILL E2E",
        category="cn",
        sort_order=0,
        visibility="public",
        rel_dir=rel_dir,
        approved_by_admin_user_id=admin_uid,
    )
    with patch.dict(data_job_registry.MONITOR_DATA_JOB_REGISTRY, {job_key: binding_override}):
        frozen_dir = freeze_monitor_publish_artifacts(
            data_root=data_root, job_key=job_key, run_id=run_id
        )
    assert frozen_dir is not None

    art = await artifacts_db.ensure_artifact(ws_id, entry_path)
    st1 = await monitor_db.stage_monitor_candidate_version_tx(
        artifact_id=art.artifact_id,
        workspace_owner_id=COMPANY_SYSTEM_USER_ID,
        files=[_vf(entry_path, "<!doctype html><html><body>SIGKILL V1</body></html>")],
        title="SIGKILL E2E",
        source_kind="data_job",
        visibility="public",
    )
    pr1 = await monitor_db.promote_verified_monitor_version_tx(
        artifact_id=art.artifact_id,
        candidate_seq=st1.candidate_seq,
        workspace_owner_id=COMPANY_SYSTEM_USER_ID,
        paths=[entry_path],
        slug=slug,
        monitor_owner_id=COMPANY_SYSTEM_USER_ID,
        title="SIGKILL E2E",
        category="cn",
        sort_order=0,
        visibility="public",
        keep_updated=True,
        source_kind="data_job",
        source_ref=job_key,
        member_user_ids=(),
    )
    assert pr1.monitor is not None
    mon_id = pr1.monitor.monitor_id

    async with await psycopg.AsyncConnection.connect(bind_db_pool, autocommit=True) as conn_setup:
        async with conn_setup.transaction():
            await _ensure_test_data_task_def(conn_setup, job_key)
            async with conn_setup.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO users (user_id, email, name, created_at, updated_at)
                    VALUES (%s, %s, %s, NOW(), NOW())
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    (admin_uid, f"{admin_uid}@dalton-test.local", admin_uid),
                )
                await cur.execute(
                    """
                    INSERT INTO dalton_user_roles (user_id, tier, created_at, updated_at)
                    VALUES (%s, 'admin', NOW(), NOW())
                    ON CONFLICT (user_id) DO UPDATE SET tier = 'admin'
                    """,
                    (admin_uid,),
                )
                await cur.execute(
                    """
                    INSERT INTO dalton_data_task_runs (
                        run_id, job_key, trigger_source, scheduled_slot_utc, deadline_utc,
                        status, completed_at, monitor_publish_status, monitor_publish_next_retry_at,
                        monitor_publish_detail
                    )
                    VALUES (
                        %s::uuid, %s, 'manual', %s, %s,
                        'succeeded', NOW(), 'pending', NOW() - INTERVAL '1 second', %s::jsonb
                    )
                    """,
                    (
                        run_id,
                        job_key,
                        slot_utc,
                        slot_utc + timedelta(minutes=30),
                        json.dumps({"frozen_dir": str(frozen_dir), "panels": {slug: {"status": "pending"}}}),
                    ),
                )

    ready_marker = tmp_path / "worker_ready_before_promote.marker"
    worker_script = tmp_path / "sigkill_worker.py"
    worker_script.write_text(
        f"""
import asyncio, os, sys
from contextlib import asynccontextmanager
from pathlib import Path
import psycopg
from src.dalton.data_jobs.container_runner import build_path_map
from src.dalton.data_jobs.scheduler import process_monitor_publish_for_run
from src.dalton.monitors import data_job_registry, db as monitor_db
from src.dalton.monitors.data_job_registry import MonitorDataJobBinding
from src.server.database import pool as db_pool_mod
import src.dalton.access_tiers, src.dalton.auth.roles, src.dalton.company_identity
import src.server.database.artifacts.records, src.server.database.artifacts.versions
import src.server.database.server_keys, src.server.database.workspace, src.server.database.workspace_file_blobs

DSN = os.environ["DALTON_TEST_PG_DSN"]
MARKER = Path({str(ready_marker)!r})

@asynccontextmanager
async def _conn_ctx(conn=None):
    if conn is not None:
        yield conn
        return
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as c:
        yield c

orig_gdc = db_pool_mod.get_db_connection
for n, m in list(sys.modules.items()):
    if n.startswith(("src.", "ptc_agent.")) and getattr(m, "get_db_connection", None) is orig_gdc:
        setattr(m, "get_db_connection", _conn_ctx)
db_pool_mod.get_db_connection = _conn_ctx

data_job_registry.MONITOR_DATA_JOB_REGISTRY[{job_key!r}] = MonitorDataJobBinding(
    job_key={job_key!r},
    slug={slug!r},
    title="SIGKILL E2E",
    category="cn",
    sort_order=0,
    visibility="public",
    rel_dir={rel_dir!r},
    approved_by_admin_user_id={admin_uid!r},
)

orig_promote = monitor_db.promote_verified_monitor_version_tx
async def _stall_before_promote(*args, **kwargs):
    MARKER.write_text("READY", encoding="utf-8")
    await asyncio.sleep(60)
    return await orig_promote(*args, **kwargs)
monitor_db.promote_verified_monitor_version_tx = _stall_before_promote

async def main():
    pm = build_path_map(host_var_dir=Path({str(tmp_path / "h")!r}), backend_var_dir=Path({str(tmp_path / "b")!r}))
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        await process_monitor_publish_for_run(conn, run_id={run_id!r}, path_map=pm, claimed_by="real-worker-sigkill")

asyncio.run(main())
""",
        encoding="utf-8",
    )

    child_env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "DALTON_TEST_PG_DSN": bind_db_pool,
        "DALTON_ARTIFACT_PUBLISHING_ENABLED": "1",
        "DALTON_AUTH_MODE": "local",
        "HOST_MODE": "oss",
        "DALTON_CAP_DATA_JOBS": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, str(worker_script)],
        cwd=str(REPO_ROOT),
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30.0
        while not ready_marker.exists():
            if proc.poll() is not None:
                out, err = proc.communicate(timeout=5)
                raise AssertionError(
                    f"Child worker exited early with code {proc.returncode}:\nSTDOUT:\n{out}\nSTDERR:\n{err}"
                )
            if time.monotonic() > deadline:
                raise AssertionError("Timed out waiting for child worker to reach pre-promote stage")
            await asyncio.sleep(0.1)

        # Hard-kill the REAL worker process mid-publish right before Stage 3 promote!
        os.kill(proc.pid, signal.SIGKILL)
        exit_code = proc.wait(timeout=10)
        assert exit_code == -signal.SIGKILL

        # Verify in Postgres: candidate v2 was staged, run is stuck in 'publishing' epoch=1,
        # and anonymous public routes STILL serve v1 cleanly (v2 is 404).
        app = FastAPI()
        app.include_router(monitors_router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            detail_v1 = await client.get(f"/api/v1/dalton/monitors/public/{slug}")
            assert detail_v1.status_code == 200
            assert detail_v1.json()["monitor"]["last_verified_seq"] == 1

            v1_page = await client.get(f"/api/v1/dalton/monitors/public/{slug}/v/1/")
            assert v1_page.status_code == 200
            assert "SIGKILL V1" in v1_page.text

            v2_404 = await client.get(f"/api/v1/dalton/monitors/public/{slug}/v/2/")
            assert v2_404.status_code == 404

        # Expire the killed worker's lease -> replay scanner takes over (epoch=2) and completes v2!
        with patch.dict(data_job_registry.MONITOR_DATA_JOB_REGISTRY, {job_key: binding_override}):
            async with await psycopg.AsyncConnection.connect(bind_db_pool, autocommit=True) as conn_rec:
                await conn_rec.execute(
                    """
                    UPDATE dalton_data_task_runs
                       SET monitor_publish_lease_expires_at = NOW() - INTERVAL '5 seconds'
                     WHERE run_id = %s::uuid
                    """,
                    (run_id,),
                )
                recovered = await replay_pending_monitor_publishes(
                    conn_rec, path_map=path_map, claimed_by="scanner-after-sigkill"
                )
                assert recovered >= 1
                async with conn_rec.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT monitor_publish_status, monitor_publish_claim_epoch
                          FROM dalton_data_task_runs
                         WHERE run_id = %s::uuid
                        """,
                        (run_id,),
                    )
                    run_after = await cur.fetchone()
                    assert run_after is not None
                    assert run_after["monitor_publish_status"] == "published"
                    assert int(run_after["monitor_publish_claim_epoch"]) == 2

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            detail_v2 = await client.get(f"/api/v1/dalton/monitors/public/{slug}")
            assert detail_v2.status_code == 200
            assert detail_v2.json()["monitor"]["last_verified_seq"] == 2

            v2_page = await client.get(f"/api/v1/dalton/monitors/public/{slug}/v/2/")
            assert v2_page.status_code == 200
            assert "SIGKILL V2" in v2_page.text

            v2_js = await client.get(f"/api/v1/dalton/monitors/public/{slug}/v/2/app.js")
            assert v2_js.status_code == 200
            assert "sigkill-v2" in v2_js.text
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        await _cleanup_test_rows(
            bind_db_pool,
            run_ids=[run_id],
            job_keys=[job_key],
            monitor_ids=[mon_id],
            artifact_ids=[art.artifact_id],
            user_ids=[admin_uid],
        )


@pytest.mark.asyncio
async def test_db_fault_injection_first_pass_vs_scanner_race_takeover_and_completion(
    bind_db_pool: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fault injection E2E: first-pass worker and replay scanner race concurrently on the same
    pending run; winner stalls before Stage 3 promote, lease expires, scanner takes over and
    completes publish cleanly, and the stalled first-pass worker is fenced out inside promote tx."""
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    ws_id = await monitor_db.ensure_company_monitor_workspace()
    suffix = uuid4().hex[:8]
    slug = f"race-takeover-{suffix}"
    job_key = slug
    rel_dir = f"research-outputs/{job_key}"
    entry_path = f"{slug}/index.html"
    run_id = str(uuid4())
    slot_utc = datetime.now(UTC)

    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    data_root = path_map.backend_data_root
    panel_dir = data_root / rel_dir
    panel_dir.mkdir(parents=True)
    (panel_dir / "index.html").write_text(
        '<!doctype html><html><head><script src="app.js"></script></head><body>Race V2</body></html>',
        encoding="utf-8",
    )
    (panel_dir / "app.js").write_text("console.log('race-v2');", encoding="utf-8")
    binding_override = MonitorDataJobBinding(
        job_key=job_key,
        slug=slug,
        title="Race Takeover E2E",
        category="cn",
        sort_order=0,
        visibility="company",
        rel_dir=rel_dir,
    )
    with patch.dict(data_job_registry.MONITOR_DATA_JOB_REGISTRY, {job_key: binding_override}):
        frozen_dir = freeze_monitor_publish_artifacts(
            data_root=data_root, job_key=job_key, run_id=run_id
        )
    assert frozen_dir is not None

    art = await artifacts_db.ensure_artifact(ws_id, entry_path)
    st1 = await monitor_db.stage_monitor_candidate_version_tx(
        artifact_id=art.artifact_id,
        workspace_owner_id=COMPANY_SYSTEM_USER_ID,
        files=[_vf(entry_path, "<!doctype html><html><body>Race V1</body></html>")],
        title="Race Takeover E2E",
        source_kind="data_job",
        visibility="company",
    )
    pr1 = await monitor_db.promote_verified_monitor_version_tx(
        artifact_id=art.artifact_id,
        candidate_seq=st1.candidate_seq,
        workspace_owner_id=COMPANY_SYSTEM_USER_ID,
        paths=[entry_path],
        slug=slug,
        monitor_owner_id=COMPANY_SYSTEM_USER_ID,
        title="Race Takeover E2E",
        category="cn",
        sort_order=0,
        visibility="company",
        keep_updated=True,
        source_kind="data_job",
        source_ref=job_key,
        member_user_ids=(),
    )
    assert pr1.monitor is not None
    mon_id = pr1.monitor.monitor_id

    async with await psycopg.AsyncConnection.connect(bind_db_pool, autocommit=True) as conn_setup:
        async with conn_setup.transaction():
            await _ensure_test_data_task_def(conn_setup, job_key)
            async with conn_setup.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO dalton_data_task_runs (
                        run_id, job_key, trigger_source, scheduled_slot_utc, deadline_utc,
                        status, completed_at, monitor_publish_status, monitor_publish_next_retry_at,
                        monitor_publish_detail
                    )
                    VALUES (
                        %s::uuid, %s, 'manual', %s, %s,
                        'succeeded', NOW(), 'pending', NOW() - INTERVAL '1 second', %s::jsonb
                    )
                    """,
                    (
                        run_id,
                        job_key,
                        slot_utc,
                        slot_utc + timedelta(minutes=30),
                        json.dumps({"frozen_dir": str(frozen_dir), "panels": {slug: {"status": "pending"}}}),
                    ),
                )

    epoch1_entered_promote = asyncio.Event()
    release_epoch1 = asyncio.Event()
    orig_promote = monitor_db.promote_verified_monitor_version_tx

    async def _gated_promote(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("expected_claim_epoch") == 1:
            epoch1_entered_promote.set()
            await asyncio.wait_for(release_epoch1.wait(), timeout=20.0)
        return await orig_promote(*args, **kwargs)

    conn_first = await psycopg.AsyncConnection.connect(bind_db_pool, autocommit=True)
    conn_scan_race = await psycopg.AsyncConnection.connect(bind_db_pool, autocommit=True)
    conn_scan_takeover = await psycopg.AsyncConnection.connect(bind_db_pool, autocommit=True)
    try:
        with (
            patch.dict(data_job_registry.MONITOR_DATA_JOB_REGISTRY, {job_key: binding_override}),
            patch.object(monitor_db, "promote_verified_monitor_version_tx", _gated_promote),
        ):
            # Step 1: First-pass and replay scanner race concurrently on the pending run.
            first_task = asyncio.create_task(
                process_monitor_publish_for_run(
                    conn_first, run_id=run_id, path_map=path_map, claimed_by="first-pass-worker"
                )
            )
            scan_race_task = asyncio.create_task(
                replay_pending_monitor_publishes(
                    conn_scan_race, path_map=path_map, claimed_by="scanner-race-worker"
                )
            )
            await asyncio.wait_for(epoch1_entered_promote.wait(), timeout=15.0)
            # Exactly one of (first_task, scan_race_task) won epoch=1 and stalled at promote;
            # the other MUST have observed SKIP LOCKED and finished without claiming.
            done, pending = await asyncio.wait(
                {first_task, scan_race_task}, timeout=5.0, return_when=asyncio.FIRST_COMPLETED
            )
            assert len(done) == 1 and len(pending) == 1
            loser_task = next(iter(done))
            winner_task = next(iter(pending))
            loser_res = await loser_task
            assert loser_res in (None, 0)

            # Monitor is STILL on v1 while epoch=1 winner is stalled!
            mid_mon = await monitor_db.get_monitor_by_id(mon_id)
            assert mid_mon is not None and mid_mon.published_seq == 1

            # Step 2: Lease expires -> scanner takes over with claim_epoch=2 and completes publish!
            await conn_scan_takeover.execute(
                """
                UPDATE dalton_data_task_runs
                   SET monitor_publish_lease_expires_at = NOW() - INTERVAL '5 seconds'
                 WHERE run_id = %s::uuid
                """,
                (run_id,),
            )
            takeover_count = await replay_pending_monitor_publishes(
                conn_scan_takeover, path_map=path_map, claimed_by="scanner-takeover-worker"
            )
            assert takeover_count >= 1

            after_takeover = await monitor_db.get_monitor_by_id(mon_id)
            assert after_takeover is not None
            assert after_takeover.published_seq == 2
            assert after_takeover.last_verified_seq == 2

            # Step 3: Release stalled epoch=1 winner -> fenced out inside promote tx & settle!
            release_epoch1.set()
            winner_res = await asyncio.wait_for(winner_task, timeout=15.0)
            assert winner_res in ("fenced_out", 1)

            async with conn_scan_takeover.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT monitor_publish_status, monitor_publish_claim_epoch
                      FROM dalton_data_task_runs
                     WHERE run_id = %s::uuid
                    """,
                    (run_id,),
                )
                final_run = await cur.fetchone()
                assert final_run is not None
                assert final_run["monitor_publish_status"] == "published"
                assert int(final_run["monitor_publish_claim_epoch"]) == 2
    finally:
        release_epoch1.set()
        await conn_first.close()
        await conn_scan_race.close()
        await conn_scan_takeover.close()
        await _cleanup_test_rows(
            bind_db_pool,
            run_ids=[run_id],
            job_keys=[job_key],
            monitor_ids=[mon_id],
            artifact_ids=[art.artifact_id],
        )


def test_db_d06y_alembic_upgrade_downgrade_roundtrip(migrated_monitor_dsn: str) -> None:
    """Real PG Alembic round-trip: downgrade d061 -> d060 and re-upgrade d060 -> head (d061)."""
    cfg = _dalton_alembic_cfg(migrated_monitor_dsn)
    try:
        alembic_command.downgrade(cfg, "d060")
    finally:
        alembic_command.upgrade(cfg, "head")
