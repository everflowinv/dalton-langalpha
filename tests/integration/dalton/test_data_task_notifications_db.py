"""Real PostgreSQL + local HTTP integration tests for Dalton data-task report notifications.

Reads the isolated test database DSN from ``DALTON_CHANNEL_GATEWAY_TEST_DSN``
(or ``DALTON_TEST_DB_DSN`` / ``TEST_DATABASE_URL``), requiring a ``_test`` database name.
Exercises real ``dalton_data_task_defs``, ``dalton_data_task_runs``, ``dalton_notify_deliveries``,
``dalton_channel_identities``, ``dalton_channel_user_settings``, ``dalton_channel_chat_bindings``,
and ``dalton_channel_deliveries`` tables plus the internal FastAPI router.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
from urllib.parse import urlsplit
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest
import pytest_asyncio

from src.dalton import access_tiers as access_tiers_mod
from src.dalton.app import data_task_notifications_router as dtn_router_mod
from src.dalton.app.router import router as dalton_router
from src.dalton.data_jobs.container_runner import DataTaskPathMap, build_path_map
from src.dalton.data_jobs.manifest import load_data_task_manifest, sync_manifest_to_db
from src.dalton.data_jobs.scheduler import (
    dispatch_pending_data_task_notifications,
    open_dedicated_lock_connection,
    try_scheduler_lock,
)
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database import data_task_notifications as dtn_db
from src.dalton.llm import model_prefs as model_prefs_mod
from src.dalton.notify import channel_transport as ct_mod
from src.dalton.notify import dispatch as notify_dispatch_mod
from src.server.database.user import upsert_user_preferences

pytestmark = pytest.mark.integration

SERVICE_TOKEN = "s" * 44

_DSN_ENV_VARS = (
    "DALTON_CHANNEL_GATEWAY_TEST_DSN",
    "DALTON_TEST_DB_DSN",
    "TEST_DATABASE_URL",
)


@pytest.fixture(scope="session", autouse=True)
def dalton_schema():
    yield


@pytest.fixture(autouse=True)
def cleanup_dalton_tables():
    yield


def _get_test_dsn() -> str:
    for var in _DSN_ENV_VARS:
        val = os.environ.get(var, "").strip()
        if val:
            db_name = urlsplit(val).path.lstrip("/")
            if not db_name.endswith("_test"):
                pytest.fail(
                    f"Refusing to run integration tests against non-test DB {db_name!r} (from {var})"
                )
            return val

    host = os.getenv("TEST_DB_HOST", "localhost")
    port = os.getenv("TEST_DB_PORT", "5432")
    name = (os.getenv("TEST_DB_NAME", "langalpha_test") or "").strip().lstrip("/")
    if not name.endswith("_test"):
        pytest.fail(
            f"Refusing to run integration tests against non-test DB {name!r}"
        )
    user = os.getenv("TEST_DB_USER", "postgres")
    password = os.getenv("TEST_DB_PASSWORD", "postgres")
    sslmode = os.getenv("TEST_DB_SSLMODE", "prefer")
    fallback_dsn = f"postgresql://{user}:{password}@{host}:{port}/{name}?sslmode={sslmode}"
    try:
        with psycopg.connect(fallback_dsn, connect_timeout=2):
            return fallback_dsn
    except psycopg.OperationalError as exc:
        pytest.skip(
            f"None of {_DSN_ENV_VARS} is set and default test DB is unreachable ({exc})"
        )


def _run_alembic_upgrade_sync(cfg: Config, revision: str) -> None:
    command.upgrade(cfg, revision)


async def _ensure_alembic_upgraded(dsn: str) -> None:
    project_root = Path(__file__).resolve().parents[3]
    sa_url = dsn.replace("postgresql://", "postgresql+psycopg://", 1).replace("%", "%%")

    upstream_cfg = Config(str(project_root / "alembic.ini"))
    upstream_cfg.set_main_option("script_location", str(project_root / "migrations"))
    upstream_cfg.set_main_option("sqlalchemy.url", sa_url)
    upstream_heads = set(ScriptDirectory.from_config(upstream_cfg).get_heads())

    dalton_cfg = Config(str(project_root / "src" / "dalton" / "alembic.ini"))
    dalton_cfg.set_main_option(
        "script_location", str(project_root / "src" / "dalton" / "migrations")
    )
    dalton_cfg.set_main_option("sqlalchemy.url", sa_url)
    dalton_heads = set(ScriptDirectory.from_config(dalton_cfg).get_heads())

    current_up: set[str] = set()
    current_dalton: set[str] = set()
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT to_regclass('public.alembic_version')")
            r = await cur.fetchone()
            if r and r[0] is not None:
                await cur.execute("SELECT version_num FROM alembic_version")
                current_up = {str(row[0]) for row in await cur.fetchall()}

            await cur.execute("SELECT to_regclass('public.dalton_alembic_version')")
            dr = await cur.fetchone()
            if dr and dr[0] is not None:
                await cur.execute("SELECT version_num FROM dalton_alembic_version")
                current_dalton = {str(row[0]) for row in await cur.fetchall()}

    if current_up != upstream_heads:
        await asyncio.to_thread(_run_alembic_upgrade_sync, upstream_cfg, "heads")
    if not dalton_heads.issubset(current_dalton):
        await asyncio.to_thread(_run_alembic_upgrade_sync, dalton_cfg, "heads")


@pytest_asyncio.fixture(scope="session")
async def migrated_dsn() -> str:
    dsn = _get_test_dsn()
    os.environ.setdefault("BYOK_ENCRYPTION_KEY", "test-data-task-notify-enc-key-32b")
    await _ensure_alembic_upgraded(dsn)
    return dsn


def _make_png(label: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + label.encode("ascii") + b"\x00" * 24


@pytest.mark.asyncio
async def test_authorization_gates_foreign_openid_protection_and_bounded_artifact_endpoint(
    migrated_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirements 1 & 5 against real PostgreSQL + ASGI HTTP:
    - Refuses unauthorized token, preview_only run, disabled task, disabled notify, master switch off,
      removed route, unbound Dalton Feishu identity (never reusing foreign app open_id), cross-user open_id,
      unbound group, and cross-run artifact fetch;
    - Resolves legacy single Feishu personal target strictly through `dalton_channel_identities`.
    """
    dsn = migrated_dsn
    pm_uid = f"pm-dtn-{uuid4().hex[:8]}"
    colleague_uid = f"col-dtn-{uuid4().hex[:8]}"
    prod_run_id = str(uuid4())
    preview_run_id = str(uuid4())
    other_run_id = str(uuid4())

    path_map: DataTaskPathMap = build_path_map(
        host_var_dir=tmp_path / "host_var",
        backend_var_dir=tmp_path / "backend_var",
    )

    chart_bytes = _make_png("SYNTH-SB-NAV")
    expected_sha = hashlib.sha256(chart_bytes).hexdigest()

    for rid in (prod_run_id, preview_run_id, other_run_id):
        rdir = path_map.backend_run_artifacts_dir(rid)
        (rdir / "images").mkdir(parents=True, exist_ok=True)
        b_data = chart_bytes if rid != other_run_id else _make_png("OTHER-RUN-IMG")
        (rdir / "images" / "c0.png").write_bytes(b_data)
        (rdir / "outbox.json").write_text(
            json.dumps(
                {
                    "feishu_cards": [
                        {
                            "card": {
                                "schema": "2.0",
                                "header": {"title": {"tag": "plain_text", "content": "SoftBank NAV"}},
                                "body": {
                                    "elements": [
                                        {"tag": "markdown", "content": "Synthetic body"},
                                        {"tag": "__img__", "path": "ph_0"},
                                    ]
                                },
                            },
                            "images": [{"placeholder": "ph_0", "path": "images/c0.png"}],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    @asynccontextmanager
    async def _real_db_conn(conn: Any = None):
        if conn is not None:
            yield conn
            return
        c = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        try:
            yield c
        finally:
            await c.close()

    async def _real_get_prefs(user_id: str) -> dict[str, Any] | None:
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as c:
            async with c.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT model_preference, other_preference FROM user_preferences WHERE user_id = %s",
                    (user_id,),
                )
                return await cur.fetchone()

    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("DALTON_PM_USER_IDS", pm_uid)
    monkeypatch.setattr(
        dtn_db,
        "get_db_connection",
        create_autospec(dtn_db.get_db_connection, side_effect=_real_db_conn),
    )
    monkeypatch.setattr(
        cg_db,
        "get_db_connection",
        create_autospec(cg_db.get_db_connection, side_effect=_real_db_conn),
    )
    monkeypatch.setattr(
        dtn_db,
        "build_path_map",
        create_autospec(dtn_db.build_path_map, return_value=path_map),
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "get_user_preferences",
        create_autospec(model_prefs_mod.get_user_preferences, side_effect=_real_get_prefs),
    )
    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(access_tiers_mod.get_user_tier, return_value="admin"),
    )
    monkeypatch.setattr(
        cg_db,
        "get_user_tier",
        create_autospec(cg_db.get_user_tier, return_value="admin"),
    )

    admin_conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        manifest = load_data_task_manifest()
        async with admin_conn.transaction():
            await sync_manifest_to_db(admin_conn, manifest)
            async with admin_conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_defs
                    SET enabled = TRUE, notify_enabled = TRUE
                    WHERE job_key = 'softbank-nav-monitor'
                    """
                )
                for uid in (pm_uid, colleague_uid):
                    await cur.execute(
                        """
                        INSERT INTO users (user_id, email, name, created_at, updated_at)
                        VALUES (%s, %s, %s, NOW(), NOW())
                        ON CONFLICT (user_id) DO NOTHING
                        """,
                        (uid, f"{uid}@synth.local", uid),
                    )
                slot = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)
                await cur.execute(
                    """
                    INSERT INTO dalton_data_task_runs (
                        run_id, job_key, trigger_source, scheduled_slot_utc,
                        deadline_utc, status, notify_status, completed_at
                    ) VALUES
                    (%s::uuid, 'softbank-nav-monitor', 'cron', %s, %s, 'succeeded', 'pending_dispatch', NOW()),
                    (%s::uuid, 'softbank-nav-monitor', 'manual', %s, %s, 'succeeded', 'preview_only', NOW()),
                    (%s::uuid, 'softbank-nav-monitor', 'cron', %s, %s, 'succeeded', 'pending_dispatch', NOW())
                    """,
                    (
                        prod_run_id,
                        slot,
                        slot + timedelta(minutes=10),
                        preview_run_id,
                        slot + timedelta(hours=1),
                        slot + timedelta(hours=1, minutes=10),
                        other_run_id,
                        slot + timedelta(hours=2),
                        slot + timedelta(hours=2, minutes=10),
                    ),
                )
            await upsert_user_preferences(
                user_id=pm_uid,
                model_preference={
                    "dalton_notify_enabled": True,
                    "dalton_notify_targets": ["feishu:user:ou_foreign_app_legacy"],
                    "dalton_notify_routes": {
                        "data:softbank-nav-monitor": [
                            "feishu:user:ou_foreign_app_legacy",
                            "feishu:chat:oc_synth_group_1",
                        ]
                    },
                },
                conn=admin_conn,
            )

        app = FastAPI()
        app.include_router(dalton_router)
        transport = httpx.ASGITransport(app=app)
        headers = {"X-Service-Token": SERVICE_TOKEN}

        async with httpx.AsyncClient(transport=transport, base_url="http://backend.local") as client:
            # 1. Preview run is refused (never becomes live send)
            r_prev = await client.post(
                "/api/v1/dalton/channels/internal/data-task-notifications/plan",
                headers=headers,
                json={"run_id": preview_run_id, "target": "feishu:user:ou_foreign_app_legacy"},
            )
            assert r_prev.status_code == 200
            assert r_prev.json()["authorized"] is False
            assert r_prev.json()["code"] == "preview_run_forbidden"

            r_prev_art = await client.get(
                f"/api/v1/dalton/channels/internal/data-task-notifications/runs/{preview_run_id}/artifacts/card0-img0",
                headers=headers,
            )
            assert r_prev_art.status_code == 403

            # 2. Unbound Dalton Feishu identity -> refused `not_linked` (never reuses ou_foreign_app_legacy!)
            r_unlinked = await client.post(
                "/api/v1/dalton/channels/internal/data-task-notifications/plan",
                headers=headers,
                json={"run_id": prod_run_id, "target": "feishu:user:ou_foreign_app_legacy"},
            )
            assert r_unlinked.json()["authorized"] is False
            assert r_unlinked.json()["code"] == "not_linked"

            # 3. Bind PM to dedicated Dalton Feishu app (`ou_dalton_dedicated_pm`) and colleague to `ou_colleague_dalton`
            async with admin_conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_identities (user_id, platform, platform_user_id)
                    VALUES (%s, 'feishu', 'ou_dalton_dedicated_pm'),
                           (%s, 'feishu', 'ou_colleague_dalton')
                    """,
                    (pm_uid, colleague_uid),
                )

            # Legacy single personal target resolves recipient strictly to `ou_dalton_dedicated_pm`, NEVER `ou_foreign_app_legacy`
            r_remapped = await client.post(
                "/api/v1/dalton/channels/internal/data-task-notifications/plan",
                headers=headers,
                json={"run_id": prod_run_id, "target": "feishu:user:ou_foreign_app_legacy"},
            )
            remapped_body = r_remapped.json()
            assert remapped_body["authorized"] is True
            assert remapped_body["recipient_id"] == "ou_dalton_dedicated_pm"
            assert remapped_body["canonical_target_address"] == "feishu:@me"
            assert "ou_foreign_app_legacy" not in json.dumps(remapped_body["items"])

            # Cross-user open_id (`ou_colleague_dalton`) is refused even if added to routes
            await upsert_user_preferences(
                user_id=pm_uid,
                model_preference={
                    "dalton_notify_enabled": True,
                    "dalton_notify_targets": ["feishu:user:ou_colleague_dalton"],
                    "dalton_notify_routes": {
                        "data:softbank-nav-monitor": ["feishu:user:ou_colleague_dalton"]
                    },
                },
                conn=admin_conn,
            )
            r_cross = await client.post(
                "/api/v1/dalton/channels/internal/data-task-notifications/plan",
                headers=headers,
                json={"run_id": prod_run_id, "target": "feishu:user:ou_colleague_dalton"},
            )
            assert r_cross.json()["authorized"] is False
            assert r_cross.json()["code"] == "cross_user_target"

            # Restore PM target + route and verify unbound group is refused until bound in Dalton channel settings
            await upsert_user_preferences(
                user_id=pm_uid,
                model_preference={
                    "dalton_notify_enabled": True,
                    "dalton_notify_targets": ["feishu:user:ou_dalton_dedicated_pm"],
                    "dalton_notify_routes": {
                        "data:softbank-nav-monitor": [
                            "feishu:user:ou_dalton_dedicated_pm",
                            "feishu:chat:oc_synth_group_1",
                        ]
                    },
                },
                conn=admin_conn,
            )
            r_grp_unbound = await client.post(
                "/api/v1/dalton/channels/internal/data-task-notifications/plan",
                headers=headers,
                json={"run_id": prod_run_id, "target": "feishu:chat:oc_synth_group_1"},
            )
            assert r_grp_unbound.json()["authorized"] is False
            assert r_grp_unbound.json()["code"] == "not_allowed"

            # Bind group in `dalton_channel_chat_bindings` and `dalton_channel_user_settings` -> authorized!
            async with admin_conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_chat_bindings (platform, chat_address, chat_name, bound_by_user_id)
                    VALUES ('feishu', 'feishu:oc_synth_group_1', 'Synth Group', %s)
                    """,
                    (pm_uid,),
                )
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_user_settings (user_id, apps)
                    VALUES (%s, %s)
                    """,
                    (
                        pm_uid,
                        Jsonb(
                            {
                                "feishu": {
                                    "chats": {
                                        "feishu:oc_synth_group_1": {"name": "Synth Group"}
                                    }
                                }
                            }
                        ),
                    ),
                )

            r_grp_ok = await client.post(
                "/api/v1/dalton/channels/internal/data-task-notifications/plan",
                headers=headers,
                json={"run_id": prod_run_id, "target": "feishu:chat:oc_synth_group_1"},
            )
            assert r_grp_ok.json()["authorized"] is True
            assert r_grp_ok.json()["recipient_id"] == "oc_synth_group_1"
            assert r_grp_ok.json()["canonical_target_address"] == "feishu:oc_synth_group_1"

            # Bounded artifact read returns exact bytes, MIME, filename, and SHA-256
            r_art = await client.get(
                f"/api/v1/dalton/channels/internal/data-task-notifications/runs/{prod_run_id}/artifacts/card0-img0",
                headers=headers,
            )
            assert r_art.status_code == 200
            assert r_art.content == chart_bytes
            assert r_art.headers["content-type"] == "image/png"
            assert r_art.headers["x-artifact-sha256"] == expected_sha

            # Removing route or disabling task/notifications immediately denies authorization
            await upsert_user_preferences(
                user_id=pm_uid,
                model_preference={
                    "dalton_notify_enabled": True,
                    "dalton_notify_targets": ["feishu:user:ou_dalton_dedicated_pm"],
                    "dalton_notify_routes": {
                        "data:softbank-nav-monitor": ["feishu:user:ou_dalton_dedicated_pm"]
                    },
                },
                conn=admin_conn,
            )
            r_removed = await client.post(
                "/api/v1/dalton/channels/internal/data-task-notifications/plan",
                headers=headers,
                json={"run_id": prod_run_id, "target": "feishu:chat:oc_synth_group_1"},
            )
            assert r_removed.json()["authorized"] is False
            assert r_removed.json()["code"] == "target_not_routed"

            async with admin_conn.cursor() as cur:
                await cur.execute(
                    "UPDATE dalton_data_task_defs SET notify_enabled = FALSE WHERE job_key = 'softbank-nav-monitor'"
                )
            r_notify_off = await client.post(
                "/api/v1/dalton/channels/internal/data-task-notifications/plan",
                headers=headers,
                json={"run_id": prod_run_id, "target": "feishu:user:ou_dalton_dedicated_pm"},
            )
            assert r_notify_off.json()["authorized"] is False
            assert r_notify_off.json()["code"] == "task_notify_disabled"

            async with admin_conn.cursor() as cur:
                await cur.execute(
                    "UPDATE dalton_data_task_defs SET enabled = FALSE, notify_enabled = TRUE WHERE job_key = 'softbank-nav-monitor'"
                )
            r_task_off = await client.post(
                "/api/v1/dalton/channels/internal/data-task-notifications/plan",
                headers=headers,
                json={"run_id": prod_run_id, "target": "feishu:user:ou_dalton_dedicated_pm"},
            )
            assert r_task_off.json()["authorized"] is False
            assert r_task_off.json()["code"] == "task_disabled"
    finally:
        async with admin_conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM dalton_channel_deliveries WHERE run_id IN (%s, %s, %s)",
                (prod_run_id, preview_run_id, other_run_id),
            )
            await cur.execute(
                "DELETE FROM dalton_channel_chat_bindings WHERE chat_address = 'feishu:oc_synth_group_1'"
            )
            await cur.execute(
                "DELETE FROM dalton_channel_user_settings WHERE user_id IN (%s, %s)",
                (pm_uid, colleague_uid),
            )
            await cur.execute(
                "DELETE FROM dalton_channel_identities WHERE user_id IN (%s, %s)",
                (pm_uid, colleague_uid),
            )
            await cur.execute(
                "DELETE FROM dalton_data_task_runs WHERE run_id IN (%s::uuid, %s::uuid, %s::uuid)",
                (prod_run_id, preview_run_id, other_run_id),
            )
            await cur.execute(
                "DELETE FROM dalton_data_task_defs WHERE job_key = 'softbank-nav-monitor'"
            )
            await cur.execute(
                "DELETE FROM user_preferences WHERE user_id IN (%s, %s)",
                (pm_uid, colleague_uid),
            )
            await cur.execute(
                "DELETE FROM users WHERE user_id IN (%s, %s)",
                (pm_uid, colleague_uid),
            )
        await admin_conn.close()


