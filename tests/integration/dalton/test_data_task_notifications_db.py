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
from src.dalton.data_jobs.notification_payloads import prepare_run_outbox_image_metadata
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
        prepared_ob = prepare_run_outbox_image_metadata(
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
            },
            run_artifacts_dir=rdir,
            job_key="softbank-nav-monitor",
        )
        (rdir / "outbox.json").write_text(
            json.dumps(prepared_ob),
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
            assert "owner_platform_user_id" not in r_grp_ok.json()
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
        prepared_two_cards = prepare_run_outbox_image_metadata(
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
            },
            run_artifacts_dir=rdir,
            job_key="tsmc-adr-premium",
        )
        (rdir / "outbox.json").write_text(
            json.dumps(prepared_two_cards),
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


@pytest.mark.asyncio
async def test_unbound_admin_group_delivery_workspace_gate_and_real_vault_startup_db(
    migrated_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2 §1 & §2 against real PostgreSQL + ASGI HTTP:
    - Primary administrator with zero `dalton_channel_identities` and zero `dalton_channel_inbound_events`
      saves group settings via `update_user_channel_settings` (`I-5`), obtains an authorized group plan
      omitting `owner_platform_user_id`, and claims/completes `dalton_channel_deliveries` (`I-6`/`I-7`);
    - Personal DM targets for that unbound administrator remain denied (`not_linked`);
    - Group bound to an inaccessible workspace is denied (`not_allowed`);
    - Internal Vault startup endpoint decrypts only the primary administrator's two dedicated Feishu keys
      from real `user_vault_secrets` (`pgcrypto`) and reflects rotated values on subsequent startup reads.
    """
    from src.server.database import user_vault_secrets as vault_db

    dsn = migrated_dsn
    pm_uid = f"pm-r2-unbound-{uuid4().hex[:8]}"
    other_uid = f"other-r2-{uuid4().hex[:8]}"
    prod_run_id = str(uuid4())
    inaccessible_ws_id = str(uuid4())

    synth_app_id = "cli_synth_r2_pg_app"
    synth_secret_v1 = "sec_synth_r2_pg_secret_v1"
    synth_secret_v2 = "sec_synth_r2_pg_secret_v2"

    path_map = build_path_map(
        host_var_dir=tmp_path / "r2_h_var",
        backend_var_dir=tmp_path / "r2_b_var",
    )
    rdir = path_map.backend_run_artifacts_dir(prod_run_id)
    (rdir / "images").mkdir(parents=True, exist_ok=True)
    (rdir / "images" / "c0.png").write_bytes(_make_png("R2-GROUP-CARD"))
    prepared_r2_ob = prepare_run_outbox_image_metadata(
        {
            "feishu_cards": [
                {
                    "card": {
                        "schema": "2.0",
                        "body": {"elements": [{"tag": "__img__", "path": "ph0"}]},
                    },
                    "images": [{"placeholder": "ph0", "path": "images/c0.png"}],
                }
            ]
        },
        run_artifacts_dir=rdir,
        job_key="softbank-nav-monitor",
    )
    (rdir / "outbox.json").write_text(
        json.dumps(prepared_r2_ob),
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
    monkeypatch.setenv("DALTON_COLLEAGUE_DISPATCH_ENABLED", "false")
    for mod in (dtn_db, cg_db, vault_db):
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
                for uid in (pm_uid, other_uid):
                    await cur.execute(
                        """
                        INSERT INTO users (user_id, email, name, created_at, updated_at)
                        VALUES (%s, %s, %s, NOW(), NOW())
                        ON CONFLICT (user_id) DO NOTHING
                        """,
                        (uid, f"{uid}@synth.local", uid),
                    )
                await cur.execute(
                    """
                    INSERT INTO workspaces (workspace_id, user_id, name, status, created_at, updated_at)
                    VALUES (%s::uuid, %s, 'Other Private WS', 'stopped', NOW(), NOW())
                    """,
                    (inaccessible_ws_id, other_uid),
                )
                slot = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
                await cur.execute(
                    """
                    INSERT INTO dalton_data_task_runs (
                        run_id, job_key, trigger_source, scheduled_slot_utc,
                        deadline_utc, status, notify_status, completed_at
                    ) VALUES (
                        %s::uuid, 'softbank-nav-monitor', 'cron', %s, %s,
                        'succeeded', 'pending_dispatch', NOW()
                    )
                    """,
                    (prod_run_id, slot, slot + timedelta(minutes=10)),
                )
            await upsert_user_preferences(
                user_id=pm_uid,
                model_preference={
                    "dalton_notify_enabled": True,
                    "dalton_notify_targets": [],
                    "dalton_notify_routes": {
                        "data:softbank-nav-monitor": [
                            "feishu:chat:oc_synth_admin_only",
                            "feishu:chat:oc_synth_denied_ws",
                        ]
                    },
                },
                conn=admin_conn,
            )

        # Save explicit group settings as admin via `update_user_channel_settings` WITHOUT any personal binding or inbound event
        saved_settings = await cg_db.update_user_channel_settings(
            user_id=pm_uid,
            version="v1",
            settings={
                "feishu": {
                    "preferred": None,
                    "chats": {
                        "feishu:oc_synth_admin_only": {
                            "name": "Synth Admin Group",
                            "workspace_id": None,
                        }
                    },
                    "automation_output": {},
                    "agent_messages": {
                        "enabled": True,
                        "allowed": ["feishu:oc_synth_admin_only"],
                    },
                }
            },
        )
        assert saved_settings["version"] == "v2"

        # Insert a group bound to `other_uid`'s inaccessible workspace to verify workspace write check
        async with admin_conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_channel_chat_bindings (platform, chat_address, chat_name, workspace_id, bound_by_user_id)
                VALUES ('feishu', 'feishu:oc_synth_denied_ws', 'Denied WS Group', %s::uuid, %s)
                """,
                (inaccessible_ws_id, other_uid),
            )
            await cur.execute(
                """
                UPDATE dalton_channel_user_settings
                SET apps = jsonb_set(
                    apps,
                    '{feishu,chats,feishu:oc_synth_denied_ws}',
                    %s::jsonb
                )
                WHERE user_id = %s
                """,
                (
                    json.dumps({"name": "Denied WS Group", "workspace_id": inaccessible_ws_id}),
                    pm_uid,
                ),
            )

        app = FastAPI()
        app.include_router(dalton_router)
        transport = httpx.ASGITransport(app=app)
        headers = {"X-Service-Token": SERVICE_TOKEN}

        async with httpx.AsyncClient(transport=transport, base_url="http://backend.local") as client:
            # 1. Group target succeeds without personal binding; `owner_platform_user_id` is omitted
            r_grp = await client.post(
                "/api/v1/dalton/channels/internal/data-task-notifications/plan",
                headers=headers,
                json={"run_id": prod_run_id, "target": "feishu:chat:oc_synth_admin_only"},
            )
            assert r_grp.status_code == 200
            grp_data = r_grp.json()
            assert grp_data["authorized"] is True
            assert grp_data["owner_user_id"] == pm_uid
            assert "owner_platform_user_id" not in grp_data
            assert grp_data["target_kind"] == "group"
            assert grp_data["canonical_target_address"] == "feishu:oc_synth_admin_only"
            assert grp_data["recipient_id"] == "oc_synth_admin_only"
            d_key = grp_data["items"][0]["delivery_key"]
            assert d_key == f"data_task:{prod_run_id}:feishu:oc_synth_admin_only:item:0"

            # 2. Group bound to inaccessible workspace is denied (`not_allowed`)
            r_denied_ws = await client.post(
                "/api/v1/dalton/channels/internal/data-task-notifications/plan",
                headers=headers,
                json={"run_id": prod_run_id, "target": "feishu:chat:oc_synth_denied_ws"},
            )
            assert r_denied_ws.json()["authorized"] is False
            assert r_denied_ws.json()["code"] == "not_allowed"

            # 3. Personal DM target for the same unbound administrator remains denied (`not_linked`)
            await upsert_user_preferences(
                user_id=pm_uid,
                model_preference={
                    "dalton_notify_enabled": True,
                    "dalton_notify_targets": ["feishu:user:ou_synth_unbound"],
                    "dalton_notify_routes": {
                        "data:softbank-nav-monitor": [
                            "feishu:user:ou_synth_unbound",
                            "feishu:chat:oc_synth_admin_only",
                        ]
                    },
                },
                conn=admin_conn,
            )
            r_dm = await client.post(
                "/api/v1/dalton/channels/internal/data-task-notifications/plan",
                headers=headers,
                json={"run_id": prod_run_id, "target": "feishu:user:ou_synth_unbound"},
            )
            assert r_dm.json()["authorized"] is False
            assert r_dm.json()["code"] == "not_linked"

            # 4. I-6 claim + I-7 completion for the unbound admin's group target succeeds cleanly
            r_claim = await client.post(
                "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
                headers=headers,
                json={
                    "delivery_key": d_key,
                    "kind": "data_task",
                    "run_id": prod_run_id,
                    "target": "feishu:chat:oc_synth_admin_only",
                    "platform": "feishu",
                    "has_non_empty_text": True,
                    "total_chunks": 1,
                },
            )
            assert r_claim.status_code == 200
            assert r_claim.json()["claim_status"] == "claimed"
            assert r_claim.json()["recipient_id"] == "oc_synth_admin_only"
            assert r_claim.json()["target_address"] == "feishu:oc_synth_admin_only"

            r_prog = await client.post(
                "/api/v1/dalton/channels/internal/deliveries/progress",
                headers=headers,
                json={
                    "delivery_key": d_key,
                    "sent_chunks": 1,
                    "total_chunks": 1,
                    "platform_message_ids": ["om_synth_grp_msg_1"],
                    "status": "sent",
                },
            )
            assert r_prog.status_code == 200
            assert r_prog.json()["status"] == "sent"

            # Confirm zero personal identity rows and zero inbound events were ever created
            async with admin_conn.cursor() as cur:
                await cur.execute(
                    "SELECT COUNT(*) FROM dalton_channel_identities WHERE user_id = %s",
                    (pm_uid,),
                )
                assert (await cur.fetchone())[0] == 0
                await cur.execute(
                    "SELECT COUNT(*) FROM dalton_channel_inbound_events WHERE dalton_user_id = %s",
                    (pm_uid,),
                )
                assert (await cur.fetchone())[0] == 0

            # 5. Real PostgreSQL `user_vault_secrets` (`pgp_sym_encrypt` / `pgp_sym_decrypt`) startup fetch & rotation
            vault_ep = "/api/v1/dalton/channels/internal/data-task-notifications/feishu-credentials"
            r_vault_empty = await client.post(vault_ep, headers=headers, json={})
            assert r_vault_empty.status_code == 503
            assert r_vault_empty.headers.get("cache-control") == "no-store"
            assert r_vault_empty.json()["code"] == "vault_credentials_missing"
            assert r_vault_empty.json()["missing"] == [
                "DALTON_FEISHU_APP_ID",
                "DALTON_FEISHU_APP_SECRET",
            ]

            await vault_db.create_user_secret(pm_uid, "DALTON_FEISHU_APP_ID", synth_app_id)
            await vault_db.create_user_secret(pm_uid, "DALTON_FEISHU_APP_SECRET", synth_secret_v1)
            await vault_db.create_user_secret(pm_uid, "UNRELATED_VAULT_TOKEN", "unrelated_secret_val")
            await vault_db.create_user_secret(other_uid, "DALTON_FEISHU_APP_SECRET", "other_user_secret")

            r_vault_v1 = await client.post(vault_ep, headers=headers, json={})
            assert r_vault_v1.status_code == 200
            assert r_vault_v1.headers.get("cache-control") == "no-store"
            assert r_vault_v1.json() == {
                "credentials": {
                    "DALTON_FEISHU_APP_ID": synth_app_id,
                    "DALTON_FEISHU_APP_SECRET": synth_secret_v1,
                }
            }

            await vault_db.update_user_secret(
                pm_uid,
                "DALTON_FEISHU_APP_SECRET",
                value=synth_secret_v2,
            )
            r_vault_v2 = await client.post(vault_ep, headers=headers, json={})
            assert r_vault_v2.status_code == 200
            assert r_vault_v2.json()["credentials"]["DALTON_FEISHU_APP_SECRET"] == synth_secret_v2
    finally:
        async with admin_conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM user_vault_secrets WHERE user_id IN (%s, %s)",
                (pm_uid, other_uid),
            )
            await cur.execute(
                "DELETE FROM dalton_channel_deliveries WHERE run_id = %s",
                (prod_run_id,),
            )
            await cur.execute(
                "DELETE FROM dalton_channel_chat_bindings WHERE chat_address IN ('feishu:oc_synth_admin_only', 'feishu:oc_synth_denied_ws')"
            )
            await cur.execute(
                "DELETE FROM dalton_channel_user_settings WHERE user_id IN (%s, %s)",
                (pm_uid, other_uid),
            )
            await cur.execute(
                "DELETE FROM dalton_data_task_runs WHERE run_id = %s::uuid",
                (prod_run_id,),
            )
            await cur.execute(
                "DELETE FROM dalton_data_task_defs WHERE job_key = 'softbank-nav-monitor'"
            )
            await cur.execute(
                "DELETE FROM workspaces WHERE workspace_id = %s::uuid",
                (inaccessible_ws_id,),
            )
            await cur.execute(
                "DELETE FROM user_preferences WHERE user_id IN (%s, %s)",
                (pm_uid, other_uid),
            )
            await cur.execute(
                "DELETE FROM users WHERE user_id IN (%s, %s)",
                (pm_uid, other_uid),
            )
        await admin_conn.close()


