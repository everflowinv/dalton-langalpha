"""Real-PostgreSQL integration tests for External Member Permissions Batch 1.

Covers:
1. `commit_monitor_publish_tx` (`POST /monitors`) dual-connection `FOR SHARE` on
   `dalton_user_roles` vs `set_user_role` (`FOR NO KEY UPDATE`) bidirectional blocking
   and post-commit demotion rejection (`403 external_member_forbidden`).
2. `commit_monitor_version_tx` / `_cut_and_finalize_monitor_in_cur` (`republish` and
   post-turn auto-update) dual-connection `FOR SHARE` vs `set_user_role` bidirectional
   blocking and post-commit demotion rejection (`403 external_member_forbidden`).
3. Demoted external owner lifecycle on real PG: `get_monitor_by_artifact` (`can_manage=True`),
   `PATCH /monitors/{slug}` independent downgrade to `own` without cutting a version,
   enabling `keep_updated=True` on `own` monitor (R8 #2), and `COMPANY_SYSTEM_USER_ID`
   data-job monitor preservation.
4. Atomic unshare (`POST /artifacts/{id}/unshare` & `PATCH /workspaces/{id}/share-links/{code}`)
   lock ordering (`share_links -> dalton_monitors FOR UPDATE`), `public -> own` downgrade,
   `acl_revision` & `share_links.revision` increment, admin takeover unshare by original
   file owner, and concurrent stale `ReadBasis` `409 SharingChanged`.
5. `list_shared` (`SELECT {_COLUMNS}`) end-to-end DB + HTTP model serialization with
   normal shared files, historical `shared_at IS NULL` public monitors, and private `app`
   share links (`command_key`).
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import create_autospec, patch

import psycopg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from src.config import settings as app_settings
from src.dalton.auth import roles as auth_roles
from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.monitors import db as monitor_db
from src.dalton.monitors import grants as monitor_grants
from src.dalton.monitors import service as monitor_service
from src.dalton.monitors.models import UpdateMonitorRequest
from src.server.app import share_links as share_links_app
from src.server.database import artifacts as artifacts_db
from src.server.database import share_links as share_links_db
from src.server.database.artifacts import VersionFile
from src.server.database.artifacts.versions import ReadBasis, SharingChanged
from src.server.services import signed_grants

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def dalton_db_pool(
    patched_get_db_connection,
    dalton_schema,
    test_db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
):
    """Bind a function-loop AsyncConnectionPool + Dalton migrations + tuple-safe get_server_key.

    `test_db_pool` is session-scoped with `min_size=1`: its background pool-growth
    workers live on the session event loop, which is frozen while a function-scoped
    async test runs. Checking out a 2nd concurrent connection from `test_db_pool`
    inside one test would therefore stall waiting for the frozen session loop to
    grow the pool. Opening a pool on this test's own live loop avoids that stall.
    """
    local_pool = AsyncConnectionPool(
        conninfo=test_db_uri,
        min_size=2,
        max_size=8,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        open=False,
    )
    await local_pool.open()
    await local_pool.wait()

    @asynccontextmanager
    async def _live_get_db_connection(conn: Any = None):
        if conn is not None:
            yield conn
            return
        async with local_pool.connection(timeout=10.0) as owned:
            yield owned

    for name, module in list(sys.modules.items()):
        if name.startswith(("src.", "ptc_agent.")) and (
            getattr(module, "get_db_connection", None) is patched_get_db_connection
        ):
            monkeypatch.setattr(module, "get_db_connection", _live_get_db_connection)
    assert auth_roles.get_db_connection is _live_get_db_connection
    assert monitor_db.get_db_connection is _live_get_db_connection

    async def _tuple_safe_get_server_key(name: str) -> bytes:
        async with await psycopg.AsyncConnection.connect(test_db_uri) as kconn:
            async with kconn.cursor() as kcur:
                await kcur.execute(
                    "SELECT secret FROM server_keys WHERE name = %s", (name,)
                )
                krow = await kcur.fetchone()
        if not krow or not krow[0]:
            raise LookupError(f"server key {name!r} is not provisioned")
        return bytes(krow[0])

    monitor_grants._MONITOR_CODEC._key = None
    monkeypatch.setattr(
        signed_grants,
        "get_server_key",
        create_autospec(
            signed_grants.get_server_key, side_effect=_tuple_safe_get_server_key
        ),
    )
    try:
        yield test_db_uri
    finally:
        await local_pool.close()


def _html_version_file(
    content: bytes = b"<html><body>v1</body></html>",
    *,
    path: str = "report.html",
) -> list[VersionFile]:
    import hashlib

    return [
        VersionFile(
            path=path,
            sha256=hashlib.sha256(content).hexdigest(),
            byte_len=len(content),
            content_type="text/html",
            content=content,
        )
    ]


async def _wait_for_pg_lock_wait(
    pg_dsn: str,
    *,
    blocker_pid: int,
    waiter_task: asyncio.Task[Any],
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Poll `pg_locks` until another backend has `granted = false` blocked by `blocker_pid`."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    activity_rows: list[dict[str, Any]] = []
    async with await psycopg.AsyncConnection.connect(
        pg_dsn, autocommit=True, row_factory=dict_row
    ) as obs_conn:
        async with obs_conn.cursor() as obs_cur:
            while loop.time() < deadline:
                if waiter_task.done():
                    raise AssertionError(
                        "Concurrent waiter task completed without blocking on "
                        f"blocker_pid={blocker_pid}"
                    )
                await obs_cur.execute(
                    """
                    SELECT l.pid, l.locktype, l.mode, l.granted
                      FROM pg_locks l
                     WHERE l.granted = false
                       AND %s = ANY(pg_blocking_pids(l.pid))
                     LIMIT 1
                    """,
                    (blocker_pid,),
                )
                row = await obs_cur.fetchone()
                if row is not None:
                    return dict(row)
                await asyncio.sleep(0.02)
            await obs_cur.execute(
                """
                SELECT pid,
                       state,
                       wait_event_type,
                       wait_event,
                       pg_blocking_pids(pid) AS blocking_pids,
                       left(query, 120) AS query
                  FROM pg_stat_activity
                 WHERE datname = current_database()
                   AND pid <> pg_backend_pid()
                 ORDER BY pid
                """
            )
            activity_rows = [dict(r) for r in await obs_cur.fetchall()]
    activity_summary = (
        "\n".join(
            f"  pid={r['pid']} state={r['state']!r} "
            f"wait={r['wait_event_type']}:{r['wait_event']} "
            f"blocking={r['blocking_pids']} query={r['query']!r}"
            for r in activity_rows
        )
        or "  <no other connections>"
    )
    raise AssertionError(
        f"Timed out waiting for pg_locks(granted=false) blocked by pid={blocker_pid}\n"
        f"pg_stat_activity (current_database):\n{activity_summary}"
    )


async def _seed_user_and_workspace(
    pg_dsn: str,
    *,
    user_id: str | None = None,
    tier: str = "internal",
    preview_servers: dict[str, Any] | None = None,
) -> tuple[str, str]:
    import json

    uid = user_id or f"test-emp-{uuid.uuid4().hex[:10]}"
    ws_id = str(uuid.uuid4())
    email = f"{uid}@dalton-emp-test.local"
    artifacts_json = json.dumps(
        {"preview_servers": preview_servers} if preview_servers else {}
    )
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, email, name, created_at, updated_at)
            VALUES (%s, %s, %s, NOW(), NOW())
            ON CONFLICT (user_id) DO UPDATE SET email = EXCLUDED.email
            """,
            (uid, email, uid),
        )
        await conn.execute(
            """
            INSERT INTO dalton_user_roles (user_id, tier, created_at, updated_at)
            VALUES (%s, %s, NOW(), NOW())
            ON CONFLICT (user_id) DO UPDATE SET tier = EXCLUDED.tier, updated_at = NOW()
            """,
            (uid, tier),
        )
        await conn.execute(
            """
            INSERT INTO workspaces (
                workspace_id, user_id, name, status, artifacts, created_at, updated_at
            )
            VALUES (%s::uuid, %s, %s, 'running', %s::jsonb, NOW(), NOW())
            """,
            (ws_id, uid, f"ws-{uid}", artifacts_json),
        )
    return uid, ws_id