@pytest.mark.asyncio
async def test_concurrent_claim_multi_item_resume_and_uncertain_ledger_protection_db(
    migrated_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirements 6 & 7 against real PostgreSQL:
    - Concurrent duplicate claims on `dalton_channel_deliveries` grant `claimed` to exactly one caller;
    - Multi-item card delivery resumes from item 1 when item 0 is `sent` and item 1 failed pre-send upload;
    - `unknown` / `in_flight` items are never re-claimed, and `retry_failed_notify_deliveries` never converts
      an uncertain `pending` row (`delivery_uncertain`, `attempt_count >= 3`) to `failed`.
    """
    dsn = migrated_dsn
    pm_uid = f"pm-ledger-{uuid4().hex[:8]}"
    run_id_resume = str(uuid4())
    run_id_uncertain = str(uuid4())

    path_map = build_path_map(
        host_var_dir=tmp_path / "h_var",
        backend_var_dir=tmp_path / "b_var",
    )
    for rid in (run_id_resume, run_id_uncertain):
        rdir = path_map.backend_run_artifacts_dir(rid)
        (rdir / "images").mkdir(parents=True, exist_ok=True)
        (rdir / "images" / "0.png").write_bytes(_make_png("CARD0"))
        (rdir / "images" / "1.png").write_bytes(_make_png("CARD1"))
        (rdir / "outbox.json").write_text(
            json.dumps(
                {
                    "feishu_cards": [
                        {
                            "card": {"schema": "2.0", "body": {"elements": [{"tag": "__img__", "path": "p0"}]}},
                            "images": [{"placeholder": "p0", "path": "images/0.png"}],
                        },
                        {
                            "card": {"schema": "2.0", "body": {"elements": [{"tag": "__img__", "path": "p1"}]}},
                            "images": [{"placeholder": "p1", "path": "images/1.png"}],
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

    @asynccontextmanager
    async def _real_db_conn(conn: Any = None):
        if conn is not None:
            yield conn
            return
        c = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        try:
            yield c
        finally:
            await c.close()

    async def _real_get_prefs(user_id: str) -> dict[str, Any] | None:
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as c:
            async with c.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT model_preference, other_preference FROM user_preferences WHERE user_id = %s",
                    (user_id,),
                )
                return await cur.fetchone()

    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("DALTON_PM_USER_IDS", pm_uid)
    monkeypatch.setattr(dtn_router_mod, "load_authorized_data_task_artifact", dtn_db.load_authorized_data_task_artifact)
    for mod in (dtn_db, cg_db, notify_dispatch_mod):
        monkeypatch.setattr(
            mod,
            "get_db_connection",
            create_autospec(mod.get_db_connection, side_effect=_real_db_conn),
        )
    monkeypatch.setattr(
        dtn_db,
        "build_path_map",
        create_autospec(dtn_db.build_path_map, return_value=path_map),
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "get_user_preferences",
        create_autospec(model_prefs_mod.get_user_preferences, side_effect=_real_get_prefs),
    )
    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(access_tiers_mod.get_user_tier, return_value="admin"),
    )

    admin_conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    lock_conn = await open_dedicated_lock_connection(dsn)
    try:
        assert await try_scheduler_lock(lock_conn) is True
        manifest = load_data_task_manifest()
        async with admin_conn.transaction():
            await sync_manifest_to_db(admin_conn, manifest)
            async with admin_conn.cursor() as cur:
                await cur.execute(
                    "UPDATE dalton_data_task_defs SET enabled = TRUE, notify_enabled = TRUE WHERE job_key = 'tsmc-adr-premium'"
                )
                await cur.execute(
                    """
                    INSERT INTO users (user_id, email, name, created_at, updated_at)
                    VALUES (%s, %s, 'Ledger PM', NOW(), NOW())
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    (pm_uid, f"{pm_uid}@synth.local"),
                )
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_identities (user_id, platform, platform_user_id)
                    VALUES (%s, 'feishu', 'ou_dalton_ledger_pm')
                    """,
                    (pm_uid,),
                )
                slot = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
                for idx, rid in enumerate((run_id_resume, run_id_uncertain)):
                    await cur.execute(
                        """
                        INSERT INTO dalton_data_task_runs (
                            run_id, job_key, trigger_source, scheduled_slot_utc,
                            deadline_utc, status, notify_status, completed_at
                        ) VALUES (%s::uuid, 'tsmc-adr-premium', 'cron', %s, %s, 'succeeded', 'pending_dispatch', NOW())
                        """,
                        (
                            rid,
                            slot + timedelta(hours=idx),
                            slot + timedelta(hours=idx, minutes=10),
                        ),
                    )
            await upsert_user_preferences(
                user_id=pm_uid,
                model_preference={
                    "dalton_notify_enabled": True,
                    "dalton_notify_targets": ["feishu:user:ou_dalton_ledger_pm"],
                    "dalton_notify_routes": {
                        "data:tsmc-adr-premium": ["feishu:user:ou_dalton_ledger_pm"]
                    },
                },
                conn=admin_conn,
            )

        plan_res = await dtn_db.authorize_data_task_notification_plan(
            run_id=run_id_resume,
            target="feishu:user:ou_dalton_ledger_pm",
            path_map=path_map,
        )
        assert plan_res["authorized"] is True
        key0 = plan_res["items"][0]["delivery_key"]
        key1 = plan_res["items"][1]["delivery_key"]

        # 1. Two concurrent I-6 claims on key0 serialize via advisory lock: one gets `claimed`, one gets `in_flight_or_unknown`
        c_a, c_b = await asyncio.gather(
            cg_db.authorize_and_claim_delivery(
                delivery_key=key0,
                kind="data_task",
                run_id=run_id_resume,
                target="feishu:user:ou_dalton_ledger_pm",
                platform="feishu",
                has_non_empty_text=True,
                total_chunks=1,
            ),
            cg_db.authorize_and_claim_delivery(
                delivery_key=key0,
                kind="data_task",
                run_id=run_id_resume,
                target="feishu:user:ou_dalton_ledger_pm",
                platform="feishu",
                has_non_empty_text=True,
                total_chunks=1,
            ),
        )
        assert sorted([c_a["claim_status"], c_b["claim_status"]]) == [
            "claimed",
            "in_flight_or_unknown",
        ]

        # Complete item 0 as `sent`, claim item 1 and fail pre-send upload (`status='failed'`)
        await cg_db.update_delivery_progress(
            delivery_key=key0,
            sent_chunks=1,
            total_chunks=1,
            platform_message_ids=["om_card_0"],
            status="sent",
        )
        c1_first = await cg_db.authorize_and_claim_delivery(
            delivery_key=key1,
            kind="data_task",
            run_id=run_id_resume,
            target="feishu:user:ou_dalton_ledger_pm",
            platform="feishu",
            has_non_empty_text=True,
            total_chunks=1,
        )
        assert c1_first["claim_status"] == "claimed"
        await cg_db.update_delivery_progress(
            delivery_key=key1,
            sent_chunks=0,
            total_chunks=1,
            status="failed",
            error_code="upload_failed",
        )

        # 2. Retry on run_id_resume: key0 returns `already_completed` (`sent`), key1 is re-claimable!
        c0_retry = await cg_db.authorize_and_claim_delivery(
            delivery_key=key0,
            kind="data_task",
            run_id=run_id_resume,
            target="feishu:user:ou_dalton_ledger_pm",
            platform="feishu",
            has_non_empty_text=True,
            total_chunks=1,
        )
        c1_retry = await cg_db.authorize_and_claim_delivery(
            delivery_key=key1,
            kind="data_task",
            run_id=run_id_resume,
            target="feishu:user:ou_dalton_ledger_pm",
            platform="feishu",
            has_non_empty_text=True,
            total_chunks=1,
        )
        assert c0_retry["claim_status"] == "already_completed"
        assert c0_retry["status"] == "sent"
        assert c1_retry["claim_status"] == "claimed"
        await cg_db.update_delivery_progress(
            delivery_key=key1,
            sent_chunks=1,
            total_chunks=1,
            platform_message_ids=["om_card_1"],
            status="sent",
        )

        # 3. Uncertain send on run_id_uncertain: gateway timeout leaves dalton_channel_deliveries `unknown`
        #    and dalton_notify_deliveries `pending` (`error_code='delivery_uncertain'`). Even at attempt_count=3
        #    and stale updated_at, `retry_failed_notify_deliveries` NEVER converts it to `failed`!
        plan_unc = await dtn_db.authorize_data_task_notification_plan(
            run_id=run_id_uncertain,
            target="feishu:user:ou_dalton_ledger_pm",
            path_map=path_map,
        )
        ukey0 = plan_unc["items"][0]["delivery_key"]
        uclaim = await cg_db.authorize_and_claim_delivery(
            delivery_key=ukey0,
            kind="data_task",
            run_id=run_id_uncertain,
            target="feishu:user:ou_dalton_ledger_pm",
            platform="feishu",
            has_non_empty_text=True,
            total_chunks=1,
        )
        assert uclaim["claim_status"] == "claimed"
        # Simulate gateway restart marking in_flight -> unknown
        await cg_db.recover_channel_state(mode="startup")

        uclaim_after_restart = await cg_db.authorize_and_claim_delivery(
            delivery_key=ukey0,
            kind="data_task",
            run_id=run_id_uncertain,
            target="feishu:user:ou_dalton_ledger_pm",
            platform="feishu",
            has_non_empty_text=True,
            total_chunks=1,
        )
        assert uclaim_after_restart["claim_status"] == "in_flight_or_unknown"
        assert uclaim_after_restart["status"] == "unknown"

        async def _fake_gw_transport(
            targets: Any,
            text: str = "",
            card: dict[str, Any] | None = None,
            *,
            images: list[dict[str, Any]] | None = None,
            run_id: str | None = None,
            gateway_url: str | None = None,
            service_token: str | None = None,
            timeout_s: float | None = None,
            http_client: httpx.AsyncClient | None = None,
        ) -> dict[str, Any]:
            _ = (text, card, images, gateway_url, service_token, timeout_s, http_client)
            t = targets[0]
            if run_id == run_id_uncertain:
                return {
                    "ok": False,
                    "status": "unknown",
                    "uncertain": True,
                    "status_code": 200,
                    "error_code": "in_flight",
                    "results": [{"target": t, "ok": False, "status": "unknown", "uncertain": True, "error_code": "in_flight"}],
                }
            return {
                "ok": True,
                "status": "sent",
                "uncertain": False,
                "status_code": 200,
                "results": [{"target": t, "ok": True, "status": "sent"}],
            }

        gw_double = create_autospec(ct_mod.post_data_task_gateway_notify, side_effect=_fake_gw_transport)
        outcomes = await dispatch_pending_data_task_notifications(
            admin_conn,
            lock_conn=lock_conn,
            path_map=path_map,
            post_notify_fn=gw_double,
        )
        assert dict(outcomes) == {
            run_id_resume: "dispatched",
            run_id_uncertain: "pending_dispatch",
        }

        # Age uncertain row past stale_pending_minutes and set attempt_count = 3 -> must NOT become `failed`!
        async with admin_conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_notify_deliveries
                SET attempt_count = 3,
                    updated_at = NOW() - INTERVAL '30 minutes'
                WHERE execution_id = %s
                """,
                (run_id_uncertain,),
            )

        await notify_dispatch_mod.retry_failed_notify_deliveries(
            data_task_notify_fn=gw_double,
            max_attempts=3,
            stale_pending_minutes=10,
        )

        async with admin_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT status, error_code
                FROM dalton_notify_deliveries
                WHERE execution_id = %s
                """,
                (run_id_uncertain,),
            )
            unc_notify_row = await cur.fetchone()
        assert unc_notify_row is not None
        assert unc_notify_row["status"] == "pending"
        assert unc_notify_row["error_code"] == "in_flight"
    finally:
        await lock_conn.close()
        async with admin_conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM dalton_notify_deliveries WHERE execution_id IN (%s, %s)",
                (run_id_resume, run_id_uncertain),
            )
            await cur.execute(
                "DELETE FROM dalton_channel_deliveries WHERE run_id IN (%s, %s)",
                (run_id_resume, run_id_uncertain),
            )
            await cur.execute(
                "DELETE FROM dalton_channel_identities WHERE user_id = %s",
                (pm_uid,),
            )
            await cur.execute(
                "DELETE FROM dalton_data_task_runs WHERE run_id IN (%s::uuid, %s::uuid)",
                (run_id_resume, run_id_uncertain),
            )
            await cur.execute(
                "DELETE FROM dalton_data_task_defs WHERE job_key = 'tsmc-adr-premium'"
            )
            await cur.execute(
                "DELETE FROM user_preferences WHERE user_id = %s",
                (pm_uid,),
            )
            await cur.execute(
                "DELETE FROM users WHERE user_id = %s",
                (pm_uid,),
            )
        await admin_conn.close()