@pytest.mark.asyncio
async def test_read_only_reconciliation_exhausted_uncertain_fairness_and_multi_item_matching_db(
    migrated_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4 §4 against real PostgreSQL:
    - Third-attempt (`attempt_count=3`) lost response (`pending`, `delivery_uncertain`) reconciles read-only to `sent`
      once all expected items in `dalton_channel_deliveries` are durably `sent`, without send/claim calls or attempt resets;
    - Mixed multi-item states (`sent` + `in_flight`/`unknown`/`partial`/missing) and mismatched recipient/user/run records
      never mark the outer delivery `sent`;
    - Six (`>= 5`) older exhausted uncertain entries do not starve a newer retryable `failed` entry (`limit=5`), while a
      late durable `sent` item update on one of those exhausted uncertain entries reconciles on the next pass;
    - Repeated and concurrent reconciliation (`asyncio.gather`) are idempotent, and DB read errors leave state intact.
    """
    dsn = migrated_dsn
    pm_uid = f"pm-r4-reconcile-{uuid4().hex[:8]}"
    other_uid = f"other-r4-reconcile-{uuid4().hex[:8]}"
    run_3rd = str(uuid4())
    run_conc = str(uuid4())
    run_other = str(uuid4())
    run_ambiguous = str(uuid4())
    retry_run_id = str(uuid4())
    ordinary_exhausted_exec_id = f"ord-exh-{uuid4().hex[:8]}"
    ordinary_retry_exec_id = f"ord-ret-{uuid4().hex[:8]}"
    exhausted_uncertain_runs = [str(uuid4()) for _ in range(6)]
    all_run_ids = [run_3rd, run_conc, run_other, run_ambiguous, retry_run_id, *exhausted_uncertain_runs]
    all_notify_exec_ids = [*all_run_ids, ordinary_exhausted_exec_id, ordinary_retry_exec_id]

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
        notify_dispatch_mod,
        "get_db_connection",
        create_autospec(notify_dispatch_mod.get_db_connection, side_effect=_real_db_conn),
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

    observed_send_run_ids: list[str] = []

    async def _fake_gw_send(
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
        observed_send_run_ids.append(str(run_id or ""))
        return {
            "ok": True,
            "status": "sent",
            "uncertain": False,
            "status_code": 200,
            "results": [{"target": targets[0], "ok": True, "status": "sent"}],
        }

    gw_spy = create_autospec(ct_mod.post_data_task_gateway_notify, side_effect=_fake_gw_send)
    claim_spy = create_autospec(
        cg_db.authorize_and_claim_delivery,
        wraps=cg_db.authorize_and_claim_delivery,
    )
    monkeypatch.setattr(cg_db, "authorize_and_claim_delivery", claim_spy)

    admin_conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        manifest = load_data_task_manifest()
        slot_base = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)
        two_card_payload = {
            "route_key": "data:tsmc-adr-premium",
            "automation_id": "data:tsmc-adr-premium",
            "job_key": "tsmc-adr-premium",
            "expected_item_count": 2,
            "reconcile_platform": "feishu",
            "reconcile_recipient_id": "ou_r4_pm",
            "reconcile_canonical_target": "feishu:user:ou_r4_pm",
            "feishu_cards": [
                {"card": {"schema": "2.0", "body": {"elements": [{"tag": "hr"}]}}},
                {"card": {"schema": "2.0", "body": {"elements": [{"tag": "hr"}]}}},
            ],
        }
        one_card_payload = {
            "route_key": "data:tsmc-adr-premium",
            "automation_id": "data:tsmc-adr-premium",
            "job_key": "tsmc-adr-premium",
            "expected_item_count": 1,
            "reconcile_platform": "feishu",
            "reconcile_recipient_id": "ou_r4_pm",
            "reconcile_canonical_target": "feishu:user:ou_r4_pm",
            "feishu_cards": [
                {"card": {"schema": "2.0", "body": {"elements": [{"tag": "hr"}]}}},
            ],
        }

        async with admin_conn.transaction():
            await sync_manifest_to_db(admin_conn, manifest)
            async with admin_conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_defs
                    SET enabled = TRUE, notify_enabled = TRUE
                    WHERE job_key = 'tsmc-adr-premium'
                    """
                )
                for uid in (pm_uid, other_uid):
                    await cur.execute(
                        """
                        INSERT INTO users (user_id, email, name, created_at, updated_at)
                        VALUES (%s, %s, %s, NOW(), NOW())
                        ON CONFLICT (user_id) DO NOTHING
                        """,
                        (uid, f"{uid}@synth.local", uid),
                    )
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_identities (user_id, platform, platform_user_id)
                    VALUES (%s, 'feishu', 'ou_r4_pm'),
                           (%s, 'feishu', 'ou_r4_other')
                    """,
                    (pm_uid, other_uid),
                )
                for idx, rid in enumerate(all_run_ids):
                    r_slot = slot_base + timedelta(minutes=idx * 5)
                    await cur.execute(
                        """
                        INSERT INTO dalton_data_task_runs (
                            run_id, job_key, trigger_source, scheduled_slot_utc,
                            deadline_utc, status, notify_status, completed_at
                        )
                        VALUES (%s::uuid, 'tsmc-adr-premium', 'cron', %s, %s, 'succeeded', 'pending_dispatch', NOW())
                        """,
                        (rid, r_slot, r_slot + timedelta(minutes=10)),
                    )

            await upsert_user_preferences(
                user_id=pm_uid,
                model_preference={
                    "dalton_notify_enabled": True,
                    "dalton_notify_targets": ["feishu:@me"],
                    "dalton_notify_routes": {"data:tsmc-adr-premium": ["feishu:@me"]},
                },
                conn=admin_conn,
            )

        # ---------------------------------------------------------------------
        # 1. Third-attempt lost response + mixed multi-item states + mismatched records + late completion
        # ---------------------------------------------------------------------
        k0_3rd = dtn_db.build_data_task_item_delivery_key(
            run_id=run_3rd,
            platform="feishu",
            recipient_id="ou_r4_pm",
            item_index=0,
        )
        k1_3rd = dtn_db.build_data_task_item_delivery_key(
            run_id=run_3rd,
            platform="feishu",
            recipient_id="ou_r4_pm",
            item_index=1,
        )
        async with admin_conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_notify_deliveries (
                    delivery_id, execution_id, automation_id, user_id, channel, target,
                    status, attempt_count, error_code, payload_json,
                    created_at, updated_at
                )
                VALUES (%s, %s, 'data:tsmc-adr-premium', %s, 'feishu', 'feishu:@me',
                        'pending', 3, 'delivery_uncertain', %s,
                        NOW() - INTERVAL '45 minutes', NOW() - INTERVAL '45 minutes')
                """,
                (
                    str(uuid4()),
                    run_3rd,
                    pm_uid,
                    Jsonb({**two_card_payload, "reconcile_delivery_keys": [k0_3rd, k1_3rd]}),
                ),
            )
            # Item 0 is `sent`; Item 1 is still `in_flight` (mixed multi-item state).
            # Also insert decoy `sent` rows for another run, another user, and another target!
            await cur.execute(
                """
                INSERT INTO dalton_channel_deliveries (
                    delivery_key, kind, user_id, run_id, execution_id,
                    is_current, has_non_empty_text, platform, target_address,
                    status, total_chunks, sent_chunks, created_at, updated_at
                )
                VALUES
                (%s, 'data_task', %s, %s, %s::uuid, FALSE, TRUE, 'feishu', 'feishu:@me', 'sent', 1, 1, NOW(), NOW()),
                (%s, 'data_task', %s, %s, %s::uuid, FALSE, TRUE, 'feishu', 'feishu:@me', 'in_flight', 1, 0, NOW(), NOW()),
                (%s, 'data_task', %s, %s, %s::uuid, FALSE, TRUE, 'feishu', 'feishu:@me', 'sent', 1, 1, NOW(), NOW()),
                (%s, 'data_task', %s, %s, %s::uuid, FALSE, TRUE, 'feishu', 'feishu:oc_wrong', 'sent', 1, 1, NOW(), NOW())
                """,
                (
                    k0_3rd,
                    pm_uid,
                    run_3rd,
                    run_3rd,
                    k1_3rd,
                    pm_uid,
                    run_3rd,
                    run_3rd,
                    dtn_db.build_data_task_item_delivery_key(
                        run_id=run_other,
                        platform="feishu",
                        recipient_id="ou_r4_pm",
                        item_index=1,
                    ),
                    pm_uid,
                    run_other,
                    run_other,
                    dtn_db.build_data_task_item_delivery_key(
                        run_id=run_3rd,
                        platform="feishu",
                        recipient_id="ou_r4_other",
                        item_index=1,
                    ),
                    other_uid,
                    run_3rd,
                    run_3rd,
                ),
            )

        for non_sent_status in ("in_flight", "unknown", "partial", "failed"):
            async with admin_conn.cursor() as cur:
                await cur.execute(
                    "UPDATE dalton_channel_deliveries SET status = %s WHERE delivery_key = %s",
                    (non_sent_status, k1_3rd),
                )
            await notify_dispatch_mod.retry_failed_notify_deliveries(
                limit=5,
                max_attempts=3,
                stale_pending_minutes=10,
                data_task_notify_fn=gw_spy,
            )
            async with admin_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT status, attempt_count, error_code FROM dalton_notify_deliveries WHERE execution_id = %s",
                    (run_3rd,),
                )
                r_mid = await cur.fetchone()
            assert r_mid == {
                "status": "pending",
                "attempt_count": 3,
                "error_code": "delivery_uncertain",
            }
            assert observed_send_run_ids == []
            assert claim_spy.call_count == 0

        # Missing second item record -> also remains unresolved
        async with admin_conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM dalton_channel_deliveries WHERE delivery_key = %s",
                (k1_3rd,),
            )
        await notify_dispatch_mod.retry_failed_notify_deliveries(
            limit=5,
            max_attempts=3,
            stale_pending_minutes=10,
            data_task_notify_fn=gw_spy,
        )
        async with admin_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status, attempt_count FROM dalton_notify_deliveries WHERE execution_id = %s",
                (run_3rd,),
            )
            assert await cur.fetchone() == {"status": "pending", "attempt_count": 3}

        # R5 Part A #2: User unlinks `ou_r4_pm`, rebinds to a different recipient `ou_rebound_new`, and removes route!
        async with admin_conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM dalton_channel_identities WHERE user_id = %s AND platform = 'feishu'",
                (pm_uid,),
            )
            await cur.execute(
                """
                INSERT INTO dalton_channel_identities (user_id, platform, platform_user_id)
                VALUES (%s, 'feishu', 'ou_rebound_new')
                """,
                (pm_uid,),
            )
            # Insert `sent` item 0 and item 1 for the NEW rebound recipient `ou_rebound_new` on `run_3rd`.
            # Reconciliation must NOT use today's `dalton_channel_identities` binding (`ou_rebound_new`)!
            k0_rebound = dtn_db.build_data_task_item_delivery_key(
                run_id=run_3rd,
                platform="feishu",
                recipient_id="ou_rebound_new",
                item_index=0,
            )
            k1_rebound = dtn_db.build_data_task_item_delivery_key(
                run_id=run_3rd,
                platform="feishu",
                recipient_id="ou_rebound_new",
                item_index=1,
            )
            await cur.execute(
                """
                INSERT INTO dalton_channel_deliveries (
                    delivery_key, kind, user_id, run_id, execution_id,
                    is_current, has_non_empty_text, platform, target_address,
                    status, total_chunks, sent_chunks, created_at, updated_at
                )
                VALUES
                (%s, 'data_task', %s, %s, %s::uuid, FALSE, TRUE, 'feishu', 'feishu:@me', 'sent', 1, 1, NOW(), NOW()),
                (%s, 'data_task', %s, %s, %s::uuid, FALSE, TRUE, 'feishu', 'feishu:@me', 'sent', 1, 1, NOW(), NOW())
                """,
                (k0_rebound, pm_uid, run_3rd, run_3rd, k1_rebound, pm_uid, run_3rd, run_3rd),
            )
            # Also seed an ambiguous legacy row (`run_ambiguous`, without `reconcile_recipient_id` in `payload_json`)
            # that has `sent` records for two conflicting recipients (`ou_r4_pm` and `ou_rebound_new`).
            legacy_ambig_payload = {
                "route_key": "data:tsmc-adr-premium",
                "automation_id": "data:tsmc-adr-premium",
                "job_key": "tsmc-adr-premium",
                "expected_item_count": 1,
                "feishu_cards": [{"card": {"schema": "2.0", "body": {"elements": [{"tag": "hr"}]}}}],
            }
            await cur.execute(
                """
                INSERT INTO dalton_notify_deliveries (
                    delivery_id, execution_id, automation_id, user_id, channel, target,
                    status, attempt_count, error_code, payload_json,
                    created_at, updated_at
                )
                VALUES (%s, %s, 'data:tsmc-adr-premium', %s, 'feishu', 'feishu:@me',
                        'pending', 3, 'delivery_uncertain', %s,
                        NOW() - INTERVAL '40 minutes', NOW() - INTERVAL '40 minutes')
                """,
                (str(uuid4()), run_ambiguous, pm_uid, Jsonb(legacy_ambig_payload)),
            )
            await cur.execute(
                """
                INSERT INTO dalton_channel_deliveries (
                    delivery_key, kind, user_id, run_id, execution_id,
                    is_current, has_non_empty_text, platform, target_address,
                    status, total_chunks, sent_chunks, created_at, updated_at
                )
                VALUES
                (%s, 'data_task', %s, %s, %s::uuid, FALSE, TRUE, 'feishu', 'feishu:@me', 'sent', 1, 1, NOW(), NOW()),
                (%s, 'data_task', %s, %s, %s::uuid, FALSE, TRUE, 'feishu', 'feishu:@me', 'sent', 1, 1, NOW(), NOW())
                """,
                (
                    dtn_db.build_data_task_item_delivery_key(
                        run_id=run_ambiguous, platform="feishu", recipient_id="ou_r4_pm", item_index=0
                    ),
                    pm_uid,
                    run_ambiguous,
                    run_ambiguous,
                    dtn_db.build_data_task_item_delivery_key(
                        run_id=run_ambiguous, platform="feishu", recipient_id="ou_rebound_new", item_index=0
                    ),
                    pm_uid,
                    run_ambiguous,
                    run_ambiguous,
                ),
            )

        # Remove route in preferences to confirm reconciliation matches original persisted target/items, not current routes!
        await upsert_user_preferences(
            user_id=pm_uid,
            model_preference={
                "dalton_notify_enabled": True,
                "dalton_notify_targets": ["feishu:@me"],
                "dalton_notify_routes": {"data:tsmc-adr-premium": []},
            },
            conn=admin_conn,
        )

        # Verify that neither the new rebound recipient's `sent` rows nor the ambiguous legacy row reconcile!
        assert await notify_dispatch_mod.retry_failed_notify_deliveries(
            limit=5,
            max_attempts=3,
            stale_pending_minutes=10,
            data_task_notify_fn=gw_spy,
        ) == 0
        async with admin_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT execution_id, status FROM dalton_notify_deliveries WHERE execution_id IN (%s, %s)",
                (run_3rd, run_ambiguous),
            )
            statuses_before = {r["execution_id"]: r["status"] for r in await cur.fetchall()}
            assert statuses_before == {run_3rd: "pending", run_ambiguous: "pending"}

        # Now also unlink the user completely (`DELETE FROM dalton_channel_identities`) before the original recipient's
        # delayed item 1 (`k1_3rd` for `ou_r4_pm`) completes -> reconciliation still succeeds using persisted recipient!
        async with admin_conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM dalton_channel_identities WHERE user_id = %s",
                (pm_uid,),
            )
            await cur.execute(
                "DELETE FROM dalton_notify_deliveries WHERE execution_id = %s",
                (run_ambiguous,),
            )
            await cur.execute(
                """
                INSERT INTO dalton_channel_deliveries (
                    delivery_key, kind, user_id, run_id, execution_id,
                    is_current, has_non_empty_text, platform, target_address,
                    status, total_chunks, sent_chunks, created_at, updated_at
                )
                VALUES (%s, 'data_task', %s, %s, %s::uuid, FALSE, TRUE, 'feishu', 'feishu:@me', 'sent', 1, 1, NOW(), NOW())
                """,
                (k1_3rd, pm_uid, run_3rd, run_3rd),
            )

        reconciled_sent = await notify_dispatch_mod.retry_failed_notify_deliveries(
            limit=5,
            max_attempts=3,
            stale_pending_minutes=10,
            data_task_notify_fn=gw_spy,
        )
        assert reconciled_sent == 1
        assert observed_send_run_ids == []
        assert claim_spy.call_count == 0

        async with admin_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status, attempt_count, error_code FROM dalton_notify_deliveries WHERE execution_id = %s",
                (run_3rd,),
            )
            assert await cur.fetchone() == {
                "status": "sent",
                "attempt_count": 3,
                "error_code": None,
            }
            await cur.execute(
                "SELECT notify_status FROM dalton_data_task_runs WHERE run_id = %s::uuid",
                (run_3rd,),
            )
            assert (await cur.fetchone())["notify_status"] == "dispatched"
            # Restore original identity `ou_r4_pm` for subsequent steps
            await cur.execute(
                """
                INSERT INTO dalton_channel_identities (user_id, platform, platform_user_id)
                VALUES (%s, 'feishu', 'ou_r4_pm')
                """,
                (pm_uid,),
            )

        # Restore route for subsequent retry queue progress check
        await upsert_user_preferences(
            user_id=pm_uid,
            model_preference={
                "dalton_notify_enabled": True,
                "dalton_notify_targets": ["feishu:@me"],
                "dalton_notify_routes": {"data:tsmc-adr-premium": ["feishu:@me"]},
            },
            conn=admin_conn,
        )

        # ---------------------------------------------------------------------
        # 2. Repeated and concurrent reconciliation (`asyncio.gather`)
        # ---------------------------------------------------------------------
        k0_conc = dtn_db.build_data_task_item_delivery_key(
            run_id=run_conc,
            platform="feishu",
            recipient_id="ou_r4_pm",
            item_index=0,
        )
        async with admin_conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_notify_deliveries (
                    delivery_id, execution_id, automation_id, user_id, channel, target,
                    status, attempt_count, error_code, payload_json,
                    created_at, updated_at
                )
                VALUES (%s, %s, 'data:tsmc-adr-premium', %s, 'feishu', 'feishu:@me',
                        'pending', 3, 'in_flight', %s,
                        NOW() - INTERVAL '30 minutes', NOW() - INTERVAL '30 minutes')
                """,
                (
                    str(uuid4()),
                    run_conc,
                    pm_uid,
                    Jsonb({**one_card_payload, "reconcile_delivery_keys": [k0_conc]}),
                ),
            )
            await cur.execute(
                """
                INSERT INTO dalton_channel_deliveries (
                    delivery_key, kind, user_id, run_id, execution_id,
                    is_current, has_non_empty_text, platform, target_address,
                    status, total_chunks, sent_chunks, created_at, updated_at
                )
                VALUES (%s, 'data_task', %s, %s, %s::uuid, FALSE, TRUE, 'feishu', 'feishu:@me', 'sent', 1, 1, NOW(), NOW())
                """,
                (k0_conc, pm_uid, run_conc, run_conc),
            )

        conc_counts = await asyncio.gather(
            *(dtn_db.reconcile_uncertain_data_task_deliveries(limit=5) for _ in range(4))
        )
        assert sum(conc_counts) == 1
        assert await dtn_db.reconcile_uncertain_data_task_deliveries(limit=5) == 0

        # ---------------------------------------------------------------------
        # 3. Fairness & R5 Part A #1: 6 older exhausted uncertain `data:` entries +
        #    newer retryable `data:` `failed` entry + ordinary non-`data:` `pending`/`failed` rows (`limit=5`)
        # ---------------------------------------------------------------------
        async with admin_conn.cursor() as cur:
            for idx, urid in enumerate(exhausted_uncertain_runs):
                ukey = dtn_db.build_data_task_item_delivery_key(
                    run_id=urid,
                    platform="feishu",
                    recipient_id="ou_r4_pm",
                    item_index=0,
                )
                age_mins = 120 - idx * 5
                await cur.execute(
                    """
                    INSERT INTO dalton_notify_deliveries (
                        delivery_id, execution_id, automation_id, user_id, channel, target,
                        status, attempt_count, error_code, payload_json,
                        created_at, updated_at
                    )
                    VALUES (%s, %s, 'data:tsmc-adr-premium', %s, 'feishu', 'feishu:@me',
                            'pending', 3, 'delivery_uncertain', %s,
                            NOW() - make_interval(mins => %s),
                            NOW() - make_interval(mins => %s))
                    """,
                    (
                        str(uuid4()),
                        urid,
                        pm_uid,
                        Jsonb({**one_card_payload, "reconcile_delivery_keys": [ukey]}),
                        age_mins,
                        age_mins,
                    ),
                )
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_deliveries (
                        delivery_key, kind, user_id, run_id, execution_id,
                        is_current, has_non_empty_text, platform, target_address,
                        status, total_chunks, sent_chunks, created_at, updated_at
                    )
                    VALUES (%s, 'data_task', %s, %s, %s::uuid, FALSE, TRUE, 'feishu', 'feishu:@me', 'unknown', 1, 0, NOW(), NOW())
                    """,
                    (ukey, pm_uid, urid, urid),
                )

            # Newer retryable `data:` `failed` row (`attempt_count = 1`) placed AFTER all 6 old uncertain rows
            await cur.execute(
                """
                INSERT INTO dalton_notify_deliveries (
                    delivery_id, execution_id, automation_id, user_id, channel, target,
                    status, attempt_count, error_code, payload_json,
                    created_at, updated_at
                )
                VALUES (%s, %s, 'data:tsmc-adr-premium', %s, 'feishu', 'feishu:@me',
                        'failed', 1, 'http_502', %s, NOW(), NOW())
                """,
                (
                    str(uuid4()),
                    retry_run_id,
                    pm_uid,
                    Jsonb(
                        {
                            **one_card_payload,
                            "run_id": retry_run_id,
                            "text": "Retryable summary",
                        }
                    ),
                ),
            )
            # R5 Part A #1: Seed ordinary non-`data:` rows AFTER the 6 exhausted uncertain `data:` rows:
            # (a) an ordinary `pending` row with `attempt_count = 3` and `error_code = 'delivery_uncertain'`
            #     (must NOT be excluded by `data:` uncertain rules; transitions to `failed`/`attempts_exhausted`);
            # (b) an ordinary `failed` row (`attempt_count = 1`, `channel = 'unknown_test_channel'`)
            #     (must be claimed and processed by `retry_failed_notify_deliveries` within `limit=5`!).
            await cur.execute(
                """
                INSERT INTO dalton_notify_deliveries (
                    delivery_id, execution_id, automation_id, user_id, channel, target,
                    status, attempt_count, error_code, payload_json,
                    created_at, updated_at
                )
                VALUES
                (%s, %s, 'briefing:morning', %s, 'feishu', 'feishu:oc_ord_1',
                 'pending', 3, 'delivery_uncertain', %s,
                 NOW() - INTERVAL '25 minutes', NOW() - INTERVAL '25 minutes'),
                (%s, %s, 'briefing:evening', %s, 'unknown_test_channel', 'unknown:target_1',
                 'failed', 1, 'http_502', %s, NOW(), NOW())
                """,
                (
                    str(uuid4()),
                    ordinary_exhausted_exec_id,
                    pm_uid,
                    Jsonb({"title": "Morning Briefing", "summary": "Ordinary non-data pending row"}),
                    str(uuid4()),
                    ordinary_retry_exec_id,
                    pm_uid,
                    Jsonb({"title": "Evening Briefing", "summary": "Ordinary non-data retryable row"}),
                ),
            )

        # Also verify `claim_target_delivery` still claims an ordinary non-`data:` row even if its `error_code` was `delivery_uncertain`
        ordinary_claim_ok = await notify_dispatch_mod.claim_target_delivery(
            execution_id=ordinary_retry_exec_id,
            automation_id="briefing:evening",
            user_id=pm_uid,
            channel="unknown_test_channel",
            target="unknown:target_claim_check",
            payload_snapshot={"title": "Claim check"},
            max_attempts=3,
            stale_pending_minutes=10,
        )
        assert ordinary_claim_ok is True
        async with admin_conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM dalton_notify_deliveries WHERE execution_id = %s AND target = 'unknown:target_claim_check'",
                (ordinary_retry_exec_id,),
            )

        progressed = await notify_dispatch_mod.retry_failed_notify_deliveries(
            limit=5,
            max_attempts=3,
            stale_pending_minutes=10,
            data_task_notify_fn=gw_spy,
        )
        assert progressed == 1
        # Only the newer retryable `data:` `failed` row was sent via `data_task_notify_fn`;
        # zero send/claim calls for the 6 uncertain `data:` rows, and both ordinary non-`data:` rows were processed!
        assert observed_send_run_ids == [retry_run_id]
        assert claim_spy.call_count == 0

        async with admin_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status, attempt_count, error_code FROM dalton_notify_deliveries WHERE execution_id = %s",
                (retry_run_id,),
            )
            assert await cur.fetchone() == {
                "status": "sent",
                "attempt_count": 2,
                "error_code": None,
            }
            await cur.execute(
                "SELECT status, attempt_count, error_code FROM dalton_notify_deliveries WHERE execution_id = %s",
                (ordinary_exhausted_exec_id,),
            )
            assert await cur.fetchone() == {
                "status": "failed",
                "attempt_count": 3,
                "error_code": "attempts_exhausted",
            }
            await cur.execute(
                "SELECT status, attempt_count, error_code FROM dalton_notify_deliveries WHERE execution_id = %s",
                (ordinary_retry_exec_id,),
            )
            assert await cur.fetchone() == {
                "status": "failed",
                "attempt_count": 2,
                "error_code": "unknown_channel",
            }
            await cur.execute(
                "SELECT COUNT(*) AS cnt FROM dalton_notify_deliveries WHERE execution_id = ANY(%s) AND status = 'pending'",
                (exhausted_uncertain_runs,),
            )
            assert (await cur.fetchone())["cnt"] == 6

        # Late durable `sent` arrives on `exhausted_uncertain_runs[0]` while 5 other uncertain rows remain unresolved (`limit=5`)
        late_key = dtn_db.build_data_task_item_delivery_key(
            run_id=exhausted_uncertain_runs[0],
            platform="feishu",
            recipient_id="ou_r4_pm",
            item_index=0,
        )
        await cg_db.update_delivery_progress(
            delivery_key=late_key,
            sent_chunks=1,
            total_chunks=1,
            platform_message_ids=["om_late_ack_0"],
            status="sent",
        )
        late_progressed = await notify_dispatch_mod.retry_failed_notify_deliveries(
            limit=5,
            max_attempts=3,
            stale_pending_minutes=10,
            data_task_notify_fn=gw_spy,
        )
        assert late_progressed == 1
        assert observed_send_run_ids == [retry_run_id]
        assert claim_spy.call_count == 0

        async with admin_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status, attempt_count, error_code FROM dalton_notify_deliveries WHERE execution_id = %s",
                (exhausted_uncertain_runs[0],),
            )
            assert await cur.fetchone() == {
                "status": "sent",
                "attempt_count": 3,
                "error_code": None,
            }

        # ---------------------------------------------------------------------
        # 4. Simulated database read failure leaves remaining 5 uncertain rows intact
        # ---------------------------------------------------------------------
        @asynccontextmanager
        async def _failing_reconcile_conn(_c: Any = None):
            raise psycopg.OperationalError("simulated read error during reconciliation")
            yield  # pragma: no cover

        monkeypatch.setattr(
            dtn_db,
            "get_db_connection",
            create_autospec(dtn_db.get_db_connection, side_effect=_failing_reconcile_conn),
        )
        assert await dtn_db.reconcile_uncertain_data_task_deliveries(limit=5) == 0

        async with admin_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM dalton_notify_deliveries
                WHERE execution_id = ANY(%s)
                  AND status = 'pending'
                  AND attempt_count = 3
                  AND error_code = 'delivery_uncertain'
                """,
                (exhausted_uncertain_runs[1:],),
            )
            assert (await cur.fetchone())["cnt"] == 5
    finally:
        async with admin_conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM dalton_notify_deliveries WHERE execution_id = ANY(%s)",
                (all_notify_exec_ids,),
            )
            await cur.execute(
                "DELETE FROM dalton_channel_deliveries WHERE run_id = ANY(%s)",
                (all_run_ids,),
            )
            await cur.execute(
                "DELETE FROM dalton_channel_identities WHERE user_id IN (%s, %s)",
                (pm_uid, other_uid),
            )
            await cur.execute(
                "DELETE FROM dalton_data_task_runs WHERE run_id = ANY(%s::uuid[])",
                (all_run_ids,),
            )
            await cur.execute(
                "DELETE FROM dalton_data_task_defs WHERE job_key = 'tsmc-adr-premium'"
            )
            await cur.execute(
                "DELETE FROM user_preferences WHERE user_id IN (%s, %s)",
                (pm_uid, other_uid),
            )
            await cur.execute(
                "DELETE FROM users WHERE user_id IN (%s, %s)",
                (pm_uid, other_uid),
            )
        await admin_conn.close()


@pytest.mark.asyncio
async def test_persisted_image_metadata_finalization_and_tamper_rejection_db(
    migrated_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5 Part B §4 items 5 & 6 against real isolated PostgreSQL:
    - `finalize_task_run` commits `status='succeeded'`, `notify_status='pending_dispatch'`, and `outbox_payload`
      containing verified positive `size_bytes` and 64-hex `sha256` for representative T52 and T54 reports;
    - `/plan` and `/artifacts/{artifact_id}` succeed when on-disk PNG matches persisted `size_bytes` and `sha256`;
    - Replacing the on-disk PNG with a different valid PNG of the exact same byte length causes both a fresh `/plan`
      request (rebuilt from persisted DB state) and `/artifacts/{artifact_id}` to reject it (`artifact_sha256_mismatch`);
    - Unsuccessful image preparation (mismatched supplied digest or missing image) commits `status='failed'`,
      `notify_status='skipped'`, `outbox_payload=NULL`, and cannot authorize or claim a delivery (`run_not_succeeded`);
    - Historical image record in DB without `size_bytes`/`sha256` is rejected (`artifact_metadata_missing`) without
      recomputing digests from disk or mutating DB state, while text-only runs continue to work.
    """
    from src.dalton.data_jobs import scheduler as sched
    from src.dalton.data_jobs.contracts import TaskEvaluationResult
    from src.dalton.data_jobs.docker_runner import ContainerInspectionOutcome

    dsn = migrated_dsn
    pm_uid = f"pm-r5-img-{uuid4().hex[:8]}"
    run_t52 = str(uuid4())
    run_t54 = str(uuid4())
    run_prep_fail = str(uuid4())
    run_legacy_unprepared = str(uuid4())
    run_text_only = str(uuid4())
    all_runs = [run_t52, run_t54, run_prep_fail, run_legacy_unprepared, run_text_only]

    path_map = build_path_map(
        host_var_dir=tmp_path / "r5_h_var",
        backend_var_dir=tmp_path / "r5_b_var",
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

    monkeypatch.setenv("DALTON_INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("DALTON_PM_USER_IDS", pm_uid)
    monkeypatch.setattr(dtn_router_mod, "build_path_map", lambda: path_map)
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
        model_prefs_mod,
        "get_user_preferences",
        create_autospec(model_prefs_mod.get_user_preferences, side_effect=_real_get_prefs),
    )
    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(access_tiers_mod.get_user_tier, return_value="admin"),
    )
    monkeypatch.setattr(sched, "check_scheduler_lock", create_autospec(sched.check_scheduler_lock, return_value=None))
    monkeypatch.setattr(sched, "freeze_task_run_artifacts", lambda **_kw: None)
    monkeypatch.setattr(sched, "cleanup_run_secrets_dir", lambda *_a, **_kw: None)

    png_orig = _make_png("R5-T52-T54-ORIG")
    # Same-byte-length replacement PNG with a 1-bit flip in the tag payload
    png_same_len_tampered = _make_png("R5-T52-T54-TAMP")
    assert len(png_orig) == len(png_same_len_tampered)
    assert hashlib.sha256(png_orig).hexdigest() != hashlib.sha256(png_same_len_tampered).hexdigest()

    admin_conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        manifest = load_data_task_manifest()
        slot_base = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
        async with admin_conn.transaction():
            await sync_manifest_to_db(admin_conn, manifest)
            async with admin_conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE dalton_data_task_defs
                    SET enabled = TRUE, notify_enabled = TRUE
                    WHERE job_key IN ('softbank-nav-monitor', 'tsmc-adr-premium')
                    """
                )
                await cur.execute(
                    """
                    INSERT INTO users (user_id, email, name, created_at, updated_at)
                    VALUES (%s, %s, %s, NOW(), NOW())
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    (pm_uid, f"{pm_uid}@synth.local", pm_uid),
                )
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_identities (user_id, platform, platform_user_id)
                    VALUES (%s, 'feishu', 'ou_r5_pm')
                    """,
                    (pm_uid,),
                )
                # Insert `running` rows for `run_t52`, `run_t54`, and `run_prep_fail` to finalize via `finalize_task_run`
                for idx, (rid, jkey) in enumerate(
                    [
                        (run_t52, "softbank-nav-monitor"),
                        (run_t54, "tsmc-adr-premium"),
                        (run_prep_fail, "softbank-nav-monitor"),
                    ]
                ):
                    s_utc = slot_base + timedelta(minutes=idx * 5)
                    await cur.execute(
                        """
                        INSERT INTO dalton_data_task_runs (
                            run_id, job_key, trigger_source, scheduled_slot_utc,
                            deadline_utc, status, notify_status
                        )
                        VALUES (%s::uuid, %s, 'cron', %s, %s, 'running', 'skipped')
                        """,
                        (rid, jkey, s_utc, s_utc + timedelta(minutes=10)),
                    )
                # Insert historical image record without `size_bytes`/`sha256` and a text-only record
                legacy_unprepared_outbox = {
                    "text": "Legacy unprepared image report",
                    "feishu_cards": [
                        {
                            "card": {"schema": "2.0", "body": {"elements": [{"tag": "img", "img_key": "ph_leg"}]}},
                            "images": [{"placeholder": "ph_leg", "path": "images/c0.png"}],
                        }
                    ],
                }
                text_only_outbox = {
                    "text": "Text-only report summary",
                    "feishu_cards": [
                        {
                            "card": {
                                "schema": "2.0",
                                "body": {"elements": [{"tag": "markdown", "content": "Text only"}]},
                            },
                            "images": [],
                        }
                    ],
                }
                await cur.execute(
                    """
                    INSERT INTO dalton_data_task_runs (
                        run_id, job_key, trigger_source, scheduled_slot_utc,
                        deadline_utc, status, notify_status, outbox_payload, notify_preview, completed_at
                    )
                    VALUES
                    (%s::uuid, 'softbank-nav-monitor', 'cron', %s, %s, 'succeeded', 'pending_dispatch', %s, %s, NOW()),
                    (%s::uuid, 'tsmc-adr-premium', 'cron', %s, %s, 'succeeded', 'pending_dispatch', %s, %s, NOW())
                    """,
                    (
                        run_legacy_unprepared,
                        slot_base + timedelta(minutes=30),
                        slot_base + timedelta(minutes=40),
                        Jsonb(legacy_unprepared_outbox),
                        Jsonb(legacy_unprepared_outbox),
                        run_text_only,
                        slot_base + timedelta(minutes=45),
                        slot_base + timedelta(minutes=55),
                        Jsonb(text_only_outbox),
                        Jsonb(text_only_outbox),
                    ),
                )
            await upsert_user_preferences(
                user_id=pm_uid,
                model_preference={
                    "dalton_notify_enabled": True,
                    "dalton_notify_targets": ["feishu:@me"],
                    "dalton_notify_routes": {
                        "data:softbank-nav-monitor": ["feishu:@me"],
                        "data:tsmc-adr-premium": ["feishu:@me"],
                    },
                },
                conn=admin_conn,
            )

        # Stage artifacts on disk for `run_t52`, `run_t54`, `run_prep_fail`, and `run_legacy_unprepared`
        for rid in (run_t52, run_t54, run_prep_fail, run_legacy_unprepared):
            rdir = path_map.backend_run_artifacts_dir(rid)
            (rdir / "images").mkdir(parents=True, exist_ok=True)
            (rdir / "images" / "c0.png").write_bytes(png_orig)

        # 1. Finalize `run_t52` and `run_t54` via `finalize_task_run` against real PostgreSQL
        for idx, (rid, jkey, label) in enumerate(
            [
                (run_t52, "softbank-nav-monitor", "T52 Pre-Market"),
                (run_t54, "tsmc-adr-premium", "T54 Post-Market"),
            ]
        ):
            rdir = path_map.backend_run_artifacts_dir(rid)
            raw_ob = {
                "text": f"{label} summary",
                "feishu_cards": [
                    {
                        "card": {
                            "schema": "2.0",
                            "header": {"title": {"tag": "plain_text", "content": label}},
                            "body": {"elements": [{"tag": "img", "img_key": "ph_0"}]},
                        },
                        "images": [{"placeholder": "ph_0", "path": "images/c0.png"}],
                    }
                ],
            }
            (rdir / "outbox.json").write_text(json.dumps(raw_ob), encoding="utf-8")
            claimed = sched.ClaimedTaskRun(
                run_id=rid,
                job_key=jkey,
                trigger_source="cron",
                scheduled_slot_utc=slot_base + timedelta(minutes=idx * 5),
                attempt_no=1,
                tier="T2",
                exec_class="container_playwright",
                command_json=["python", "-m", "task"],
                timeout_seconds=120,
                no_output_timeout_seconds=60,
                notify_enabled=True,
                is_trial=False,
            )
            ok_fin, _ = await sched.finalize_task_run(
                admin_conn,
                lock_conn=object(),
                docker_client=None,
                claimed=claimed,
                container_id=None,
                outcome=ContainerInspectionOutcome(running=False, exit_code=0, oom_killed=False, log_tail="ok"),
                evaluation=TaskEvaluationResult(ok=True, status="succeeded"),
                outbox_payload=raw_ob,
                frozen_manifest={"job_key": jkey, "status": "ok"},
                path_map=path_map,
            )
            assert ok_fin is True

            async with admin_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT status, notify_status, outbox_payload FROM dalton_data_task_runs WHERE run_id = %s::uuid",
                    (rid,),
                )
                row_db = await cur.fetchone()
                assert row_db["status"] == "succeeded"
                assert row_db["notify_status"] == "pending_dispatch"
                img_db = row_db["outbox_payload"]["feishu_cards"][0]["images"][0]
                assert img_db["size_bytes"] == len(png_orig)
                assert img_db["sha256"] == hashlib.sha256(png_orig).hexdigest()

        # 2. Finalize `run_prep_fail` with mismatched supplied sha256 -> commits `failed`, `skipped`, `outbox_payload=NULL`
        bad_ob = {
            "text": "Bad digest",
            "feishu_cards": [
                {
                    "card": {"schema": "2.0", "body": {"elements": [{"tag": "img", "img_key": "ph_0"}]}},
                    "images": [
                        {
                            "placeholder": "ph_0",
                            "path": "images/c0.png",
                            "size_bytes": len(png_orig),
                            "sha256": "0" * 64,
                        }
                    ],
                }
            ],
        }
        claimed_fail = sched.ClaimedTaskRun(
            run_id=run_prep_fail,
            job_key="softbank-nav-monitor",
            trigger_source="cron",
            scheduled_slot_utc=slot_base + timedelta(minutes=10),
            attempt_no=1,
            tier="T2",
            exec_class="container_playwright",
            command_json=["python", "-m", "task"],
            timeout_seconds=120,
            no_output_timeout_seconds=60,
            notify_enabled=True,
            is_trial=False,
        )
        ok_fail_fin, _ = await sched.finalize_task_run(
            admin_conn,
            lock_conn=object(),
            docker_client=None,
            claimed=claimed_fail,
            container_id=None,
            outcome=ContainerInspectionOutcome(running=False, exit_code=0, oom_killed=False, log_tail="ok"),
            evaluation=TaskEvaluationResult(ok=True, status="succeeded"),
            outbox_payload=bad_ob,
            frozen_manifest={"job_key": "softbank-nav-monitor", "status": "ok"},
            path_map=path_map,
        )
        assert ok_fail_fin is True
        async with admin_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT status, notify_status, outbox_payload, failure_reason FROM dalton_data_task_runs WHERE run_id = %s::uuid",
                (run_prep_fail,),
            )
            fail_row = await cur.fetchone()
            assert fail_row["status"] == "failed"
            assert fail_row["notify_status"] == "skipped"
            assert fail_row["outbox_payload"] is None
            assert "report_preparation_failed" in str(fail_row["failure_reason"])

        # 3. Exercise internal FastAPI `/plan` and `/artifacts/{artifact_id}` endpoints against real PostgreSQL
        app = FastAPI()
        app.include_router(dalton_router)
        headers = {"X-Dalton-Service-Token": SERVICE_TOKEN}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://backend.test",
        ) as client:
            # (a) Valid prepared run (`run_t52`): `/plan` and `/artifacts/{artifact_id}` succeed
            plan_res = await client.post(
                f"/api/v1/dalton/internal/data-task-runs/{run_t52}/notification-plan",
                headers=headers,
                json={"user_id": pm_uid, "platform": "feishu", "target": "feishu:@me"},
            )
            assert plan_res.status_code == 200
            plan_body = plan_res.json()
            assert plan_body["ok"] is True
            artifact_id = plan_body["plan"]["artifacts"][0]["artifact_id"]

            art_res = await client.get(
                f"/api/v1/dalton/internal/data-task-runs/{run_t52}/artifacts/{artifact_id}",
                headers=headers,
            )
            assert art_res.status_code == 200
            assert art_res.content == png_orig

            # (b) Replace on-disk PNG for `run_t52` with a different valid PNG of the EXACT SAME byte length:
            #     both a fresh `/plan` call (rebuilt from persisted DB state) and `/artifacts/{artifact_id}` reject it!
            t52_img_path = path_map.backend_run_artifacts_dir(run_t52) / "images" / "c0.png"
            t52_img_path.write_bytes(png_same_len_tampered)

            tampered_plan_res = await client.post(
                f"/api/v1/dalton/internal/data-task-runs/{run_t52}/notification-plan",
                headers=headers,
                json={"user_id": pm_uid, "platform": "feishu", "target": "feishu:@me"},
            )
            assert tampered_plan_res.status_code == 200
            assert tampered_plan_res.json()["ok"] is False
            assert tampered_plan_res.json()["code"] == "artifact_sha256_mismatch"

            tampered_art_res = await client.get(
                f"/api/v1/dalton/internal/data-task-runs/{run_t52}/artifacts/{artifact_id}",
                headers=headers,
            )
            assert tampered_art_res.status_code == 422
            assert tampered_art_res.json()["detail"]["code"] == "artifact_sha256_mismatch"

            # (c) Unsuccessful preparation run (`run_prep_fail`) cannot authorize or claim a delivery (`run_not_succeeded`)
            prep_fail_plan_res = await client.post(
                f"/api/v1/dalton/internal/data-task-runs/{run_prep_fail}/notification-plan",
                headers=headers,
                json={"user_id": pm_uid, "platform": "feishu", "target": "feishu:@me"},
            )
            assert prep_fail_plan_res.status_code == 200
            assert prep_fail_plan_res.json()["ok"] is False
            assert prep_fail_plan_res.json()["code"] == "run_not_succeeded"

            # (d) Historical image record without `size_bytes`/`sha256` (`run_legacy_unprepared`) is rejected
            #     with `artifact_metadata_missing` without recomputing digests from disk
            legacy_plan_res = await client.post(
                f"/api/v1/dalton/internal/data-task-runs/{run_legacy_unprepared}/notification-plan",
                headers=headers,
                json={"user_id": pm_uid, "platform": "feishu", "target": "feishu:@me"},
            )
            assert legacy_plan_res.status_code == 200
            assert legacy_plan_res.json()["ok"] is False
            assert legacy_plan_res.json()["code"] == "artifact_metadata_missing"

            # (e) Text-only run (`run_text_only`) continues to authorize and build plans normally
            text_plan_res = await client.post(
                f"/api/v1/dalton/internal/data-task-runs/{run_text_only}/notification-plan",
                headers=headers,
                json={"user_id": pm_uid, "platform": "feishu", "target": "feishu:@me"},
            )
            assert text_plan_res.status_code == 200
            assert text_plan_res.json()["ok"] is True
            assert len(text_plan_res.json()["plan"]["feishu_cards"]) == 1
    finally:
        async with admin_conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM dalton_notify_deliveries WHERE execution_id = ANY(%s)",
                (all_runs,),
            )
            await cur.execute(
                "DELETE FROM dalton_channel_deliveries WHERE run_id = ANY(%s)",
                (all_runs,),
            )
            await cur.execute(
                "DELETE FROM dalton_channel_identities WHERE user_id = %s",
                (pm_uid,),
            )
            await cur.execute(
                "DELETE FROM dalton_data_task_runs WHERE run_id = ANY(%s::uuid[])",
                (all_runs,),
            )
            await cur.execute(
                "DELETE FROM dalton_data_task_defs WHERE job_key IN ('softbank-nav-monitor', 'tsmc-adr-premium')"
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