async def _cleanup_user_and_workspace(pg_dsn: str, uid: str, ws_id: str) -> None:
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        await conn.execute(
            "DELETE FROM dalton_monitors WHERE owner_user_id = %s OR source_ref LIKE %s",
            (uid, f"{ws_id}:%"),
        )
        await conn.execute(
            "DELETE FROM share_links WHERE workspace_id = %s::uuid",
            (ws_id,),
        )
        await conn.execute(
            "DELETE FROM workspaces WHERE workspace_id = %s::uuid",
            (ws_id,),
        )
        await conn.execute(
            "DELETE FROM dalton_user_roles WHERE user_id = %s",
            (uid,),
        )
        await conn.execute(
            "DELETE FROM users WHERE user_id = %s",
            (uid,),
        )


# ---------------------------------------------------------------------------
# 1. commit_monitor_publish_tx (POST /monitors) FOR SHARE vs set_user_role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_commit_monitor_publish_tx_for_share_blocks_and_catches_demotion(
    dalton_db_pool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")

    # Seed an admin so set_user_role's last-admin guard is satisfied
    admin_uid, admin_ws = await _seed_user_and_workspace(dalton_db_pool, tier="admin")
    uid, ws_id = await _seed_user_and_workspace(dalton_db_pool, tier="internal")
    slug = f"emp-pub-{uuid.uuid4().hex[:8]}"
    try:
        artifact = await artifacts_db.ensure_artifact(ws_id, "report.html")
        # Initial publish as internal member with visibility='company'
        mon, _res = await monitor_db.commit_monitor_publish_tx(
            slug=slug,
            artifact_id=artifact.artifact_id,
            workspace_owner_id=uid,
            monitor_owner_id=uid,
            title="Initial Company Board",
            category="overview",
            sort_order=0,
            visibility="company",
            keep_updated=True,
            source_kind="workspace",
            source_ref=f"{ws_id}:report.html",
            member_user_ids=(),
            files=_html_version_file(b"<html>v1</html>"),
        )
        assert mon.visibility == "company"
        assert mon.published_seq == 1

        # (a) Direction 1: Connection A holds FOR SHARE on dalton_user_roles ->
        # Connection B calling set_user_role(uid, 'external') blocks until A commits.
        conn_a = await psycopg.AsyncConnection.connect(dalton_db_pool, autocommit=False)
        try:
            async with conn_a.cursor(row_factory=dict_row) as cur_a:
                live_tier = await monitor_db._resolve_live_tier_in_cur(cur_a, uid)
                assert live_tier == "internal"

                role_update_done = asyncio.Event()

                async def _run_set_role() -> None:
                    await auth_roles.set_user_role(uid, "external")
                    role_update_done.set()

                role_task = asyncio.create_task(_run_set_role())
                await asyncio.sleep(0.15)
                assert not role_update_done.is_set(), (
                    "set_user_role must block while publish transaction holds FOR SHARE"
                )
                await conn_a.commit()
                await asyncio.wait_for(role_task, timeout=5.0)
                assert role_update_done.is_set()
        finally:
            await conn_a.close()

        # Reset back to 'internal' for Direction 2
        await auth_roles.set_user_role(uid, "internal")

        # (b) Direction 2: Connection B holds uncommitted demotion (internal -> external)
        # on dalton_user_roles -> commit_monitor_publish_tx (even passing visibility='own'
        # on existing 'company' monitor) blocks on FOR SHARE, then after B commits,
        # reads live_tier == 'external' and existing.visibility == 'company' and raises 403!
        conn_b = await psycopg.AsyncConnection.connect(dalton_db_pool, autocommit=False)
        try:
            async with conn_b.cursor() as cur_b:
                await cur_b.execute(
                    "UPDATE dalton_user_roles SET tier = 'external', updated_at = NOW() WHERE user_id = %s",
                    (uid,),
                )

                publish_finished = asyncio.Event()

                async def _concurrent_publish() -> Any:
                    try:
                        return await monitor_db.commit_monitor_publish_tx(
                            slug=slug,
                            artifact_id=artifact.artifact_id,
                            workspace_owner_id=uid,
                            monitor_owner_id=uid,
                            title="Attempted Direct New Version",
                            category="overview",
                            sort_order=0,
                            visibility="own",
                            keep_updated=True,
                            source_kind="workspace",
                            source_ref=f"{ws_id}:report.html",
                            member_user_ids=(),
                            files=_html_version_file(b"<html>v2</html>"),
                        )
                    finally:
                        publish_finished.set()

                pub_task = asyncio.create_task(_concurrent_publish())
                await asyncio.sleep(0.15)
                assert not publish_finished.is_set(), (
                    "commit_monitor_publish_tx must block on FOR SHARE while role update is uncommitted"
                )
                await conn_b.commit()

                with pytest.raises(HTTPException) as exc_demoted:
                    await asyncio.wait_for(pub_task, timeout=5.0)
                assert exc_demoted.value.status_code == 403
                assert exc_demoted.value.detail["code"] == "external_member_forbidden"
        finally:
            await conn_b.close()

        # Verify published_seq was not advanced
        after_mon = await monitor_db.get_monitor_by_slug(slug)
        assert after_mon is not None
        assert after_mon.published_seq == 1
        assert after_mon.visibility == "company"
    finally:
        await _cleanup_user_and_workspace(dalton_db_pool, uid, ws_id)
        await _cleanup_user_and_workspace(dalton_db_pool, admin_uid, admin_ws)


# ---------------------------------------------------------------------------
# 2. commit_monitor_version_tx (republish / auto-update) FOR SHARE vs set_user_role
#    & Demoted Owner Independent Downgrade + keep_updated=True (R8 #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_commit_monitor_version_tx_for_share_and_demoted_downgrade_keep_updated(
    dalton_db_pool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")

    admin_uid, admin_ws = await _seed_user_and_workspace(dalton_db_pool, tier="admin")
    uid, ws_id = await _seed_user_and_workspace(dalton_db_pool, tier="internal")
    slug = f"emp-ver-{uuid.uuid4().hex[:8]}"
    try:
        artifact = await artifacts_db.ensure_artifact(ws_id, "report.html")
        mon, _res = await monitor_db.commit_monitor_publish_tx(
            slug=slug,
            artifact_id=artifact.artifact_id,
            workspace_owner_id=uid,
            monitor_owner_id=uid,
            title="Auto Update Board",
            category="overview",
            sort_order=0,
            visibility="company",
            keep_updated=True,
            source_kind="workspace",
            source_ref=f"{ws_id}:report.html",
            member_user_ids=(),
            files=_html_version_file(b"<html>v1</html>"),
        )
        assert mon.published_seq == 1

        # Hold uncommitted demotion in conn_b -> commit_monitor_version_tx blocks on FOR SHARE,
        # then fails with 403 external_member_forbidden once conn_b commits.
        conn_b = await psycopg.AsyncConnection.connect(dalton_db_pool, autocommit=False)
        try:
            async with conn_b.cursor() as cur_b:
                await cur_b.execute(
                    "UPDATE dalton_user_roles SET tier = 'external', updated_at = NOW() WHERE user_id = %s",
                    (uid,),
                )
                cut_done = asyncio.Event()

                async def _concurrent_cut() -> Any:
                    try:
                        return await monitor_db.commit_monitor_version_tx(
                            monitor_id=mon.monitor_id,
                            artifact_id=artifact.artifact_id,
                            workspace_owner_id=uid,
                            files=_html_version_file(b"<html>v2</html>"),
                            title=mon.title,
                        )
                    finally:
                        cut_done.set()

                cut_task = asyncio.create_task(_concurrent_cut())
                await asyncio.sleep(0.15)
                assert not cut_done.is_set(), (
                    "commit_monitor_version_tx must block on FOR SHARE while role update is uncommitted"
                )
                await conn_b.commit()

                with pytest.raises(HTTPException) as exc_cut:
                    await asyncio.wait_for(cut_task, timeout=5.0)
                assert exc_cut.value.status_code == 403
                assert exc_cut.value.detail["code"] == "external_member_forbidden"
        finally:
            await conn_b.close()

        # Demoted external owner can read metadata via get_monitor_by_artifact with can_manage=True
        meta = await monitor_service.get_monitor_by_artifact(
            artifact.artifact_id, viewer_id=uid
        )
        assert meta is not None
        assert meta["can_manage"] is True
        assert meta["visibility"] == "company"

        # Independent downgrade via PATCH / update_monitor_settings to 'own' succeeds without cutting version
        downgraded = await monitor_service.update_monitor_settings(
            slug,
            UpdateMonitorRequest(visibility="own", keep_updated=False),
            caller_id=uid,
        )
        assert downgraded["monitor"]["visibility"] == "own"
        assert downgraded["monitor"]["published_seq"] == 1
        assert downgraded["monitor"]["acl_revision"] == mon.acl_revision + 1

        # R8 #2: External owner can enable keep_updated=True on their own 'own' monitor
        reenabled = await monitor_service.update_monitor_settings(
            slug,
            UpdateMonitorRequest(keep_updated=True),
            caller_id=uid,
        )
        assert reenabled["monitor"]["visibility"] == "own"
        assert reenabled["monitor"]["keep_updated"] is True

        # And now commit_monitor_version_tx succeeds for the external owner's 'own' monitor!
        updated_mon, cut_res, deduped = await monitor_db.commit_monitor_version_tx(
            monitor_id=mon.monitor_id,
            artifact_id=artifact.artifact_id,
            workspace_owner_id=uid,
            files=_html_version_file(b"<html>v2-own</html>"),
            title=mon.title,
        )
        assert deduped is False
        assert cut_res is not None and cut_res.cut is True
        assert updated_mon.published_seq == 2
        assert updated_mon.visibility == "own"

        # Data-job monitor owned by COMPANY_SYSTEM_USER_ID continues to cut normally
        company_ws_id = await monitor_db.ensure_company_monitor_workspace()
        job_art = await artifacts_db.ensure_artifact(company_ws_id, f"{slug}/index.html")
        job_mon = await monitor_db.upsert_monitor(
            slug=f"{slug}-job",
            artifact_id=job_art.artifact_id,
            owner_user_id=COMPANY_SYSTEM_USER_ID,
            title="Company Data Job",
            category="overview",
            sort_order=0,
            visibility="company",
            keep_updated=True,
            source_kind="data_job",
            source_ref=f"job-{slug}",
            member_user_ids=(),
        )
        try:
            job_updated, job_res, _ = await monitor_db.commit_monitor_version_tx(
                monitor_id=job_mon.monitor_id,
                artifact_id=job_art.artifact_id,
                workspace_owner_id=COMPANY_SYSTEM_USER_ID,
                files=_html_version_file(
                    b"<html>job-v1</html>", path=f"{slug}/index.html"
                ),
                title=job_mon.title,
                run_id=f"run-{uuid.uuid4().hex[:6]}",
            )
            assert job_res is not None and job_res.cut is True
            assert job_updated.published_seq == 1
        finally:
            async with await psycopg.AsyncConnection.connect(
                dalton_db_pool, autocommit=True
            ) as c:
                await c.execute(
                    "DELETE FROM dalton_monitors WHERE monitor_id = %s::uuid",
                    (job_mon.monitor_id,),
                )
                await c.execute(
                    "DELETE FROM share_links WHERE artifact_id = %s::uuid",
                    (job_art.artifact_id,),
                )
    finally:
        await _cleanup_user_and_workspace(dalton_db_pool, uid, ws_id)
        await _cleanup_user_and_workspace(dalton_db_pool, admin_uid, admin_ws)


# ---------------------------------------------------------------------------
# 3. Atomic Unshare + Admin Takeover + Stale ReadBasis 409 + Public Routes 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_atomic_unshare_downgrades_public_monitor_and_invalidates_read_basis(
    dalton_db_pool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")

    admin_a, ws_a = await _seed_user_and_workspace(dalton_db_pool, tier="admin")
    admin_b, ws_b = await _seed_user_and_workspace(dalton_db_pool, tier="admin")
    slug = f"emp-unshare-{uuid.uuid4().hex[:8]}"
    try:
        artifact = await artifacts_db.ensure_artifact(ws_a, "report.html")
        mon, _ = await monitor_db.commit_monitor_publish_tx(
            slug=slug,
            artifact_id=artifact.artifact_id,
            workspace_owner_id=admin_a,
            monitor_owner_id=admin_a,
            title="Public Board",
            category="overview",
            sort_order=0,
            visibility="public",
            keep_updated=True,
            source_kind="workspace",
            source_ref=f"{ws_a}:report.html",
            member_user_ids=(),
            files=_html_version_file(b"<html>public-v1</html>"),
        )
        assert mon.visibility == "public"

        # Admin B takes over the public monitor (dalton_monitors.owner_user_id becomes admin_b,
        # while workspace owner of share_links remains admin_a)
        assert await auth_roles.get_user_role(admin_b) == "admin"
        taken_over = await monitor_db.update_monitor_settings_tx(
            mon.monitor_id,
            artifact_id=artifact.artifact_id,
            owner_user_id=admin_a,
            caller_id=admin_b,
            title="Taken Over Public Board",
            category=None,
            sort_order=None,
            visibility="public",
            keep_updated=None,
            member_user_ids=None,
        )
        assert taken_over.owner_user_id == admin_b
        assert taken_over.visibility == "public"
        prev_acl_rev = taken_over.acl_revision

        # Capture ReadBasis before unshare (simulating a concurrent publish/republish snapshot)
        shared_art_before = await artifacts_db.get_artifact(
            artifact.artifact_id, user_id=admin_a
        )
        assert shared_art_before is not None and shared_art_before.shared is True
        stale_basis = ReadBasis.of(shared_art_before)

        # Original workspace owner admin_a unshares via atomic helper
        unshared_art = await artifacts_db.unshare_artifact_and_downgrade_public_monitor(
            user_id=admin_a,
            artifact_id=artifact.artifact_id,
        )
        assert unshared_art.shared is False
        assert unshared_art.shared_at is None
        assert unshared_art.shared_seq == 0
        assert unshared_art.revision == shared_art_before.revision + 1

        # Monitor was atomically downgraded to 'own' and acl_revision incremented
        after_mon = await monitor_db.get_monitor_by_slug(slug)
        assert after_mon is not None
        assert after_mon.visibility == "own"
        assert after_mon.acl_revision == prev_acl_rev + 1

        # Public anonymous monitor endpoint now returns 404
        with pytest.raises(HTTPException) as exc_pub_detail:
            await monitor_service.get_public_monitor_detail(slug)
        assert exc_pub_detail.value.status_code == 404

        # Concurrent late commit_monitor_version_tx holding pre-unshare ReadBasis fails with SharingChanged (409)
        with pytest.raises(SharingChanged):
            await monitor_db.commit_monitor_version_tx(
                monitor_id=mon.monitor_id,
                artifact_id=artifact.artifact_id,
                workspace_owner_id=admin_a,
                files=_html_version_file(b"<html>stale-v2</html>"),
                title="Stale Cut",
                read_basis=stale_basis,
            )
    finally:
        await _cleanup_user_and_workspace(dalton_db_pool, admin_a, ws_a)
        await _cleanup_user_and_workspace(dalton_db_pool, admin_b, ws_b)


# ---------------------------------------------------------------------------
# 4. list_shared (SELECT {_COLUMNS}) End-to-End DB + HTTP Serialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_list_shared_columns_historical_public_monitor_and_app_link(
    dalton_db_pool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")

    admin_uid, ws_id = await _seed_user_and_workspace(
        dalton_db_pool,
        tier="admin",
        preview_servers={"3000": "python -m http.server 3000"},
    )
    slug_hist = f"emp-hist-{uuid.uuid4().hex[:8]}"
    try:
        # 1) Normal shared file (shared_at IS NOT NULL)
        art_normal = await artifacts_db.ensure_artifact(ws_id, "normal.html")
        await monitor_db.commit_monitor_publish_tx(
            slug=f"emp-norm-{uuid.uuid4().hex[:8]}",
            artifact_id=art_normal.artifact_id,
            workspace_owner_id=admin_uid,
            monitor_owner_id=admin_uid,
            title="Normal Public Monitor",
            category="overview",
            sort_order=0,
            visibility="public",
            keep_updated=False,
            source_kind="workspace",
            source_ref=f"{ws_id}:normal.html",
            member_user_ids=(),
            files=_html_version_file(b"<html>normal</html>", path="normal.html"),
        )

        # 2) Historical public monitor file where share_links.shared_at IS NULL
        art_hist = await artifacts_db.ensure_artifact(ws_id, "historical.html")
        await monitor_db.upsert_monitor(
            slug=slug_hist,
            artifact_id=art_hist.artifact_id,
            owner_user_id=admin_uid,
            title="Historical Public Monitor",
            category="overview",
            sort_order=0,
            visibility="public",
            keep_updated=False,
            source_kind="workspace",
            source_ref=f"{ws_id}:historical.html",
            member_user_ids=(),
        )
        # Ensure share_links.shared_at is NULL on historical artifact
        hist_check = await artifacts_db.get_artifact(art_hist.artifact_id, user_id=admin_uid)
        assert hist_check is not None and hist_check.shared_at is None

        # 3) Private app link (kind='app', port=3000, shared_at IS NULL)
        app_link = await share_links_db.ensure_app_link(ws_id, 3000)
        assert app_link.kind == "app"
        assert app_link.command_key is not None

        # Call list_shared and serialize via share_links_app._to_model (GET /workspaces/{id}/share-links)
        shared_links = await share_links_db.list_shared(ws_id)
        serialized = [share_links_app._to_model(item) for item in shared_links]
        returned_artifact_ids = [item.artifact_id for item in serialized]

        # Both ① normal shared file and ② historical public monitor are returned; ③ app link is excluded
        assert art_normal.artifact_id in returned_artifact_ids
        assert art_hist.artifact_id in returned_artifact_ids
        assert all(item.kind == "file" for item in serialized)

        # GET /api/v1/share-links/{code} (get_link) still returns the app link with command_key intact
        fetched_app = await share_links_db.get_link(app_link.code)
        assert fetched_app is not None
        app_model = share_links_app._to_model(fetched_app)
        assert app_model.kind == "app"
        assert app_model.port == 3000
        assert app_model.command_key == app_link.command_key

        # Unsharing historical public monitor ② downgrades its monitor to 'own' and removes it from list_shared
        await artifacts_db.unshare_artifact_and_downgrade_public_monitor(
            user_id=admin_uid,
            artifact_id=art_hist.artifact_id,
        )
        hist_mon_after = await monitor_db.get_monitor_by_slug(slug_hist)
        assert hist_mon_after is not None and hist_mon_after.visibility == "own"

        shared_links_after = await share_links_db.list_shared(ws_id)
        ids_after = [item.artifact_id for item in shared_links_after]
        assert art_hist.artifact_id not in ids_after
        assert art_normal.artifact_id in ids_after
    finally:
        await _cleanup_user_and_workspace(dalton_db_pool, admin_uid, ws_id)


# ---------------------------------------------------------------------------
# 5. R3: Stale-Public Takeover Rejection, Concurrent FOR SHARE Ordering, & POST Downgrade Guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_r3_stale_takeover_rejected_concurrent_for_share_no_deadlock_and_post_guard(
    dalton_db_pool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")

    # Use explicit IDs in reverse order (admin_z > owner_a) so locking exercises sorted order.
    run_tag = uuid.uuid4().hex[:8]
    owner_a, ws_a = await _seed_user_and_workspace(
        dalton_db_pool, user_id=f"emp-a-{run_tag}", tier="internal"
    )
    admin_z, ws_z = await _seed_user_and_workspace(
        dalton_db_pool, user_id=f"emp-z-{run_tag}", tier="admin"
    )
    slug = f"emp-r3-{run_tag}"
    try:
        artifact = await artifacts_db.ensure_artifact(ws_a, "report.html")
        mon, _ = await monitor_db.commit_monitor_publish_tx(
            slug=slug,
            artifact_id=artifact.artifact_id,
            workspace_owner_id=owner_a,
            monitor_owner_id=admin_z,
            title="Public Takeover Target",
            category="overview",
            sort_order=0,
            visibility="public",
            keep_updated=False,
            source_kind="workspace",
            source_ref=f"{ws_a}:report.html",
            member_user_ids=(),
            files=_html_version_file(b"<html>public-r3-v1</html>"),
        )
        # Wait: ensure monitor owner starts as owner_a ('public' published by admin, or transfer to owner_a)
        async with await psycopg.AsyncConnection.connect(dalton_db_pool, autocommit=True) as conn:
            await conn.execute(
                "UPDATE dalton_monitors SET owner_user_id = %s WHERE monitor_id = %s::uuid",
                (owner_a, mon.monitor_id),
            )

        # Hold uncommitted role updates on both owner_a and admin_z in ascending order while
        # admin_z republishes -> FOR SHARE must not deadlock against set_user_role-style ascending locks.
        conn_roles = await psycopg.AsyncConnection.connect(dalton_db_pool, autocommit=False)
        try:
            async with conn_roles.cursor() as cur_r:
                for uid in sorted((owner_a, admin_z)):
                    await cur_r.execute(
                        "UPDATE dalton_user_roles SET updated_at = NOW() WHERE user_id = %s",
                        (uid,),
                    )
                repub_done = asyncio.Event()

                async def _repub_task() -> Any:
                    try:
                        return await monitor_db.commit_monitor_version_tx(
                            monitor_id=mon.monitor_id,
                            artifact_id=artifact.artifact_id,
                            workspace_owner_id=owner_a,
                            files=_html_version_file(b"<html>public-r3-v2</html>"),
                            title=mon.title,
                            new_owner_user_id=admin_z,
                            caller_user_id=admin_z,
                        )
                    finally:
                        repub_done.set()

                t_repub = asyncio.create_task(_repub_task())
                await asyncio.sleep(0.15)
                assert not repub_done.is_set()
                await conn_roles.commit()

                updated_mon, _res, _deduped = await asyncio.wait_for(t_repub, timeout=5.0)
                assert updated_mon.owner_user_id == admin_z
                assert updated_mon.published_seq == 2
        finally:
            await conn_roles.close()

        # Re-assign monitor to owner_a and downgrade it to 'own', then verify stale admin_z takeover fails.
        async with await psycopg.AsyncConnection.connect(dalton_db_pool, autocommit=True) as conn:
            await conn.execute(
                "UPDATE dalton_monitors SET owner_user_id = %s WHERE monitor_id = %s::uuid",
                (owner_a, mon.monitor_id),
            )
        await monitor_service.update_monitor_settings(
            slug,
            UpdateMonitorRequest(visibility="own", keep_updated=False),
            caller_id=owner_a,
        )
        with pytest.raises(HTTPException) as exc_stale:
            await monitor_db.commit_monitor_version_tx(
                monitor_id=mon.monitor_id,
                artifact_id=artifact.artifact_id,
                workspace_owner_id=owner_a,
                files=_html_version_file(b"<html>stale-takeover</html>"),
                title=mon.title,
                new_owner_user_id=admin_z,
                caller_user_id=admin_z,
            )
        assert exc_stale.value.status_code == 403
        assert exc_stale.value.detail["code"] == "external_member_forbidden"
        loaded_after = await monitor_db.get_monitor_by_slug(slug)
        assert loaded_after is not None and loaded_after.owner_user_id == owner_a
        assert loaded_after.published_seq == 2

        # Promote monitor back to 'public', demote owner_a -> external, and verify
        # reject_external_non_own_existing=True (POST) blocks while False (PATCH) downgrades.
        await auth_roles.set_user_role(owner_a, "admin")
        await monitor_service.update_monitor_settings(
            slug,
            UpdateMonitorRequest(visibility="public", keep_updated=False),
            caller_id=owner_a,
        )
        await auth_roles.set_user_role(owner_a, "external")
        with pytest.raises(HTTPException) as exc_post_guard:
            await monitor_db.update_monitor_settings_tx(
                mon.monitor_id,
                artifact_id=artifact.artifact_id,
                owner_user_id=owner_a,
                caller_id=owner_a,
                visibility="own",
                keep_updated=True,
                reject_external_non_own_existing=True,
            )
        assert exc_post_guard.value.status_code == 403
        assert exc_post_guard.value.detail["code"] == "external_member_forbidden"

        downgraded_ok = await monitor_db.update_monitor_settings_tx(
            mon.monitor_id,
            artifact_id=artifact.artifact_id,
            owner_user_id=owner_a,
            caller_id=owner_a,
            visibility="own",
            keep_updated=False,
            reject_external_non_own_existing=False,
        )
        assert downgraded_ok.visibility == "own"
        assert downgraded_ok.published_seq == 2

        # R6 (Codex #1): Pause republish takeover immediately after its FIRST role row lock
        # (regardless of which user_id it locked), assert that first lock is `owner_a`
        # (`owner_a < admin_z`), start concurrent `set_user_role(owner_a, "external")`, confirm
        # via `pg_locks` (`granted = false` + `pg_blocking_pids`) that demotion is actively
        # waiting on takeover's connection, then release and confirm zero Postgres 40P01 deadlock.
        await auth_roles.set_user_role(owner_a, "admin")
        await auth_roles.set_user_role(admin_z, "admin")
        async with await psycopg.AsyncConnection.connect(dalton_db_pool, autocommit=True) as conn:
            await conn.execute(
                "UPDATE dalton_monitors SET owner_user_id = %s, visibility = 'public' WHERE monitor_id = %s::uuid",
                (owner_a, mon.monitor_id),
            )

        first_lock_evt = asyncio.Event()
        release_first_lock_evt = asyncio.Event()
        locked_order: list[str] = []
        takeover_pid_box: list[int] = []
        orig_resolve_live = monitor_db._resolve_live_tier_in_cur

        async def _sync_resolve_live(cur_obj: Any, uid: str) -> str:
            tier = await orig_resolve_live(cur_obj, uid)
            locked_order.append(uid)
            if len(locked_order) == 1:
                takeover_pid_box.append(int(cur_obj.connection.info.backend_pid))
                first_lock_evt.set()
                await asyncio.wait_for(release_first_lock_evt.wait(), timeout=10.0)
            return tier

        with patch.object(monitor_db, "_resolve_live_tier_in_cur", _sync_resolve_live):
            takeover_task = asyncio.create_task(
                monitor_db.commit_monitor_version_tx(
                    monitor_id=mon.monitor_id,
                    artifact_id=artifact.artifact_id,
                    workspace_owner_id=owner_a,
                    files=_html_version_file(b"<html>concurrent-r6</html>"),
                    title=mon.title,
                    new_owner_user_id=admin_z,
                    caller_user_id=admin_z,
                )
            )
            demote_task: asyncio.Task[Any] | None = None
            try:
                await asyncio.wait_for(first_lock_evt.wait(), timeout=5.0)
                assert locked_order == [owner_a], (
                    f"Expected takeover to lock owner_a ({owner_a}) first, got {locked_order}"
                )
                assert len(takeover_pid_box) == 1
                demote_task = asyncio.create_task(
                    auth_roles.set_user_role(owner_a, "external", granted_by=admin_z)
                )
                wait_row = await _wait_for_pg_lock_wait(
                    dalton_db_pool,
                    blocker_pid=takeover_pid_box[0],
                    waiter_task=demote_task,
                    timeout=5.0,
                )
                assert wait_row["granted"] is False
                assert not demote_task.done()
            finally:
                release_first_lock_evt.set()

            res_takeover = await asyncio.wait_for(takeover_task, timeout=10.0)
            assert demote_task is not None
            res_demote = await asyncio.wait_for(demote_task, timeout=10.0)

        assert locked_order == [owner_a, admin_z]
        assert res_takeover[0].owner_user_id == admin_z
        assert res_demote["tier"] == "external"

        # R6 (Codex #2, data_job public republish): exercise real `cut_monitor_version` public
        # republish calling real `_verify_data_job_public_approver(..., conn=conn)` (which does
        # NOT pre-lock user rows in `commit_monitor_version_tx`), confirm via `pg_locks`
        # (`granted = false` + `pg_blocking_pids`) that concurrent `set_user_role` is blocked on
        # the approver's FOR SHARE row lock, then verify post-demotion public republish and PATCH
        # both fail closed with `PublicAuthorityLostError`.
        await auth_roles.set_user_role(owner_a, "admin")
        from src.dalton.monitors import data_job_registry
        from src.dalton.monitors.data_job_registry import MonitorDataJobBinding

        company_ws_id = await monitor_db.ensure_company_monitor_workspace()
        job_art = await artifacts_db.ensure_artifact(
            company_ws_id, f"job-panel-{uuid.uuid4().hex[:8]}.html"
        )
        job_mon = None
        try:
            job_mon = await monitor_db.upsert_monitor(
                slug=f"job-conc-{uuid.uuid4().hex[:6]}",
                artifact_id=job_art.artifact_id,
                owner_user_id=COMPANY_SYSTEM_USER_ID,
                title="DataJob Concurrency",
                category="overview",
                sort_order=0,
                visibility="public",
                keep_updated=True,
                source_kind="data_job",
                source_ref=f"job-key-{uuid.uuid4().hex[:6]}",
                member_user_ids=(),
            )
            await monitor_db.commit_monitor_version_tx(
                monitor_id=job_mon.monitor_id,
                artifact_id=job_art.artifact_id,
                workspace_owner_id=COMPANY_SYSTEM_USER_ID,
                files=_html_version_file(b"<html>job-v1</html>", path=job_art.path),
                title="DataJob Concurrency",
                run_id=f"run-{uuid.uuid4().hex[:6]}",
            )
            binding = MonitorDataJobBinding(
                job_key=job_mon.source_ref or "",
                slug=job_mon.slug,
                title=job_mon.title,
                category="overview",
                visibility="public",
                rel_dir=f"research-outputs/{job_mon.slug}",
                approved_by_admin_user_id=owner_a,
            )
            with patch.dict(
                data_job_registry.MONITOR_DATA_JOB_REGISTRY,
                {binding.job_key: binding},
            ):
                job_locked_evt = asyncio.Event()
                job_release_evt = asyncio.Event()
                repub_pid_box: list[int] = []

                async def _verify_job_public_with_hold(
                    _owner_id: str, *, conn: Any = None
                ) -> None:
                    await monitor_service._verify_data_job_public_approver(
                        binding, is_existing_public=True, conn=conn
                    )
                    assert conn is not None
                    repub_pid_box.append(int(conn.info.backend_pid))
                    job_locked_evt.set()
                    await asyncio.wait_for(job_release_evt.wait(), timeout=10.0)

                repub_task = asyncio.create_task(
                    monitor_service.cut_monitor_version(
                        monitor=job_mon,
                        owner_user_id=COMPANY_SYSTEM_USER_ID,
                        files=_html_version_file(b"<html>job-v2</html>", path=job_art.path),
                        title=job_mon.title,
                        origin="owner",
                        run_id=f"run-{uuid.uuid4().hex[:6]}",
                        verify_public_authority=_verify_job_public_with_hold,
                    )
                )
                demote_approver_task: asyncio.Task[Any] | None = None
                try:
                    await asyncio.wait_for(job_locked_evt.wait(), timeout=5.0)
                    assert len(repub_pid_box) == 1
                    demote_approver_task = asyncio.create_task(
                        auth_roles.set_user_role(owner_a, "external", granted_by=admin_z)
                    )
                    job_wait_row = await _wait_for_pg_lock_wait(
                        dalton_db_pool,
                        blocker_pid=repub_pid_box[0],
                        waiter_task=demote_approver_task,
                        timeout=5.0,
                    )
                    assert job_wait_row["granted"] is False
                    assert not demote_approver_task.done()
                finally:
                    job_release_evt.set()

                repub_mon, _repub_res, repub_deduped = await asyncio.wait_for(
                    repub_task, timeout=10.0
                )
                assert demote_approver_task is not None
                demote_approver_res = await asyncio.wait_for(
                    demote_approver_task, timeout=10.0
                )
                assert repub_deduped is False
                assert repub_mon.visibility == "public"
                assert repub_mon.published_seq == 2
                assert demote_approver_res["tier"] == "external"

                # After demotion committed, subsequent public republish and PATCH must fail closed.
                async def _verify_job_public_in_tx(_u: str, *, conn: Any = None) -> None:
                    await monitor_service._verify_data_job_public_approver(
                        binding, is_existing_public=True, conn=conn
                    )

                with pytest.raises(monitor_service.PublicAuthorityLostError):
                    await monitor_service.cut_monitor_version(
                        monitor=repub_mon,
                        owner_user_id=COMPANY_SYSTEM_USER_ID,
                        files=_html_version_file(b"<html>job-v3</html>", path=job_art.path),
                        title=repub_mon.title,
                        origin="owner",
                        run_id=f"run-{uuid.uuid4().hex[:6]}",
                        verify_public_authority=_verify_job_public_in_tx,
                    )

                with pytest.raises(monitor_service.PublicAuthorityLostError):
                    await monitor_db.update_monitor_settings_tx(
                        job_mon.monitor_id,
                        artifact_id=job_art.artifact_id,
                        owner_user_id=COMPANY_SYSTEM_USER_ID,
                        caller_id=admin_z,
                        visibility="public",
                        verify_public_authority=_verify_job_public_in_tx,
                        resolve_caller_tier=monitor_db.get_user_tier,
                        public_approver_id=owner_a,
                    )
        finally:
            async with await psycopg.AsyncConnection.connect(dalton_db_pool, autocommit=True) as clean_conn:
                if job_mon is not None:
                    await clean_conn.execute(
                        "DELETE FROM dalton_monitors WHERE monitor_id = %s::uuid",
                        (job_mon.monitor_id,),
                    )
                await clean_conn.execute(
                    "DELETE FROM share_links WHERE artifact_id = %s::uuid",
                    (job_art.artifact_id,),
                )
    finally:
        await _cleanup_user_and_workspace(dalton_db_pool, owner_a, ws_a)
        await _cleanup_user_and_workspace(dalton_db_pool, admin_z, ws_z)
