"""Real PostgreSQL + ASGI HTTP integration tests for AU1 Batch 1 (`d035_company_automations`)."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec, patch
import uuid

from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketException
from httpx import ASGITransport, AsyncClient
import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import pytest
import pytest_asyncio
from sqlalchemy import create_engine, pool

from src.dalton import access_tiers, auth as dalton_auth, sharing as dalton_sharing
from src.dalton.app import channels_router as ch_router_mod
from src.dalton.autonomy.classification import (
    build_scope_case_sql,
    classify_automation_scope,
    needs_workspace_tracking_lookup,
)
from src.dalton.autonomy.schedule import schedule_dalton_automation_helper
from src.dalton.budget.service import resolve_user_budget_config
from src.dalton.company_identity import (
    COMPANY_SYSTEM_EMAIL,
    COMPANY_SYSTEM_USER_ID,
    ensure_company_system_identity,
)
from src.dalton.database import company_automations as company_auto_db
from src.dalton.database.channel_gateway import hash_bind_code
from src.dalton.migrations.versions import d035_company_automations as d035_mod
from src.dalton.services import company_automations as company_auto_svc
from src.dalton.settings import DaltonBudgetDefaults, get_dalton_settings
from src.server.app import automations as automations_app
from src.server.auth import jwt_bearer, oauth_resource, pats, ws_auth
from src.server.database import automation as auto_db
from src.server.database import computer as computer_db
from src.server.database.home_workspace import get_home_workspace_id
from src.server.database.session_lock import close_shared_lock_session
from src.server.dependencies import principal as principal_mod
from src.server.models.automation import AutomationCreate
from src.server.services import cli_tokens
from src.server.services.automations import lifecycle as lifecycle_mod
from src.server.utils import api as api_utils

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _dalton_alembic_cfg(test_db_uri: str) -> Config:
    cfg = Config(str(PROJECT_ROOT / "src" / "dalton" / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "src" / "dalton" / "migrations"))
    sa_url = test_db_uri.replace("postgresql://", "postgresql+psycopg://", 1)
    cfg.set_main_option("sqlalchemy.url", sa_url.replace("%", "%%"))
    return cfg


async def _reset_au1_test_rows(conn: psycopg.AsyncConnection) -> None:
    await conn.execute(
        """
        TRUNCATE TABLE
            dalton_channel_weixin_state,
            dalton_channel_automation_runs,
            dalton_channel_deliveries,
            dalton_channel_background_runs,
            dalton_channel_inbound_events,
            dalton_channel_thread_watches,
            dalton_channel_session_generations,
            dalton_channel_chat_bindings,
            dalton_channel_user_settings,
            dalton_channel_bind_codes,
            dalton_channel_identities,
            dalton_workspace_sharing,
            automation_executions,
            automations,
            dalton_coverage_units,
            dalton_coverage,
            dalton_data_task_runs,
            dalton_data_task_defs,
            dalton_company_migration_batches,
            dalton_internal_feishu_groups,
            user_preferences,
            workspaces
        CASCADE
        """
    )
    await conn.execute("DELETE FROM computers")
    await conn.execute(
        "DELETE FROM users WHERE user_id <> %s",
        (COMPANY_SYSTEM_USER_ID,),
    )
    await conn.execute(
        """
        UPDATE dalton_notify_storage_state
        SET storage_mode = 'legacy',
            vault_source = 'legacy',
            vault_source_batch_id = NULL,
            updated_by = NULL
        WHERE state_id = 1
        """
    )
    await ensure_company_system_identity(conn=conn)


@pytest_asyncio.fixture
async def au1_live_pool(
    dalton_schema,
    cleanup_dalton_tables,
    test_db_uri: str,
):
    """Open a tuple-default AsyncConnectionPool on the test's live loop matching production pool.py."""
    from ptc_agent.agent.backends import db_json_route as ptc_db_json_route
    from src.dalton import (
        company_identity as company_identity_mod,
        sharing as dalton_sharing_mod,
    )
    from src.dalton.autonomy import (
        schedule as dalton_schedule_mod,
        system_jobs as dalton_system_jobs_mod,
        tracking as dalton_tracking_mod,
    )
    from src.dalton.database import (
        budget as dalton_budget_db,
        channel_gateway as dalton_channel_gw_db,
        company_automations as company_auto_db,
        coverage as dalton_coverage_db,
        coverage_units as dalton_coverage_units_db,
        members as dalton_members_db,
    )
    from src.dalton.notify import dispatch as dalton_notify_mod
    from src.ptc_agent.agent.backends import db_json_route as src_db_json_route
    from src.server.database import (
        automation as server_auto_db,
        automation_executions as server_exec_db,
        computer as server_computer_db,
        pool as pool_mod,
        principal_reach as server_reach_db,
        user as server_user_db,
        workspace as server_workspace_db,
    )

    local_pool = AsyncConnectionPool(
        conninfo=test_db_uri,
        min_size=1,
        max_size=5,
        configure=pool_mod._configure_postgres_connection,
        check=AsyncConnectionPool.check_connection,
        kwargs=dict(pool_mod._CONNECTION_KWARGS),
        open=False,
    )
    await local_pool.open()
    await local_pool.wait()

    @asynccontextmanager
    async def _live_get_db_connection(conn=None):
        if conn is not None:
            yield conn
            return
        async with local_pool.connection(timeout=10) as owned:
            yield owned

    async def _live_session_connection():
        conn = await psycopg.AsyncConnection.connect(
            test_db_uri, **pool_mod._CONNECTION_KWARGS
        )
        await pool_mod._configure_postgres_connection(conn)
        return conn

    db_call_sites = (
        pool_mod,
        ptc_db_json_route,
        src_db_json_route,
        company_identity_mod,
        dalton_sharing_mod,
        dalton_schedule_mod,
        dalton_system_jobs_mod,
        dalton_tracking_mod,
        dalton_budget_db,
        dalton_channel_gw_db,
        company_auto_db,
        dalton_coverage_db,
        dalton_coverage_units_db,
        dalton_members_db,
        dalton_notify_mod,
        server_auto_db,
        server_exec_db,
        server_computer_db,
        server_reach_db,
        server_user_db,
        server_workspace_db,
    )

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(pool_mod, "get_db_connection_string", lambda: test_db_uri)
        )
        stack.enter_context(
            patch.object(pool_mod, "get_or_create_pool", lambda: local_pool)
        )
        stack.enter_context(
            patch.object(pool_mod, "open_session_connection", _live_session_connection)
        )
        for module in db_call_sites:
            stack.enter_context(
                patch.object(module, "get_db_connection", _live_get_db_connection)
            )
        await close_shared_lock_session()
        try:
            async with local_pool.connection() as init_conn:
                await _reset_au1_test_rows(init_conn)
            yield local_pool
        finally:
            await close_shared_lock_session()
            await local_pool.close()


async def _insert_user(conn: psycopg.AsyncConnection, user_id: str, email: str) -> None:
    await conn.execute(
        """
        INSERT INTO users (user_id, email, name, created_at, updated_at)
        VALUES (%s, %s, %s, NOW(), NOW())
        ON CONFLICT (user_id) DO UPDATE SET email = EXCLUDED.email
        """,
        (user_id, email, user_id),
    )


async def _insert_computer(
    conn: psycopg.AsyncConnection,
    computer_id: str,
    user_id: str,
    name: str,
) -> None:
    await computer_db.create_computer(
        user_id,
        kind="docker",
        name=name,
        status="running",
        computer_id=computer_id,
        conn=conn,
    )


async def _insert_workspace(
    conn: psycopg.AsyncConnection,
    workspace_id: str,
    user_id: str,
    name: str,
    *,
    status: str = "running",
    computer_id: str | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO workspaces (
            workspace_id, user_id, name, status, computer_id, created_at, updated_at
        )
        VALUES (%s::uuid, %s, %s, %s, %s::uuid, NOW(), NOW())
        """,
        (workspace_id, user_id, name, status, computer_id),
    )


# ---------------------------------------------------------------------------
# 1. Migration upgrade, missing-upstream guard, clean downgrade, and conflict refusal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d035_missing_upstream_tables_fails_without_partial_schema(
    test_db_uri: str,
) -> None:
    sa_url = test_db_uri.replace("postgresql://", "postgresql+psycopg://", 1)

    def _probe_missing_upstream() -> None:
        engine = create_engine(sa_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as sa_conn:
                trans = sa_conn.begin()
                try:
                    # Simulate running Dalton d035 when upstream public.automations is absent.
                    sa_conn.exec_driver_sql("DROP TABLE public.automations CASCADE")
                    sa_conn.exec_driver_sql(
                        "DROP TABLE IF EXISTS public.dalton_internal_feishu_groups CASCADE"
                    )
                    sa_conn.exec_driver_sql(
                        "DROP TABLE IF EXISTS public.dalton_notify_storage_state CASCADE"
                    )
                    sa_conn.exec_driver_sql(
                        "DROP TABLE IF EXISTS public.dalton_company_migration_batches CASCADE"
                    )

                    ctx = MigrationContext.configure(sa_conn)
                    with Operations.context(ctx):
                        with pytest.raises(RuntimeError, match="missing tables: public.automations"):
                            d035_mod.upgrade()

                    # Verify zero partial tables were created before the failure raised.
                    for tbl in (
                        "public.dalton_internal_feishu_groups",
                        "public.dalton_notify_storage_state",
                        "public.dalton_company_migration_batches",
                    ):
                        assert (
                            sa_conn.exec_driver_sql("SELECT to_regclass(%s)", (tbl,)).scalar()
                            is None
                        )
                finally:
                    trans.rollback()
        finally:
            engine.dispose()

    await asyncio.to_thread(_probe_missing_upstream)


async def _dalton_alembic_versions(conn: psycopg.AsyncConnection) -> list[str]:
    async with conn.cursor() as cur:
        await cur.execute("SELECT version_num FROM alembic_version_dalton ORDER BY version_num")
        return [str(r[0]) for r in await cur.fetchall()]


async def _column_exists(
    conn: psycopg.AsyncConnection,
    table_name: str,
    column_name: str,
) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
              AND column_name = %s
            """,
            (table_name, column_name),
        )
        return (await cur.fetchone()) is not None


async def _assert_d031_shared_command_columns(conn: psycopg.AsyncConnection) -> None:
    for tbl, col in (
        ("dalton_channel_session_generations", "selection_initialized_at"),
        ("dalton_channel_session_generations", "last_conversation_activity_at"),
        ("dalton_channel_inbound_events", "command_request"),
        ("dalton_channel_inbound_events", "command_result"),
        ("dalton_channel_deliveries", "activity_origin"),
        ("dalton_channel_thread_watches", "pending_interrupt"),
    ):
        assert await _column_exists(conn, tbl, col), f"Missing d031 column {tbl}.{col}"


@pytest.mark.asyncio
async def test_d035_upgrade_from_d031_preserves_state_and_enforces_safe_downgrade(
    test_db_uri: str,
    au1_live_pool: AsyncConnectionPool,
) -> None:
    dalton_cfg = _dalton_alembic_cfg(test_db_uri)
    script_dir = ScriptDirectory.from_config(dalton_cfg)
    assert script_dir.get_heads() == ["d035"]
    assert script_dir.get_revision("d035").down_revision == "d031"
    assert script_dir.get_revision("d031").down_revision == "d030"

    # First establish current d031 schema (with d030 + d031 objects intact, pre-d035).
    await asyncio.to_thread(command.downgrade, dalton_cfg, "d031")

    u1 = "u-mig-1"
    u2 = "u-mig-2"
    ws_active = str(uuid.uuid4())
    ws_other = str(uuid.uuid4())
    ws_deleted = str(uuid.uuid4())
    ws_archived_unit = str(uuid.uuid4())
    fixed_ts = datetime(2026, 10, 1, 8, 30, tzinfo=UTC)

    seeded_specs = [
        ("personal-note", u1, "Morning Journal", ws_active, {}, "personal", "active"),
        ("calendar-no-meta", u1, "dalton-earnings-calendar", None, {}, "research_tracking", "active"),
        (
            "kind-watch",
            u1,
            "dalton-nvda-watch",
            ws_active,
            {"dalton_kind": "company_watch"},
            "research_tracking",
            "paused",
        ),
        ("custom-covered", u1, "dalton-nvda-custom-check", ws_active, {}, "research_tracking", "active"),
        ("near-miss-name", u1, "my-dalton-nvda-watch", ws_active, {}, "personal", "active"),
        ("other-owner-ws", u1, "dalton-nvda-stolen", ws_other, {}, "personal", "active"),
        ("deleted-ws", u1, "dalton-amd-custom", ws_deleted, {}, "personal", "active"),
        ("archived-unit", u1, "dalton-msft-custom", ws_archived_unit, {}, "personal", "active"),
        (
            "director-daily",
            u1,
            "dalton-director-daily",
            ws_active,
            {"dalton_kind": "director_daily"},
            "personal",
            "active",
        ),
        (
            "director-weekly",
            u1,
            "dalton-director-weekly",
            ws_active,
            {"dalton_kind": "director_weekly"},
            "personal",
            "active",
        ),
        (
            "data-health",
            u1,
            "dalton-data-health",
            ws_active,
            {"dalton_kind": "data_health"},
            "personal",
            "active",
        ),
        (
            "alphaengine",
            u1,
            "AlphaEngine 机构会议增量整理入库（每日 09:00 / 18:00）",
            ws_active,
            {},
            "personal",
            "active",
        ),
    ]

    id_by_label: dict[str, str] = {}
    async with au1_live_pool.connection() as db_conn:
        assert await _dalton_alembic_versions(db_conn) == ["d031"]
        await _assert_d031_shared_command_columns(db_conn)
        assert not await _column_exists(db_conn, "automations", "scope")
        assert not await _column_exists(
            db_conn, "dalton_data_task_defs", "notify_storage_mode"
        )

        await _insert_user(db_conn, u1, "u1@dalton.test")
        await _insert_user(db_conn, u2, "u2@dalton.test")
        await _insert_workspace(db_conn, ws_active, u1, "NVDA", status="running")
        await _insert_workspace(db_conn, ws_other, u2, "NVDA", status="running")
        await _insert_workspace(db_conn, ws_deleted, u1, "AMD", status="deleted")
        await _insert_workspace(db_conn, ws_archived_unit, u1, "MSFT", status="running")

        await db_conn.execute(
            """
            INSERT INTO dalton_coverage (user_id, ticker, name, tier)
            VALUES (%s, 'NVDA', 'NVIDIA', 'core'), (%s, 'AMD', 'AMD', 'watch')
            """,
            (u1, u1),
        )
        await db_conn.execute(
            """
            INSERT INTO dalton_coverage_units (
                unit_id, kind, owner_user_id, slug, display_name, workspace_id, ticker, status
            )
            VALUES (%s::uuid, 'company', %s, 'msft', 'Microsoft', %s::uuid, 'MSFT', 'archived')
            """,
            (str(uuid.uuid4()), u1, ws_archived_unit),
        )

        for label, owner, name, ws, meta, _exp_scope, st in seeded_specs:
            aid = str(uuid.uuid4())
            id_by_label[label] = aid
            await db_conn.execute(
                """
                INSERT INTO automations (
                    automation_id, user_id, name, file_name, trigger_type,
                    cron_expression, timezone, instruction, workspace_id,
                    status, metadata, created_at, updated_at
                )
                VALUES (
                    %s::uuid, %s, %s, %s, 'cron',
                    '0 9 * * *', 'Asia/Shanghai', 'Run check', %s::uuid,
                    %s, %s::jsonb, %s, %s
                )
                """,
                (
                    aid,
                    owner,
                    name,
                    f"{label}.json",
                    ws,
                    st,
                    json.dumps(meta),
                    fixed_ts,
                    fixed_ts,
                ),
            )
            await db_conn.execute(
                "ALTER TABLE automations DISABLE TRIGGER trg_automations_updated_at"
            )
            await db_conn.execute(
                "UPDATE automations SET updated_at = %s WHERE automation_id = %s::uuid",
                (fixed_ts, aid),
            )
            await db_conn.execute(
                "ALTER TABLE automations ENABLE TRIGGER trg_automations_updated_at"
            )

        await db_conn.execute(
            """
            INSERT INTO dalton_data_task_defs (
                job_key, task_code, title, batch_no, runner, cron_expr, timezone, command_argv
            )
            VALUES ('cn-leverage-daily', 'T5', 'CN Leverage', 3, 'container', '0 18 * * 1-5', 'Asia/Shanghai', '["echo"]'::jsonb)
            """
        )

    # Upgrade d031 -> d035 (single Dalton head)
    await asyncio.to_thread(command.upgrade, dalton_cfg, "head")

    async with au1_live_pool.connection() as db_conn:
        assert await _dalton_alembic_versions(db_conn) == ["d035"]
        await _assert_d031_shared_command_columns(db_conn)

        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM dalton_notify_storage_state WHERE state_id = 1")
            notify_state = await cur.fetchone()
            assert notify_state is not None
            assert notify_state["storage_mode"] == "legacy"
            assert notify_state["vault_source"] == "legacy"
            assert notify_state["vault_source_batch_id"] is None

            await cur.execute("SELECT * FROM users WHERE user_id = %s", (COMPANY_SYSTEM_USER_ID,))
            comp_user = await cur.fetchone()
            assert comp_user is not None
            assert comp_user["email"] == COMPANY_SYSTEM_EMAIL

            for label, owner, _name, _ws, _meta, exp_scope, exp_status in seeded_specs:
                await cur.execute(
                    """
                    SELECT user_id, scope, status, notify_storage_mode, notify_routes,
                           config_version, active_version, audit_history, updated_at
                    FROM automations
                    WHERE automation_id = %s::uuid
                    """,
                    (id_by_label[label],),
                )
                row = await cur.fetchone()
                assert row is not None, label
                assert row["user_id"] == owner, label
                assert row["scope"] == exp_scope, f"{label}: got {row['scope']} != {exp_scope}"
                assert row["status"] == exp_status, label
                assert row["notify_storage_mode"] == "legacy", label
                assert row["notify_routes"] is None, label
                assert row["config_version"] == 1
                assert row["active_version"] == 1
                assert row["audit_history"] == []
                assert row["updated_at"] == fixed_ts, f"{label} updated_at mutated!"

            await cur.execute(
                """
                SELECT notify_routes, notify_storage_mode, deleted_at, migration_state
                FROM dalton_data_task_defs
                WHERE job_key = 'cn-leverage-daily'
                """
            )
            dt_row = await cur.fetchone()
            assert dt_row == {
                "notify_routes": None,
                "notify_storage_mode": "legacy",
                "deleted_at": None,
                "migration_state": {},
            }

    # Safe downgrade to d031 succeeds when no company/deleted/task_row/migration rows exist,
    # without rolling back d031 shared command columns or d030 data-task definitions!
    await asyncio.to_thread(command.downgrade, dalton_cfg, "d031")
    async with au1_live_pool.connection() as db_conn:
        assert await _dalton_alembic_versions(db_conn) == ["d031"]
        await _assert_d031_shared_command_columns(db_conn)
        assert not await _column_exists(db_conn, "automations", "scope")
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT job_key FROM dalton_data_task_defs WHERE job_key = 'cn-leverage-daily'"
            )
            assert await cur.fetchone() == {"job_key": "cn-leverage-daily"}

    await asyncio.to_thread(command.upgrade, dalton_cfg, "head")

    async with au1_live_pool.connection() as db_conn:
        assert await _dalton_alembic_versions(db_conn) == ["d035"]

        # Verify DB CHECK constraints directly on PostgreSQL:
        # 1) scope='company' with non-company user_id -> CheckViolation
        with pytest.raises(pg_errors.CheckViolation):
            await db_conn.execute(
                """
                INSERT INTO automations (
                    automation_id, user_id, name, file_name, trigger_type,
                    cron_expression, timezone, instruction, scope, notify_storage_mode
                )
                VALUES (%s::uuid, %s, 'BadOwner', 'bad-owner.json', 'cron', '0 8 * * *', 'UTC', 'x', 'company', 'task_row')
                """,
                (str(uuid.uuid4()), u1),
            )
        # 2) user_id='system:dalton-company' with scope='personal' -> CheckViolation
        with pytest.raises(pg_errors.CheckViolation):
            await db_conn.execute(
                """
                INSERT INTO automations (
                    automation_id, user_id, name, file_name, trigger_type,
                    cron_expression, timezone, instruction, scope
                )
                VALUES (%s::uuid, %s, 'BadScope', 'bad-scope.json', 'cron', '0 8 * * *', 'UTC', 'x', 'personal')
                """,
                (str(uuid.uuid4()), COMPANY_SYSTEM_USER_ID),
            )
        # 3) scope='company' with notify_storage_mode='legacy' (default or explicit) -> CheckViolation!
        with pytest.raises(pg_errors.CheckViolation):
            await db_conn.execute(
                """
                INSERT INTO automations (
                    automation_id, user_id, name, file_name, trigger_type,
                    cron_expression, timezone, instruction, scope, notify_storage_mode
                )
                VALUES (%s::uuid, %s, 'BadNotifyMode', 'bad-notify-mode.json', 'cron', '0 8 * * *', 'UTC', 'x', 'company', 'legacy')
                """,
                (str(uuid.uuid4()), COMPANY_SYSTEM_USER_ID),
            )
        # 4) Non-object JSONB in dalton_data_task_defs.migration_state -> CheckViolation
        with pytest.raises(pg_errors.CheckViolation):
            await db_conn.execute(
                """
                UPDATE dalton_data_task_defs
                SET migration_state = '["not-an-object"]'::jsonb
                WHERE job_key = 'cn-leverage-daily'
                """
            )

        # Destructive downgrade refusals:
        # A) Company task with notify_storage_mode='task_row' is accepted by DB, then blocks downgrade!
        comp_auto_id = str(uuid.uuid4())
        await db_conn.execute(
            """
            INSERT INTO automations (
                automation_id, user_id, name, file_name, trigger_type,
                cron_expression, timezone, instruction, scope, notify_storage_mode
            )
            VALUES (
                %s::uuid, %s, 'Company Job', 'company-job.json', 'cron',
                '0 9 * * *', 'UTC', 'run', 'company', 'task_row'
            )
            """,
            (comp_auto_id, COMPANY_SYSTEM_USER_ID),
        )

    with pytest.raises(RuntimeError, match="company-scoped"):
        await asyncio.to_thread(command.downgrade, dalton_cfg, "d031")

    target_id = id_by_label["personal-note"]
    dup_active_id = str(uuid.uuid4())
    async with au1_live_pool.connection() as db_conn:
        await db_conn.execute(
            "DELETE FROM automations WHERE automation_id = %s::uuid",
            (comp_auto_id,),
        )

        # B) Soft-deleted row + duplicate (user_id, file_name) allowed by partial unique index, refused on downgrade!
        await db_conn.execute(
            "UPDATE automations SET deleted_at = NOW(), deleted_by = %s WHERE automation_id = %s::uuid",
            (u1, target_id),
        )
        await db_conn.execute(
            """
            INSERT INTO automations (
                automation_id, user_id, name, file_name, trigger_type,
                cron_expression, timezone, instruction, scope
            )
            VALUES (%s::uuid, %s, 'Morning Journal 2', 'personal-note.json', 'cron', '0 9 * * *', 'UTC', 'run', 'personal')
            """,
            (dup_active_id, u1),
        )

    with pytest.raises(RuntimeError, match="duplicate \\(user_id, file_name\\)"):
        await asyncio.to_thread(command.downgrade, dalton_cfg, "d031")

    async with au1_live_pool.connection() as db_conn:
        await db_conn.execute(
            "DELETE FROM automations WHERE automation_id = %s::uuid",
            (dup_active_id,),
        )
        await db_conn.execute(
            "UPDATE automations SET deleted_at = NULL, deleted_by = NULL WHERE automation_id = %s::uuid",
            (target_id,),
        )
        # C) Row-level notify state on non-company row
        await db_conn.execute(
            "UPDATE automations SET notify_storage_mode = 'task_row' WHERE automation_id = %s::uuid",
            (target_id,),
        )

    with pytest.raises(RuntimeError, match="row-level notify state"):
        await asyncio.to_thread(command.downgrade, dalton_cfg, "d031")

    async with au1_live_pool.connection() as db_conn:
        await db_conn.execute(
            "UPDATE automations SET notify_storage_mode = 'legacy' WHERE automation_id = %s::uuid",
            (target_id,),
        )
        # D) Saved migration source identity
        await db_conn.execute(
            """
            UPDATE dalton_data_task_defs
            SET migration_state = '{"vault_source_user_id": "pm-1"}'::jsonb
            WHERE job_key = 'cn-leverage-daily'
            """
        )

    with pytest.raises(RuntimeError, match="migration_state"):
        await asyncio.to_thread(command.downgrade, dalton_cfg, "d031")

    company_comp_id = str(uuid.uuid4())
    async with au1_live_pool.connection() as db_conn:
        await db_conn.execute(
            "UPDATE dalton_data_task_defs SET migration_state = '{}'::jsonb WHERE job_key = 'cn-leverage-daily'"
        )
        # E) Company-owned computer WITHOUT any company workspace (computers.user_id ON DELETE CASCADE)
        #    MUST block downgrade so DELETE FROM users never cascade-deletes the company computer!
        await _insert_computer(
            db_conn, company_comp_id, COMPANY_SYSTEM_USER_ID, "Company Standalone Box"
        )
        async with db_conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM workspaces WHERE user_id = %s",
                (COMPANY_SYSTEM_USER_ID,),
            )
            assert (await cur.fetchone())[0] == 0

    with pytest.raises(RuntimeError, match="computers still references system:dalton-company"):
        await asyncio.to_thread(command.downgrade, dalton_cfg, "d031")

    async with au1_live_pool.connection() as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM computers WHERE computer_id = %s::uuid AND user_id = %s",
                (company_comp_id, COMPANY_SYSTEM_USER_ID),
            )
            assert (await cur.fetchone())[0] == 1
        # Removing only the isolated test computer row allows clean downgrade to d031, then re-upgrade!
        await db_conn.execute(
            "DELETE FROM computers WHERE computer_id = %s::uuid",
            (company_comp_id,),
        )

    await asyncio.to_thread(command.downgrade, dalton_cfg, "d031")
    await asyncio.to_thread(command.upgrade, dalton_cfg, "head")


# ---------------------------------------------------------------------------
# 2. Company budget idempotency, creation parity, and external auth rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_company_budget_idempotency_and_external_auth_rejection_real_db(
    au1_live_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    human_uid = "u-auth-human"
    comp_for_cli = str(uuid.uuid4())
    ws_human = str(uuid.uuid4())

    async with au1_live_pool.connection() as db_conn:
        # Seed from deployment config
        await db_conn.execute(
            "DELETE FROM dalton_budget_config WHERE user_id = %s",
            (COMPANY_SYSTEM_USER_ID,),
        )
        expected_default = get_dalton_settings().budget.default_daily_cap_usd
        await ensure_company_system_identity(conn=db_conn)
        cfg1 = await resolve_user_budget_config(db_conn, COMPANY_SYSTEM_USER_ID)
        if expected_default is not None:
            assert cfg1["daily_cap_usd"] == Decimal(str(expected_default))
            assert cfg1["configured"] is True

        # Customize DB cap, then run initialization again -> MUST NOT overwrite!
        await db_conn.execute(
            """
            INSERT INTO dalton_budget_config (user_id, daily_cap_usd, default_task_cap_usd, timezone, updated_at)
            VALUES (%s, 88.500000, 12.000000, 'Asia/Shanghai', NOW())
            ON CONFLICT (user_id) DO UPDATE SET daily_cap_usd = EXCLUDED.daily_cap_usd
            """,
            (COMPANY_SYSTEM_USER_ID,),
        )
        res_second = await ensure_company_system_identity(
            conn=db_conn,
            budget_settings=DaltonBudgetDefaults(
                default_daily_cap_usd=250.0,
                default_task_cap_usd=25.0,
                timezone="UTC",
            ),
        )
        assert res_second["budget_inserted"] is False
        cfg2 = await resolve_user_budget_config(db_conn, COMPANY_SYSTEM_USER_ID)
        assert cfg2["daily_cap_usd"] == Decimal("88.500000")

        # Missing default_daily_cap_usd with no DB row fails closed to $0 / configured=False.
        await db_conn.execute(
            "DELETE FROM dalton_budget_config WHERE user_id = %s",
            (COMPANY_SYSTEM_USER_ID,),
        )
        none_budget = DaltonBudgetDefaults(
            default_daily_cap_usd=None,
            default_task_cap_usd=None,
            timezone="Asia/Shanghai",
        )
        await ensure_company_system_identity(conn=db_conn, budget_settings=none_budget)

        class _NoDefaultSettings:
            budget = none_budget

        monkeypatch.setattr(
            "src.dalton.budget.service.get_dalton_settings",
            lambda: _NoDefaultSettings(),
        )
        cfg_closed = await resolve_user_budget_config(db_conn, COMPANY_SYSTEM_USER_ID)
        assert cfg_closed["daily_cap_usd"] == Decimal("0")
        assert cfg_closed["configured"] is False

        await _insert_user(db_conn, human_uid, "human@dalton.test")
        await _insert_computer(db_conn, comp_for_cli, human_uid, "CLI Probe")
        await _insert_workspace(
            db_conn,
            ws_human,
            human_uid,
            "Human WS",
            status="running",
            computer_id=comp_for_cli,
        )
        await db_conn.execute(
            """
            INSERT INTO automations (
                automation_id, user_id, name, file_name, trigger_type,
                cron_expression, timezone, instruction, workspace_id, scope
            )
            VALUES (
                %s::uuid, %s, 'Human Brief', 'human-brief.json', 'cron',
                '0 8 * * *', 'Asia/Shanghai', 'Summarize', %s::uuid, 'personal'
            )
            """,
            (str(uuid.uuid4()), human_uid, ws_human),
        )

    # External HTTP & WS auth refuses system:dalton-company while allowing human user.
    monkeypatch.setattr(api_utils, "_SERVICE_TOKEN", "t" * 40)
    monkeypatch.setattr(
        dalton_auth,
        "is_dalton_supabase_auth_enabled",
        create_autospec(dalton_auth.is_dalton_supabase_auth_enabled, return_value=False),
    )

    app = FastAPI()
    app.include_router(automations_app.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.get(
            "/api/v1/automations",
            headers={
                "X-Service-Token": "t" * 40,
                "X-User-Id": COMPANY_SYSTEM_USER_ID,
            },
        )
        assert refused.status_code == 401

        allowed = await client.get(
            "/api/v1/automations",
            headers={
                "X-Service-Token": "t" * 40,
                "X-User-Id": human_uid,
            },
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["total"] == 1

        # PAT, OAuth, and computer CLI tokens naming system:dalton-company are also rejected (401).
        class _PatObj:
            def __init__(self, uid: str) -> None:
                self.user_id = uid
                self.scopes = ("agent:read",)

        monkeypatch.setattr(
            pats,
            "is_pat",
            create_autospec(pats.is_pat, side_effect=lambda tok: tok.startswith("lapat_")),
        )
        monkeypatch.setattr(
            pats,
            "verify_pat",
            create_autospec(
                pats.verify_pat,
                side_effect=lambda tok: _PatObj(
                    COMPANY_SYSTEM_USER_ID if "company" in tok else human_uid
                ),
            ),
        )
        assert (
            await client.get(
                "/api/v1/automations", headers={"Authorization": "Bearer lapat_company"}
            )
        ).status_code == 401
        pat_ok = await client.get(
            "/api/v1/automations", headers={"Authorization": "Bearer lapat_human"}
        )
        assert pat_ok.status_code == 200, pat_ok.text

        class _OAuthObj:
            def __init__(self, uid: str) -> None:
                self.user_id = uid
                self.client_id = "c1"
                self.scopes = ("agent:read",)

        monkeypatch.setattr(
            oauth_resource,
            "from_resource_issuer",
            create_autospec(
                oauth_resource.from_resource_issuer,
                side_effect=lambda tok: tok.startswith("oauth_"),
            ),
        )
        monkeypatch.setattr(
            oauth_resource,
            "verify_access_token",
            create_autospec(
                oauth_resource.verify_access_token,
                side_effect=lambda tok: _OAuthObj(
                    COMPANY_SYSTEM_USER_ID if "company" in tok else human_uid
                ),
            ),
        )
        assert (
            await client.get(
                "/api/v1/automations", headers={"Authorization": "Bearer oauth_company"}
            )
        ).status_code == 401

        class _CliObj:
            def __init__(self, uid: str) -> None:
                self.user_id = uid
                self.scopes = ("agent:read",)
                self.computer_id = comp_for_cli

        monkeypatch.setattr(
            "src.dalton.settings.is_computer_cli_tokens_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            cli_tokens,
            "is_cli_token",
            create_autospec(
                cli_tokens.is_cli_token,
                side_effect=lambda tok: tok.startswith("lac1."),
            ),
        )
        monkeypatch.setattr(
            cli_tokens,
            "authenticate",
            create_autospec(
                cli_tokens.authenticate,
                side_effect=lambda tok: _CliObj(
                    COMPANY_SYSTEM_USER_ID if "company" in tok else human_uid
                ),
            ),
        )
        assert (
            await client.get(
                "/api/v1/automations", headers={"Authorization": "Bearer lac1.company"}
            )
        ).status_code == 401
        cli_ok = await client.get(
            "/api/v1/automations", headers={"Authorization": "Bearer lac1.human"}
        )
        assert cli_ok.status_code == 200, cli_ok.text

        # Session Bearer JWT via real jwt_bearer._decode_token / api_utils._decode_token
        monkeypatch.setattr(api_utils, "HOST_MODE", "saas")
        monkeypatch.setattr(jwt_bearer, "HOST_MODE", "saas")
        monkeypatch.setattr(
            jwt_bearer.jwt,
            "get_unverified_header",
            create_autospec(
                jwt_bearer.jwt.get_unverified_header,
                return_value={"kid": "real-db-kid"},
            ),
        )
        monkeypatch.setattr(
            jwt_bearer._signing_keys,
            "get",
            create_autospec(jwt_bearer._signing_keys.get, return_value="fake-jwk-key"),
        )
        monkeypatch.setattr(
            jwt_bearer.jwt,
            "decode",
            create_autospec(
                jwt_bearer.jwt.decode,
                side_effect=lambda tok, *a, **kw: {
                    "sub": COMPANY_SYSTEM_USER_ID if "company" in tok else human_uid,
                    "app_metadata": {"provider": "email"},
                },
            ),
        )
        assert (
            await client.get(
                "/api/v1/automations", headers={"Authorization": "Bearer jwt_company"}
            )
        ).status_code == 401
        jwt_ok = await client.get(
            "/api/v1/automations", headers={"Authorization": "Bearer jwt_human"}
        )
        assert jwt_ok.status_code == 200, jwt_ok.text

    monkeypatch.setattr(ws_auth, "HOST_MODE", "saas")
    ws = create_autospec(WebSocket, instance=True)
    ws.headers = {"authorization": "Bearer jwt_company"}
    ws.query_params = {}
    with pytest.raises(WebSocketException) as ws_exc:
        await ws_auth.authenticate_websocket(ws)
    assert ws_exc.value.code == 1008

    ws_human_sock = create_autospec(WebSocket, instance=True)
    ws_human_sock.headers = {"authorization": "Bearer jwt_human"}
    ws_human_sock.query_params = {}
    assert await ws_auth.authenticate_websocket(ws_human_sock) == human_uid


@pytest.mark.asyncio
async def test_creation_paths_match_sql_classification_and_keep_director_personal(
    au1_live_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "u-create-parity"
    ws_cov = str(uuid.uuid4())
    ws_plain = str(uuid.uuid4())
    home_id = get_home_workspace_id(uid)

    async with au1_live_pool.connection() as db_conn:
        await _insert_user(db_conn, uid, "analyst@dalton.test")
        await _insert_workspace(db_conn, home_id, uid, "Home", status="running")
        await _insert_workspace(db_conn, ws_cov, uid, "TSLA", status="running")
        await _insert_workspace(db_conn, ws_plain, uid, "Scratch", status="running")
        await db_conn.execute(
            "INSERT INTO dalton_coverage (user_id, ticker, name, tier) VALUES (%s, 'TSLA', 'Tesla', 'core')",
            (uid,),
        )
        await db_conn.execute(
            """
            INSERT INTO dalton_coverage_units (
                unit_id, kind, owner_user_id, slug, display_name, workspace_id, ticker, status
            )
            VALUES (%s::uuid, 'company', %s, 'tsla', 'Tesla', %s::uuid, 'TSLA', 'active')
            """,
            (str(uuid.uuid4()), uid, ws_cov),
        )

    monkeypatch.setattr(
        "src.dalton.autonomy.system_jobs.is_pm_user",
        lambda u: u == uid,
    )

    # 1. Lifecycle create: personal task in plain workspace -> personal (legacy notify mode)
    c_personal = await lifecycle_mod.create_automation(
        uid,
        AutomationCreate(
            name="My Daily Summary",
            trigger_type="cron",
            cron_expression="0 8 * * *",
            instruction="Summarize",
            workspace_id=uuid.UUID(ws_plain),
        ),
    )
    assert c_personal["scope"] == "personal"
    assert c_personal["notify_storage_mode"] == "legacy"
    assert c_personal["created_by"] == uid

    # 2. Lifecycle create: custom dalton-tsla-* in active coverage workspace -> research_tracking
    c_custom_rt = await lifecycle_mod.create_automation(
        uid,
        AutomationCreate(
            name="dalton-tsla-delivery-tracker",
            trigger_type="cron",
            cron_expression="0 10 * * *",
            instruction="Track deliveries",
            workspace_id=uuid.UUID(ws_cov),
        ),
    )
    assert c_custom_rt["scope"] == "research_tracking"
    assert c_custom_rt["notify_storage_mode"] == "legacy"

    # 3. Scheduler create: dalton-earnings-calendar -> research_tracking
    res_cal = await schedule_mod_call(
        uid=uid,
        name="dalton-earnings-calendar",
        workspace_id=home_id,
        metadata={"dalton_kind": "calendar"},
    )
    assert res_cal["scope"] == "research_tracking"

    # 4. Scheduler create: dalton-director-daily & dalton-data-health stay personal!
    res_dir = await schedule_mod_call(
        uid=uid,
        name="dalton-director-daily",
        workspace_id=home_id,
        metadata={"dalton_kind": "director_daily"},
    )
    assert res_dir["scope"] == "personal"

    res_health = await schedule_mod_call(
        uid=uid,
        name="dalton-data-health",
        workspace_id=home_id,
        metadata={"dalton_kind": "data_health"},
    )
    assert res_health["scope"] == "personal"

    # Verify classify_automation_scope agrees on all created rows.
    for created_row, ws_obj, unit_obj in (
        (c_personal, {"workspace_id": ws_plain, "user_id": uid, "status": "running"}, None),
        (
            c_custom_rt,
            {"workspace_id": ws_cov, "user_id": uid, "status": "running"},
            {"workspace_id": ws_cov, "owner_user_id": uid, "kind": "company", "status": "active"},
        ),
        (res_cal, {"workspace_id": home_id, "user_id": uid, "status": "running"}, None),
        (res_dir, {"workspace_id": home_id, "user_id": uid, "status": "running"}, None),
        (res_health, {"workspace_id": home_id, "user_id": uid, "status": "running"}, None),
    ):
        assert created_row["scope"] == classify_automation_scope(
            created_row,
            ws_obj,
            coverage_unit=unit_obj,
            covered_tickers=["TSLA"],
        )


async def schedule_mod_call(
    *,
    uid: str,
    name: str,
    workspace_id: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    out = await schedule_dalton_automation_helper(
        user_id=uid,
        name=name,
        workspace_id=workspace_id,
        spec={
            "instruction": "Run scheduled job",
            "cron": "0 8 * * *",
            "timezone": "Asia/Shanghai",
            "metadata": metadata,
        },
    )
    assert out["ok"] is True
    row = await auto_db.get_automation(out["automation_id"], uid)
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# 3. Real HTTP GET /api/v1/automations & GET /history verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_scoped_listing_computer_isolation_and_readonly_history_real_db(
    au1_live_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    internal_uid = "u-http-internal"
    external_uid = "u-http-external"
    other_uid = "u-http-other"
    comp_a = str(uuid.uuid4())
    comp_b = str(uuid.uuid4())
    ws_a = str(uuid.uuid4())
    ws_b = str(uuid.uuid4())
    ws_company = str(uuid.uuid4())
    home_internal = get_home_workspace_id(internal_uid)

    same_created = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
    auto_home_id = "10000000-0000-4000-8000-000000000001"
    auto_comp_a_1 = "20000000-0000-4000-8000-000000000001"
    auto_comp_a_2 = "20000000-0000-4000-8000-000000000002"
    auto_comp_b_rt = "30000000-0000-4000-8000-000000000001"
    comp_preset_id = "90000000-0000-4000-8000-000000000001"

    async with au1_live_pool.connection() as db_conn:
        await _insert_user(db_conn, internal_uid, "internal@dalton.test")
        await _insert_user(db_conn, external_uid, "external@guest.test")
        await _insert_user(db_conn, other_uid, "other@dalton.test")

        await _insert_computer(db_conn, comp_a, internal_uid, "Comp A")
        await _insert_computer(db_conn, comp_b, internal_uid, "Comp B")
        await _insert_workspace(
            db_conn, home_internal, internal_uid, "Home", status="running", computer_id=comp_a
        )
        await _insert_workspace(
            db_conn, ws_a, internal_uid, "WS A", status="running", computer_id=comp_a
        )
        await _insert_workspace(
            db_conn, ws_b, internal_uid, "WS B", status="running", computer_id=comp_b
        )
        await _insert_workspace(
            db_conn, ws_company, COMPANY_SYSTEM_USER_ID, "Company WS", status="running"
        )

        for aid, name, ws, sc in (
            (auto_home_id, "Home Personal", home_internal, "personal"),
            (auto_comp_a_1, "Comp A Personal 1", ws_a, "personal"),
            (auto_comp_a_2, "Comp A Personal 2", ws_a, "personal"),
            (auto_comp_b_rt, "dalton-nvda-watch", ws_b, "research_tracking"),
        ):
            await db_conn.execute(
                """
                INSERT INTO automations (
                    automation_id, user_id, name, file_name, trigger_type,
                    cron_expression, timezone, instruction, workspace_id,
                    scope, created_by, created_at, updated_at
                )
                VALUES (
                    %s::uuid, %s, %s, %s, 'cron',
                    '0 9 * * *', 'Asia/Shanghai', 'Run', %s::uuid,
                    %s, %s, %s, %s
                )
                """,
                (
                    aid,
                    internal_uid,
                    name,
                    f"{aid}.json",
                    ws,
                    sc,
                    internal_uid,
                    same_created,
                    same_created,
                ),
            )

        # Seed caller personal notify preferences to prove company list never reads/applies them.
        await db_conn.execute(
            """
            INSERT INTO user_preferences (user_id, model_preference)
            VALUES (%s, %s::jsonb)
            """,
            (
                internal_uid,
                json.dumps(
                    {
                        "dalton_notify_overrides": {f"automation:{comp_preset_id}": True},
                        "dalton_notify_routes": {
                            f"automation:{comp_preset_id}": ["feishu:chat:oc_personal_only"]
                        },
                    }
                ),
            ),
        )

    monkeypatch.setattr(
        access_tiers,
        "get_dalton_allowed_emails",
        create_autospec(
            access_tiers.get_dalton_allowed_emails,
            return_value=frozenset({"internal@dalton.test", "other@dalton.test"}),
        ),
    )

    caller_holder = {"principal": principal_mod.Principal(user_id=internal_uid, auth="session")}
    app = FastAPI()
    app.include_router(automations_app.router)

    # Exercise real require_scopes(_READ) with controlled get_principal for session vs computer token!
    app.dependency_overrides[principal_mod.get_principal] = lambda: caller_holder["principal"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Empty company list before company rows are preset -> 200, total=0
        r_empty_comp = await client.get("/api/v1/automations?scope=company")
        assert r_empty_comp.status_code == 200, r_empty_comp.text
        assert r_empty_comp.json() == {"automations": [], "total": 0}

        # 2. Omitted scope returns all 4 tasks of internal_uid (never defaults to personal)
        r_all = await client.get("/api/v1/automations")
        assert r_all.status_code == 200, r_all.text
        assert r_all.json()["total"] == 4

        # 3. Deterministic tie-breaker pagination on equal created_at (automation_id DESC)
        p1 = await client.get("/api/v1/automations?scope=personal&limit=2&offset=0")
        p2 = await client.get("/api/v1/automations?scope=personal&limit=2&offset=2")
        assert p1.status_code == 200, p1.text
        assert p2.status_code == 200, p2.text
        assert p1.json()["total"] == 3 and p2.json()["total"] == 3
        ordered_ids = [
            x["automation_id"] for x in p1.json()["automations"] + p2.json()["automations"]
        ]
        assert ordered_ids == [auto_comp_a_2, auto_comp_a_1, auto_home_id]
        assert all(
            x["notify_storage_mode"] == "legacy"
            for x in p1.json()["automations"] + p2.json()["automations"]
        )

        # 4. Preset a company row using required notify_storage_mode='task_row' and audit_history
        preset_history = [
            {
                "version": 1,
                "prev_version": None,
                "actor_user_id": internal_uid,
                "changed_at": "2026-11-08T10:00:00+00:00",
                "action": "seed",
                "changes": {},
                "snapshot": {
                    "name": "AlphaEngine Company Job",
                    "desired_status": "active",
                    "trigger_type": "cron",
                    "cron_expression": "0 9 * * *",
                    "timezone": "Asia/Shanghai",
                    "instruction": "Collect AlphaEngine v1",
                    "workspace_id": ws_company,
                    "notify_enabled": False,
                    "notify_routes": ["feishu:chat:oc_company_group"],
                    "delivery_config": {"methods": [], "webhook_secret": "TOP_SECRET"},
                    "metadata": {"pre_company_migration": {"user_id": internal_uid}},
                    "created_secrets": ["FEISHU_APP_SECRET"],
                },
            },
            {
                "version": 2,
                "prev_version": 1,
                "actor_user_id": other_uid,
                "changed_at": "2026-11-09T10:00:00+00:00",
                "action": "update",
                "changes": {
                    "cron_expression": {"old": "0 9 * * *", "new": "0 9,18 * * *"},
                    "metadata": {"old": {}, "new": {"leak": "no"}},
                },
                "snapshot": {
                    "name": "AlphaEngine Company Job",
                    "desired_status": "active",
                    "trigger_type": "cron",
                    "cron_expression": "0 9,18 * * *",
                    "timezone": "Asia/Shanghai",
                    "instruction": "Collect AlphaEngine v2",
                    "workspace_id": ws_company,
                    "notify_enabled": False,
                    "notify_routes": ["feishu:chat:oc_company_group"],
                    "delivery_config": {},
                },
            },
        ]
        async with au1_live_pool.connection() as db_conn:
            await db_conn.execute(
                """
                INSERT INTO automations (
                    automation_id, user_id, name, file_name, trigger_type,
                    cron_expression, timezone, instruction, workspace_id,
                    scope, notify_enabled, notify_routes, notify_storage_mode,
                    config_version, active_version,
                    created_by, updated_by, audit_history, created_at, updated_at
                )
                VALUES (
                    %s::uuid, %s, 'AlphaEngine Company Job', 'alphaengine.json', 'cron',
                    '0 9,18 * * *', 'Asia/Shanghai', 'Collect AlphaEngine v2', %s::uuid,
                    'company', FALSE, '["feishu:chat:oc_company_group"]'::jsonb, 'task_row',
                    2, 2,
                    %s, %s, %s::jsonb, %s, %s
                )
                """,
                (
                    comp_preset_id,
                    COMPANY_SYSTEM_USER_ID,
                    ws_company,
                    internal_uid,
                    other_uid,
                    json.dumps(preset_history),
                    same_created,
                    same_created,
                ),
            )

        # Internal member reads scope=company: gets row's task_row notify state, NOT caller prefs!
        r_comp = await client.get("/api/v1/automations?scope=company")
        assert r_comp.status_code == 200, r_comp.text
        assert r_comp.json()["total"] == 1
        [comp_item] = r_comp.json()["automations"]
        assert comp_item["automation_id"] == comp_preset_id
        assert comp_item["scope"] == "company"
        assert comp_item["engine_kind"] == "agent"
        assert comp_item["config_version"] == 2
        assert comp_item["active_version"] == 2
        assert comp_item["created_by"] == internal_uid
        assert comp_item["updated_by"] == other_uid
        assert comp_item["notify_storage_mode"] == "task_row"
        assert comp_item["dalton_notify_effective"] is False
        assert comp_item["dalton_notify_source"] == "task_row"
        assert comp_item["dalton_notify_route_targets"] == ["feishu:chat:oc_company_group"]

        # 5. History read-only check: snapshot DB state BEFORE and AFTER GET /history!
        async with au1_live_pool.connection() as db_conn:
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT config_version, active_version, audit_history, updated_at FROM automations WHERE automation_id = %s::uuid",
                    (auto_comp_a_1,),
                )
                before_empty = await cur.fetchone()
                await cur.execute("SELECT COUNT(*) AS cnt FROM workspaces")
                ws_cnt_before = (await cur.fetchone())["cnt"]
                await cur.execute("SELECT COUNT(*) AS cnt FROM automation_executions")
                exec_cnt_before = (await cur.fetchone())["cnt"]
                await cur.execute(
                    "SELECT model_preference FROM user_preferences WHERE user_id = %s",
                    (internal_uid,),
                )
                prefs_before = await cur.fetchone()

        r_hist_empty = await client.get(f"/api/v1/automations/{auto_comp_a_1}/history")
        assert r_hist_empty.status_code == 200, r_hist_empty.text
        assert r_hist_empty.json()["entries"] == []

        r_hist_comp = await client.get(f"/api/v1/automations/{comp_preset_id}/history")
        assert r_hist_comp.status_code == 200, r_hist_comp.text
        hist_body = r_hist_comp.json()
        assert [e["version"] for e in hist_body["entries"]] == [2, 1]
        for entry in hist_body["entries"]:
            assert set(entry["snapshot"].keys()) <= company_auto_svc.ALLOWED_AUTOMATION_SNAPSHOT_FIELD_SET
            assert "metadata" not in entry["snapshot"]
            assert "created_secrets" not in entry["snapshot"]
            assert "webhook_secret" not in entry["snapshot"].get("delivery_config", {})
            assert "metadata" not in entry["changes"]

        async with au1_live_pool.connection() as db_conn:
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT config_version, active_version, audit_history, updated_at FROM automations WHERE automation_id = %s::uuid",
                    (auto_comp_a_1,),
                )
                after_empty = await cur.fetchone()
                await cur.execute("SELECT COUNT(*) AS cnt FROM workspaces")
                ws_cnt_after = (await cur.fetchone())["cnt"]
                await cur.execute("SELECT COUNT(*) AS cnt FROM automation_executions")
                exec_cnt_after = (await cur.fetchone())["cnt"]
                await cur.execute(
                    "SELECT model_preference FROM user_preferences WHERE user_id = %s",
                    (internal_uid,),
                )
                prefs_after = await cur.fetchone()

        assert after_empty == before_empty
        assert ws_cnt_after == ws_cnt_before
        assert exec_cnt_after == exec_cnt_before
        assert prefs_after == prefs_before

        # 6. External member: 403 on scope=company and company /history, 403 on cross-user /history
        caller_holder["principal"] = principal_mod.Principal(
            user_id=external_uid, auth="session"
        )
        assert (await client.get("/api/v1/automations?scope=company")).status_code == 403
        assert (
            await client.get(f"/api/v1/automations/{comp_preset_id}/history")
        ).status_code == 403
        assert (
            await client.get(f"/api/v1/automations/{auto_comp_a_1}/history")
        ).status_code == 403

        # 7. Computer token on Comp A:
        #    - excludes Home (auto_home_id) and Comp B (auto_comp_b_rt)
        #    - gets 403 on scope=company
        #    - reads history on Comp A (200), refused on Home (403), Comp B (403), and company (403)!
        caller_holder["principal"] = principal_mod.Principal(
            user_id=internal_uid,
            auth="computer",
            scopes=("agent:read",),
            computer_id=comp_a,
        )
        r_comp_tok_pers = await client.get("/api/v1/automations?scope=personal")
        assert r_comp_tok_pers.status_code == 200, r_comp_tok_pers.text
        assert r_comp_tok_pers.json()["total"] == 2
        assert {a["automation_id"] for a in r_comp_tok_pers.json()["automations"]} == {
            auto_comp_a_1,
            auto_comp_a_2,
        }

        r_comp_tok_rt = await client.get("/api/v1/automations?scope=research_tracking")
        assert r_comp_tok_rt.status_code == 200, r_comp_tok_rt.text
        assert r_comp_tok_rt.json() == {"automations": [], "total": 0}

        assert (await client.get("/api/v1/automations?scope=company")).status_code == 403
        r_comp_tok_hist_ok = await client.get(f"/api/v1/automations/{auto_comp_a_1}/history")
        assert r_comp_tok_hist_ok.status_code == 200, r_comp_tok_hist_ok.text
        assert (
            await client.get(f"/api/v1/automations/{auto_home_id}/history")
        ).status_code == 403
        assert (
            await client.get(f"/api/v1/automations/{auto_comp_b_rt}/history")
        ).status_code == 403
        assert (
            await client.get(f"/api/v1/automations/{comp_preset_id}/history")
        ).status_code == 403


# ---------------------------------------------------------------------------
# 4. R4 Real PostgreSQL + ASGI Channel Gateway company-identity refusal
#    and human session / unlinked / workspace-revocation preservation
# ---------------------------------------------------------------------------


async def _channel_table_counts(conn: psycopg.AsyncConnection) -> dict[str, int]:
    tables = (
        "dalton_channel_identities",
        "dalton_channel_bind_codes",
        "dalton_channel_user_settings",
        "dalton_channel_chat_bindings",
        "dalton_channel_session_generations",
        "dalton_channel_thread_watches",
        "dalton_channel_inbound_events",
        "dalton_channel_background_runs",
        "dalton_channel_deliveries",
        "dalton_channel_automation_runs",
        "dalton_channel_weixin_state",
    )
    counts: dict[str, int] = {}
    async with conn.cursor() as cur:
        for tbl in tables:
            await cur.execute(f"SELECT COUNT(*) FROM {tbl}")
            counts[tbl] = int((await cur.fetchone())[0])
    return counts


async def _raw_asgi_request_status(
    app: FastAPI,
    *,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]],
    body: bytes = b"",
) -> int:
    sent_events: list[dict[str, Any]] = []
    body_delivered = False

    async def _receive() -> dict[str, Any]:
        nonlocal body_delivered
        if not body_delivered:
            body_delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def _send(message: dict[str, Any]) -> None:
        sent_events.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
        },
        _receive,
        _send,
    )
    return int(sent_events[0]["status"])


@pytest.mark.asyncio
async def test_channels_endpoints_reject_company_identity_and_preserve_human_flows_real_db(
    au1_live_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc_token = "k" * 40
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", svc_token)
    monkeypatch.setattr(api_utils, "_SERVICE_TOKEN", svc_token)
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setenv(
        "DALTON_ALLOWED_EMAILS",
        "chan-human@dalton.test,chan-colleague@dalton.test",
    )
    monkeypatch.setenv("DALTON_COLLEAGUE_DISPATCH_ENABLED", "1")

    human_uid = "u-chan-human"
    colleague_uid = "u-chan-colleague"
    ws_colleague = str(uuid.uuid4())
    home_human = get_home_workspace_id(human_uid)

    async with au1_live_pool.connection() as db_conn:
        await _insert_user(db_conn, human_uid, "chan-human@dalton.test")
        await _insert_user(db_conn, colleague_uid, "chan-colleague@dalton.test")
        await _insert_workspace(db_conn, home_human, human_uid, "Home", status="running")
        await _insert_workspace(
            db_conn, ws_colleague, colleague_uid, "NVDA Colleague", status="running"
        )
        before_counts = await _channel_table_counts(db_conn)

    await dalton_sharing.upsert_workspace_sharing(
        ws_colleague,
        cos_access="rw",
        web_access="rw",
        updated_by=colleague_uid,
    )

    app = FastAPI()
    app.include_router(ch_router_mod.channels_router, prefix="/api/v1/dalton")
    svc_headers = {"X-Service-Token": svc_token}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Exact required probe: cmd="new", platform="feishu", dalton_user_id="system:dalton-company",
        #    scope_key="dm:test", omitting message_id and platform_user_id -> 401 and ZERO DB writes!
        r_exact = await client.post(
            "/api/v1/dalton/channels/internal/session/command",
            headers=svc_headers,
            json={
                "cmd": "new",
                "platform": "feishu",
                "dalton_user_id": COMPANY_SYSTEM_USER_ID,
                "scope_key": "dm:test",
            },
        )
        assert r_exact.status_code == 401

        # 2a. Body/query identity variants (exact + mixed case + ASCII/Unicode whitespace) across endpoints
        for sys_variant in (
            COMPANY_SYSTEM_USER_ID,
            " \tSyStEm:DaLtOn-CoMpAnY\n\u3000",
        ):
            assert (
                await client.post(
                    "/api/v1/dalton/channels/internal/session/command",
                    headers=svc_headers,
                    json={
                        "cmd": "new",
                        "platform": "feishu",
                        "dalton_user_id": sys_variant,
                        "scope_key": "dm:test",
                    },
                )
            ).status_code == 401
            assert (
                await client.post(
                    "/api/v1/dalton/channels/internal/session/command",
                    headers=svc_headers,
                    json={
                        "cmd": "workspace_list",
                        "platform": "feishu",
                        "dalton_user_id": sys_variant,
                        "scope_key": "dm:test",
                        "is_internal_query": True,
                    },
                )
            ).status_code == 401
            assert (
                await client.post(
                    "/api/v1/dalton/channels/internal/bind-code/consume",
                    headers=svc_headers,
                    json={"action": "unbind", "platform": "feishu", "user_id": sys_variant},
                )
            ).status_code == 401
            assert (
                await client.post(
                    "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
                    headers=svc_headers,
                    json={
                        "delivery_key": "tool:comp:1",
                        "kind": "agent_send",
                        "user_id": sys_variant,
                        "target": "feishu:@me",
                    },
                )
            ).status_code == 401
            assert (
                await client.get(
                    "/api/v1/dalton/channels/internal/state",
                    headers=svc_headers,
                    params={"user_id": sys_variant},
                )
            ).status_code == 401
            assert (
                await client.put(
                    "/api/v1/dalton/channels/internal/settings",
                    headers=svc_headers,
                    json={"user_id": sys_variant, "settings": {}},
                )
            ).status_code == 401
            assert (
                await client.post(
                    "/api/v1/dalton/channels/internal/automation-runs",
                    headers=svc_headers,
                    json={
                        "execution_id": str(uuid.uuid4()),
                        "user_id": sys_variant,
                        "entries": ["feishu"],
                    },
                )
            ).status_code == 401

        # 2b. X-User-Id header variants (exact + mixed case via httpx, plus raw ASGI header with whitespace)
        for hdr_variant in (COMPANY_SYSTEM_USER_ID, "SyStEm:DaLtOn-CoMpAnY"):
            assert (
                await client.post(
                    "/api/v1/dalton/channels/internal/session/command",
                    headers={**svc_headers, "X-User-Id": hdr_variant},
                    json={"cmd": "new", "platform": "feishu", "scope_key": "dm:test"},
                )
            ).status_code == 401
            assert (
                await client.post(
                    "/api/v1/dalton/channels/internal/bind-code/consume",
                    headers={**svc_headers, "X-User-Id": hdr_variant},
                    json={"action": "unbind", "platform": "feishu"},
                )
            ).status_code == 401
            assert (
                await client.get(
                    "/api/v1/dalton/channels/internal/state",
                    headers={**svc_headers, "X-User-Id": hdr_variant},
                )
            ).status_code == 401

        assert (
            await _raw_asgi_request_status(
                app,
                method="POST",
                path="/api/v1/dalton/channels/internal/session/command",
                headers=[
                    (b"content-type", b"application/json"),
                    (b"x-service-token", svc_token.encode("ascii")),
                    (b"x-user-id", b" \tSyStEm:DaLtOn-CoMpAnY\t "),
                ],
                body=b'{"cmd":"new","platform":"feishu","scope_key":"dm:test"}',
            )
            == 401
        )
        assert (
            await _raw_asgi_request_status(
                app,
                method="GET",
                path="/api/v1/dalton/channels/internal/state",
                headers=[
                    (b"x-service-token", svc_token.encode("ascii")),
                    (b"x-user-id", b" \tSyStEm:DaLtOn-CoMpAnY\t "),
                ],
            )
            == 401
        )

        async with au1_live_pool.connection() as db_conn:
            assert await _channel_table_counts(db_conn) == before_counts

        # 3. Seed DB-level platform binding, bind code, and inbound record associated with system:dalton-company
        #    (testing both canonical and mixed-case/whitespace DB values) to verify real DB resolution paths
        #    reject BEFORE creating or mutating any rows!
        for db_sys_uid in (COMPANY_SYSTEM_USER_ID, " \tSyStEm:DaLtOn-CoMpAnY\n\u3000"):
            comp_code_plain = "DLT-COMP-0001"
            comp_code_hash = hash_bind_code(comp_code_plain)
            comp_in_req_key = str(uuid.uuid4())
            async with au1_live_pool.connection() as db_conn:
                await db_conn.execute(
                    """
                    INSERT INTO dalton_channel_identities (
                        user_id, platform, platform_user_id, platform_username
                    )
                    VALUES (%s, 'feishu', 'ou_company_bound', 'comp-bot')
                    """,
                    (db_sys_uid,),
                )
                await db_conn.execute(
                    """
                    INSERT INTO dalton_channel_bind_codes (
                        code_hash, user_id, platform, expires_at
                    )
                    VALUES (%s, %s, 'feishu', NOW() + INTERVAL '10 minutes')
                    """,
                    (comp_code_hash, db_sys_uid),
                )
                await db_conn.execute(
                    """
                    INSERT INTO dalton_channel_inbound_events (
                        platform, message_id, request_key, dalton_user_id,
                        chat_type, chat_address, thread_scope_key, run_id,
                        status, raw_payload
                    )
                    VALUES (
                        'feishu', 'om_company_inbound', %s::uuid, %s,
                        'dm', 'feishu:@me', 'dm:ou_company_bound', 'run-company-01',
                        'accepted', '{"platform_user_id": "ou_company_bound"}'::jsonb
                    )
                    """,
                    (comp_in_req_key, db_sys_uid),
                )
                seeded_counts = await _channel_table_counts(db_conn)

            # Platform binding lookup -> 401 across inbound, session/command, and unbind (no user_id in request!)
            assert (
                await client.post(
                    "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
                    headers=svc_headers,
                    json={
                        "platform": "feishu",
                        "platform_user_id": "ou_company_bound",
                        "chat_type": "dm",
                        "chat_address": "feishu:@me",
                        "thread_scope_key": "dm:ou_company_bound",
                        "message_id": "om_company_new_try",
                    },
                )
            ).status_code == 401
            assert (
                await client.post(
                    "/api/v1/dalton/channels/internal/session/command",
                    headers=svc_headers,
                    json={
                        "cmd": "new",
                        "platform": "feishu",
                        "platform_user_id": "ou_company_bound",
                        "scope_key": "dm:ou_company_bound",
                    },
                )
            ).status_code == 401
            assert (
                await client.post(
                    "/api/v1/dalton/channels/internal/bind-code/consume",
                    headers=svc_headers,
                    json={
                        "action": "unbind",
                        "platform": "feishu",
                        "platform_user_id": "ou_company_bound",
                    },
                )
            ).status_code == 401

            # Bind code lookup -> 401 on both group leak and DM consume; used_at remains NULL!
            assert (
                await client.post(
                    "/api/v1/dalton/channels/internal/bind-code/consume",
                    headers=svc_headers,
                    json={
                        "action": "bind",
                        "platform": "feishu",
                        "code": comp_code_plain,
                        "is_group": True,
                    },
                )
            ).status_code == 401
            assert (
                await client.post(
                    "/api/v1/dalton/channels/internal/bind-code/consume",
                    headers=svc_headers,
                    json={
                        "action": "bind",
                        "platform": "feishu",
                        "platform_user_id": "ou_human_try",
                        "code": comp_code_plain,
                        "is_group": False,
                    },
                )
            ).status_code == 401

            # Existing inbound record lookup -> 401 across command replay, deliveries claim, inbound progress, steer reconcile!
            assert (
                await client.post(
                    "/api/v1/dalton/channels/internal/session/command",
                    headers=svc_headers,
                    json={
                        "cmd": "stop",
                        "platform": "feishu",
                        "platform_user_id": "ou_company_bound",
                        "scope_key": "dm:ou_company_bound",
                        "message_id": "om_company_inbound",
                    },
                )
            ).status_code == 401
            assert (
                await client.post(
                    "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
                    headers=svc_headers,
                    json={
                        "delivery_key": "notice:comp:1",
                        "kind": "notice",
                        "platform": "feishu",
                        "inbound_message_id": "om_company_inbound",
                    },
                )
            ).status_code == 401
            assert (
                await client.post(
                    "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
                    headers=svc_headers,
                    json={
                        "delivery_key": "fallback:run-company-01",
                        "kind": "fallback",
                        "run_id": "run-company-01",
                    },
                )
            ).status_code == 401
            assert (
                await client.post(
                    "/api/v1/dalton/channels/internal/inbound/progress",
                    headers=svc_headers,
                    json={
                        "platform": "feishu",
                        "message_id": "om_company_inbound",
                        "status": "completed",
                    },
                )
            ).status_code == 401
            assert (
                await client.post(
                    "/api/v1/dalton/channels/internal/steers/reconcile",
                    headers=svc_headers,
                    json={"platform": "feishu", "message_id": "om_company_inbound"},
                )
            ).status_code == 401

            async with au1_live_pool.connection() as db_conn:
                assert await _channel_table_counts(db_conn) == seeded_counts
                async with db_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT used_at FROM dalton_channel_bind_codes WHERE code_hash = %s",
                        (comp_code_hash,),
                    )
                    assert (await cur.fetchone())["used_at"] is None
                    await cur.execute(
                        "SELECT status FROM dalton_channel_inbound_events WHERE platform = 'feishu' AND message_id = 'om_company_inbound'"
                    )
                    assert (await cur.fetchone())["status"] == "accepted"

                await db_conn.execute(
                    "DELETE FROM dalton_channel_identities WHERE platform_user_id = 'ou_company_bound'"
                )
                await db_conn.execute(
                    "DELETE FROM dalton_channel_bind_codes WHERE code_hash = %s",
                    (comp_code_hash,),
                )
                await db_conn.execute(
                    "DELETE FROM dalton_channel_inbound_events WHERE platform = 'feishu' AND message_id = 'om_company_inbound'"
                )

        # 4. Human user positive flows + unlinked user denial + colleague workspace revocation!
        r_unlinked = await client.post(
            "/api/v1/dalton/channels/internal/session/command",
            headers=svc_headers,
            json={
                "cmd": "new",
                "platform": "feishu",
                "platform_user_id": "ou_unlinked_visitor",
                "scope_key": "dm:ou_unlinked_visitor",
            },
        )
        assert r_unlinked.status_code == 200, r_unlinked.text
        assert r_unlinked.json()["ok"] is False
        assert r_unlinked.json()["deny_reason"] == "not_linked"

        r_create_code = await client.post(
            "/api/v1/dalton/channels/bind-codes",
            headers={**svc_headers, "X-User-Id": human_uid},
            json={"platform": "feishu"},
        )
        assert r_create_code.status_code == 200, r_create_code.text
        human_code = r_create_code.json()["code"]

        r_consume_human = await client.post(
            "/api/v1/dalton/channels/internal/bind-code/consume",
            headers=svc_headers,
            json={
                "action": "bind",
                "platform": "feishu",
                "platform_user_id": "ou_human_ok",
                "platform_username": "Human Analyst",
                "code": human_code,
            },
        )
        assert r_consume_human.status_code == 200, r_consume_human.text
        assert r_consume_human.json()["ok"] is True
        assert r_consume_human.json()["user_id"] == human_uid

        r_human_new = await client.post(
            "/api/v1/dalton/channels/internal/session/command",
            headers=svc_headers,
            json={
                "cmd": "new",
                "platform": "feishu",
                "dalton_user_id": human_uid,
                "scope_key": "dm:ou_human_ok",
            },
        )
        assert r_human_new.status_code == 200, r_human_new.text
        assert r_human_new.json()["ok"] is True
        assert r_human_new.json()["generation"] == 2

        r_switch = await client.post(
            "/api/v1/dalton/channels/internal/session/command",
            headers=svc_headers,
            json={
                "cmd": "workspace_switch",
                "platform": "feishu",
                "platform_user_id": "ou_human_ok",
                "scope_key": "dm:ou_human_ok",
                "workspace_id": ws_colleague,
            },
        )
        assert r_switch.status_code == 200, r_switch.text
        assert r_switch.json()["ok"] is True
        assert r_switch.json()["workspace_id"] == ws_colleague

        r_recover_session = await client.post(
            "/api/v1/dalton/channels/internal/session/command",
            headers=svc_headers,
            json={
                "cmd": "workspace_list",
                "platform": "feishu",
                "dalton_user_id": human_uid,
                "scope_key": "dm:ou_human_ok",
                "is_internal_query": True,
            },
        )
        assert r_recover_session.status_code == 200, r_recover_session.text
        assert r_recover_session.json()["current_workspace_id"] == ws_colleague

        # Revoke colleague write sharing -> inbound message on overridden workspace is denied!
        await dalton_sharing.upsert_workspace_sharing(
            ws_colleague,
            cos_access="ro",
            web_access="ro",
            updated_by=colleague_uid,
        )

        r_revoked = await client.post(
            "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
            headers=svc_headers,
            json={
                "platform": "feishu",
                "platform_user_id": "ou_human_ok",
                "chat_type": "dm",
                "chat_address": "feishu:@me",
                "thread_scope_key": "dm:ou_human_ok",
                "message_id": "om_human_after_revoke",
            },
        )
        assert r_revoked.status_code == 200, r_revoked.text
        assert r_revoked.json()["allowed"] is False
        assert r_revoked.json()["deny_reason"] == "workspace_write_denied"


# ---------------------------------------------------------------------------
# 5. R4 Shared SQL / Python / Create / Update classification matrix &
#    PM chief_of_staff upsert on legacy null-workspace rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sql_python_create_update_scope_parity_and_null_ws_pm_upsert_real_db(
    test_db_uri: str,
    au1_live_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dalton_cfg = _dalton_alembic_cfg(test_db_uri)
    uid = "u-parity-r4"
    other_uid = "u-parity-other"
    ws_active = str(uuid.uuid4())
    ws_unit_sector = str(uuid.uuid4())
    ws_other_owner = str(uuid.uuid4())
    ws_deleted = str(uuid.uuid4())
    ws_unbound = str(uuid.uuid4())
    ws_archived_unit = str(uuid.uuid4())
    ws_plain = str(uuid.uuid4())
    home_id = get_home_workspace_id(uid)
    fixed_ts = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)

    # Same shared input matrix tested across:
    # (1) Python classify_automation_scope, (2) SQL d035 migration backfill,
    # (3) SQL build_scope_case_sql, (4) lifecycle.create_automation, and (5) lifecycle.update_automation!
    shared_matrix: list[tuple[str, str, str | None, dict[str, Any], str]] = [
        ("cal-spaces", "  dalton-earnings-calendar  ", ws_plain, {}, "research_tracking"),
        ("cal-tab-nl", "\tdalton-earnings-calendar\n", None, {}, "research_tracking"),
        ("cal-unicode", "\u00a0dalton-earnings-calendar\u3000", ws_plain, {}, "research_tracking"),
        (
            "kind-ws-case",
            "Custom Kind Note",
            ws_plain,
            {"dalton_kind": " \t\u3000Company_Watch\n"},
            "research_tracking",
        ),
        (
            "kind-sector-upper",
            "Sector Review",
            None,
            {"dalton_kind": "SECTOR_DELTA"},
            "research_tracking",
        ),
        (
            "kind-unknown",
            "Unknown Kind Task",
            ws_active,
            {"dalton_kind": " \tunknown_kind\u3000"},
            "personal",
        ),
        (
            "kind-empty-ws",
            "Blank Kind Task",
            ws_active,
            {"dalton_kind": " \t\n\u00a0\u3000"},
            "personal",
        ),
        ("kind-null", "Null Kind Task", ws_active, {"dalton_kind": None}, "personal"),
        (
            "ticker-mixed-ws",
            " \tdalton-nvda-CustomProbe\u3000\n",
            ws_active,
            {},
            "research_tracking",
        ),
        (
            "ticker-dot-case",
            "\u3000dalton-brk.b-FilingCheck ",
            ws_active,
            {},
            "research_tracking",
        ),
        (
            "active-sector-unit",
            " \tdalton-semi-supply-chain\n",
            ws_unit_sector,
            {},
            "research_tracking",
        ),
        (
            "director-daily-ws",
            " \tdalton-director-daily\u3000",
            ws_active,
            {"dalton_kind": " \tdirector_daily\n"},
            "personal",
        ),
        (
            "director-weekly-ws",
            "\u00a0dalton-director-weekly\t",
            ws_active,
            {"dalton_kind": "director_weekly"},
            "personal",
        ),
        (
            "data-health-ws",
            " dalton-data-health\n",
            ws_active,
            {"dalton_kind": "data_health"},
            "personal",
        ),
        (
            "alphaengine-pers",
            "AlphaEngine 机构会议增量整理入库（每日 09:00 / 18:00）",
            ws_active,
            {},
            "personal",
        ),
        ("upper-prefix", "DALTON-NVDA-check", ws_active, {}, "personal"),
        ("other-owner", "dalton-nvda-other-owner", ws_other_owner, {}, "personal"),
        ("deleted-ws", "dalton-nvda-deleted-ws", ws_deleted, {}, "personal"),
        ("unbound-ws", "dalton-nvda-unbound-ws", ws_unbound, {}, "personal"),
        ("archived-unit", "dalton-msft-archived-unit", ws_archived_unit, {}, "personal"),
        ("null-ws-no-kind", "dalton-nvda-null-ws", None, {}, "personal"),
    ]

    ws_map = {
        ws_active: {"workspace_id": ws_active, "user_id": uid, "status": "running"},
        ws_unit_sector: {"workspace_id": ws_unit_sector, "user_id": uid, "status": "running"},
        ws_other_owner: {"workspace_id": ws_other_owner, "user_id": other_uid, "status": "running"},
        ws_deleted: {"workspace_id": ws_deleted, "user_id": uid, "status": "deleted"},
        ws_unbound: {"workspace_id": ws_unbound, "user_id": uid, "status": "unbound"},
        ws_archived_unit: {"workspace_id": ws_archived_unit, "user_id": uid, "status": "running"},
        ws_plain: {"workspace_id": ws_plain, "user_id": uid, "status": "running"},
        home_id: {"workspace_id": home_id, "user_id": uid, "status": "running"},
    }
    units_all = [
        {
            "workspace_id": ws_unit_sector,
            "owner_user_id": uid,
            "kind": "sector",
            "status": "active",
        },
        {
            "workspace_id": ws_archived_unit,
            "owner_user_id": uid,
            "kind": "company",
            "status": "archived",
        },
    ]
    covered_raw = [" \tnVdA\u3000", "  BrK.b\n", " \t\u3000"]

    # 1. Verify Python classify_automation_scope & needs_workspace_tracking_lookup on every matrix item
    for label, name, ws_id, meta, exp_scope in shared_matrix:
        row_probe = {"user_id": uid, "name": name, "workspace_id": ws_id, "metadata": meta}
        got_py = classify_automation_scope(
            row_probe,
            ws_map.get(ws_id or ""),
            coverage_units=[u for u in units_all if u["workspace_id"] == ws_id],
            covered_tickers=covered_raw,
        )
        assert got_py == exp_scope, f"Python mismatch on {label}: {got_py} != {exp_scope}"
        if not needs_workspace_tracking_lookup(
            uid, name=name, workspace_id=ws_id, metadata=meta
        ):
            assert classify_automation_scope(row_probe) == exp_scope, label

    # 2. Downgrade to d031, seed the exact matrix (with padded/mixed-case tickers), upgrade to d035,
    #    and verify migration backfill + live build_scope_case_sql + preserved name/metadata/updated_at!
    await asyncio.to_thread(command.downgrade, dalton_cfg, "d031")

    backfill_ids: dict[str, str] = {}
    async with au1_live_pool.connection() as db_conn:
        await _insert_user(db_conn, uid, "parity@dalton.test")
        await _insert_user(db_conn, other_uid, "parity-other@dalton.test")
        for wid, winfo in ws_map.items():
            await _insert_workspace(
                db_conn, wid, winfo["user_id"], f"WS-{wid[:6]}", status=winfo["status"]
            )
        await db_conn.execute(
            """
            INSERT INTO dalton_coverage (user_id, ticker, name, tier)
            VALUES (%s, %s, 'NVIDIA', 'core'), (%s, %s, 'Berkshire', 'watch')
            """,
            (uid, " \tnVdA\u3000", uid, "  BrK.b\n"),
        )
        await db_conn.execute(
            """
            INSERT INTO dalton_coverage_units (
                unit_id, kind, owner_user_id, slug, display_name, workspace_id, ticker, status
            )
            VALUES
                (%s::uuid, 'sector', %s, 'semi', 'Semiconductors', %s::uuid, NULL, 'active'),
                (%s::uuid, 'company', %s, 'msft', 'Microsoft', %s::uuid, 'MSFT', 'archived')
            """,
            (str(uuid.uuid4()), uid, ws_unit_sector, str(uuid.uuid4()), uid, ws_archived_unit),
        )

        for label, name, ws_id, meta, _exp_scope in shared_matrix:
            aid = str(uuid.uuid4())
            backfill_ids[label] = aid
            await db_conn.execute(
                """
                INSERT INTO automations (
                    automation_id, user_id, name, file_name, trigger_type,
                    cron_expression, timezone, instruction, workspace_id,
                    status, metadata, created_at, updated_at
                )
                VALUES (
                    %s::uuid, %s, %s, %s, 'cron',
                    '0 9 * * *', 'Asia/Shanghai', 'Run parity', %s::uuid,
                    'active', %s::jsonb, %s, %s
                )
                """,
                (aid, uid, name, f"bf-{label}.json", ws_id, json.dumps(meta), fixed_ts, fixed_ts),
            )
            await db_conn.execute(
                "ALTER TABLE automations DISABLE TRIGGER trg_automations_updated_at"
            )
            await db_conn.execute(
                "UPDATE automations SET updated_at = %s WHERE automation_id = %s::uuid",
                (fixed_ts, aid),
            )
            await db_conn.execute(
                "ALTER TABLE automations ENABLE TRIGGER trg_automations_updated_at"
            )

    await asyncio.to_thread(command.upgrade, dalton_cfg, "head")

    case_sql = build_scope_case_sql("a")
    async with au1_live_pool.connection() as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            for label, orig_name, _ws_id, orig_meta, exp_scope in shared_matrix:
                await cur.execute(
                    f"""
                    SELECT a.name, a.metadata, a.scope, a.notify_storage_mode,
                           a.audit_history, a.updated_at,
                           ({case_sql}) AS computed_sql_scope
                    FROM automations a
                    WHERE a.automation_id = %s::uuid
                    """,
                    (backfill_ids[label],),
                )
                r = await cur.fetchone()
                assert r is not None, label
                assert r["scope"] == exp_scope, f"Backfill mismatch {label}: {r['scope']} != {exp_scope}"
                assert r["computed_sql_scope"] == exp_scope, f"Case SQL mismatch {label}"
                # Stored name, metadata, legacy notify mode, empty history, and updated_at preserved!
                assert r["name"] == orig_name, label
                assert r["metadata"] == orig_meta, label
                assert r["notify_storage_mode"] == "legacy", label
                assert r["audit_history"] == [], label
                assert r["updated_at"] == fixed_ts, label

        # Clear backfilled rows so we test create + update + HTTP list totals on the same matrix!
        await db_conn.execute("DELETE FROM automations WHERE user_id = %s", (uid,))

    # 3. Create every assignable workspace case via lifecycle.create_automation and verify parity + HTTP totals!
    created_by_label: dict[str, dict[str, Any]] = {}
    expected_pers = 0
    expected_rt = 0
    for label, name, ws_id, meta, _exp_scope in shared_matrix:
        if ws_id in (ws_other_owner, ws_deleted, ws_unbound):
            continue
        target_ws = uuid.UUID(ws_id) if ws_id else uuid.UUID(home_id)
        # When ws_id is None in shared_matrix, lifecycle.create_automation places it in Home (not NULL);
        # recompute expected scope for target_ws so we test exact parity!
        exp_create_scope = classify_automation_scope(
            {"user_id": uid, "name": name, "workspace_id": str(target_ws), "metadata": meta},
            ws_map[str(target_ws)],
            coverage_units=[u for u in units_all if u["workspace_id"] == str(target_ws)],
            covered_tickers=covered_raw,
        )
        created = await lifecycle_mod.create_automation(
            uid,
            AutomationCreate(
                name=name,
                trigger_type="cron",
                cron_expression="0 9 * * *",
                instruction="Run created parity",
                workspace_id=target_ws,
                metadata=meta,
            ),
        )
        assert created["scope"] == exp_create_scope, f"Create mismatch {label}"
        assert created["name"] == name, f"Create mutated name on {label}"
        assert created["metadata"] == meta, f"Create mutated metadata on {label}"
        assert created["notify_storage_mode"] == "legacy"
        created_by_label[label] = created
        if exp_create_scope == "personal":
            expected_pers += 1
        else:
            expected_rt += 1

    # 4. Verify lifecycle.update_automation flips scope identically to SQL & Python without altering notify mode!
    flip_target = created_by_label["kind-unknown"]
    assert flip_target["scope"] == "personal"
    flipped_to_rt = await lifecycle_mod.update_automation(
        flip_target["automation_id"],
        uid,
        {"name": " \tdalton-nvda-FlippedCheck\u3000", "metadata": {}},
    )
    assert flipped_to_rt is not None
    assert flipped_to_rt["scope"] == "research_tracking"
    assert flipped_to_rt["name"] == " \tdalton-nvda-FlippedCheck\u3000"
    assert flipped_to_rt["notify_storage_mode"] == "legacy"
    expected_pers -= 1
    expected_rt += 1

    # Moving sector-unit task from ws_unit_sector to ws_plain ('semi' is not in dalton_coverage) flips to personal!
    sector_target = created_by_label["active-sector-unit"]
    assert sector_target["scope"] == "research_tracking"
    moved_sector = await lifecycle_mod.update_automation(
        sector_target["automation_id"],
        uid,
        {"workspace_id": uuid.UUID(ws_plain)},
    )
    assert moved_sector is not None
    assert moved_sector["scope"] == "personal"
    assert moved_sector["notify_storage_mode"] == "legacy"
    expected_rt -= 1
    expected_pers += 1

    # Confirm every stored row's scope equals live SQL CASE expression on PostgreSQL!
    async with au1_live_pool.connection() as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT a.automation_id, a.scope, ({case_sql}) AS sql_scope FROM automations a WHERE a.user_id = %s",
                (uid,),
            )
            for row_chk in await cur.fetchall():
                assert row_chk["scope"] == row_chk["sql_scope"], row_chk

    # Verify HTTP scoped listing & totals match Python and SQL counts!
    app = FastAPI()
    app.include_router(automations_app.router)
    app.dependency_overrides[principal_mod.get_principal] = lambda: principal_mod.Principal(
        user_id=uid, auth="session"
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r_pers = await client.get("/api/v1/automations?scope=personal&limit=100")
        r_rt = await client.get("/api/v1/automations?scope=research_tracking&limit=100")
        assert r_pers.status_code == 200, r_pers.text
        assert r_rt.status_code == 200, r_rt.text
        assert r_pers.json()["total"] == expected_pers
        assert r_rt.json()["total"] == expected_rt
        assert len(r_pers.json()["automations"]) == expected_pers
        assert len(r_rt.json()["automations"]) == expected_rt

    # 5. PM chief_of_staff upsert on legacy NULL-workspace row vs analyst/non-PM refusal vs non-null workspace!
    legacy_null_id = str(uuid.uuid4())
    bound_cov_id = str(uuid.uuid4())
    async with au1_live_pool.connection() as db_conn:
        await db_conn.execute(
            """
            INSERT INTO automations (
                automation_id, user_id, name, file_name, trigger_type,
                cron_expression, timezone, instruction, workspace_id,
                status, scope, metadata
            )
            VALUES
                (%s::uuid, %s, 'dalton-nvda-legacy-null-ws', 'legacy-null.json', 'cron',
                 '0 8 * * *', 'Asia/Shanghai', 'Legacy v1', NULL, 'active', 'personal', '{}'::jsonb),
                (%s::uuid, %s, 'dalton-nvda-bound-cov-ws', 'bound-cov.json', 'cron',
                 '0 8 * * *', 'Asia/Shanghai', 'Bound v1', %s::uuid, 'active', 'research_tracking', '{}'::jsonb)
            """,
            (legacy_null_id, uid, bound_cov_id, uid, ws_active),
        )

    # Analyst creation stays in the caller's workspace and leaves the Home task intact.
    monkeypatch.setattr(
        "src.dalton.autonomy.tracking._resolve_workspace_ticker",
        lambda _w: _coro("NVDA"),
    )
    analyst_created = await schedule_dalton_automation_helper(
        name="dalton-nvda-legacy-null-ws",
        workspace_id=ws_active,
        caller_workspace_id=ws_active,
        spec={"instruction": "Workspace analysis", "cron": "0 9 * * *"},
        user_id=uid,
        role="analyst",
    )
    assert analyst_created["ok"] is True
    assert analyst_created["action"] == "created"
    assert analyst_created["automation_id"] != legacy_null_id
    async with au1_live_pool.connection() as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT workspace_id, instruction FROM automations WHERE automation_id = %s::uuid",
                (legacy_null_id,),
            )
            untouched = await cur.fetchone()
            assert untouched["workspace_id"] is None
            assert untouched["instruction"] == "Legacy v1"
            await cur.execute(
                "DELETE FROM automations WHERE automation_id = %s::uuid AND workspace_id = %s::uuid RETURNING automation_id",
                (analyst_created["automation_id"], ws_active),
            )
            assert await cur.fetchone() is not None

    # 5b. Non-PM chief_of_staff cannot update legacy null-workspace row -> forbidden_pm
    monkeypatch.setattr("src.dalton.autonomy.system_jobs.is_pm_user", lambda _u: False)
    refused_non_pm = await schedule_dalton_automation_helper(
        name="dalton-nvda-legacy-null-ws",
        workspace_id=ws_active,
        spec={"instruction": "Non-PM attempt", "cron": "0 9 * * *"},
        user_id=uid,
        role="chief_of_staff",
    )
    assert refused_non_pm["ok"] is False
    assert refused_non_pm["error"] == "forbidden_pm"

    # 5c. PM chief_of_staff updates legacy null-workspace row while passing covered ws_active:
    #     patch does NOT change workspace_id, so workspace_id stays NULL and scope stays 'personal'!
    monkeypatch.setattr("src.dalton.autonomy.system_jobs.is_pm_user", lambda u: u == uid)
    pm_upd_null_ws = await schedule_dalton_automation_helper(
        name="dalton-nvda-legacy-null-ws",
        workspace_id=ws_active,
        spec={"instruction": "PM updated legacy", "cron": "0 10 * * *"},
        user_id=uid,
        role="chief_of_staff",
    )
    assert pm_upd_null_ws["ok"] is True
    assert pm_upd_null_ws["action"] == "updated"
    assert pm_upd_null_ws["automation_id"] == legacy_null_id

    async with au1_live_pool.connection() as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT a.*, ({case_sql}) AS sql_scope
                FROM automations a
                WHERE a.automation_id = %s::uuid
                """,
                (legacy_null_id,),
            )
            saved_null_row = await cur.fetchone()
            assert saved_null_row is not None
            assert saved_null_row["workspace_id"] is None
            assert saved_null_row["instruction"] == "PM updated legacy"
            assert saved_null_row["scope"] == "personal"
            assert saved_null_row["sql_scope"] == "personal"
            assert (
                classify_automation_scope(
                    saved_null_row,
                    covered_tickers=covered_raw,
                )
                == "personal"
            )

    # The PM updates the selected covered workspace's existing task.
    pm_upd_bound = await schedule_dalton_automation_helper(
        name="dalton-nvda-bound-cov-ws",
        workspace_id=ws_active,
        spec={"instruction": "PM updated bound", "cron": "0 11 * * *"},
        user_id=uid,
        role="chief_of_staff",
    )
    assert pm_upd_bound["ok"] is True
    assert pm_upd_bound["action"] == "updated"

    # 5e. PM chief_of_staff adds research dalton_kind on null-workspace row -> scope becomes 'research_tracking'!
    pm_upd_null_rt = await schedule_dalton_automation_helper(
        name="dalton-nvda-legacy-null-ws",
        spec={
            "instruction": "PM watch kind",
            "cron": "0 12 * * *",
            "metadata": {"dalton_kind": " \tCOMPANY_WATCH\u3000"},
        },
        user_id=uid,
        role="chief_of_staff",
    )
    assert pm_upd_null_rt["ok"] is True

    async with au1_live_pool.connection() as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT a.workspace_id, a.scope, ({case_sql}) AS sql_scope FROM automations a WHERE a.automation_id = %s::uuid",
                (bound_cov_id,),
            )
            r_bound = await cur.fetchone()
            assert str(r_bound["workspace_id"]) == ws_active
            assert r_bound["scope"] == "research_tracking"
            assert r_bound["sql_scope"] == "research_tracking"

            await cur.execute(
                f"SELECT a.workspace_id, a.scope, ({case_sql}) AS sql_scope FROM automations a WHERE a.automation_id = %s::uuid",
                (legacy_null_id,),
            )
            r_null_rt = await cur.fetchone()
            assert r_null_rt["workspace_id"] is None
            assert r_null_rt["scope"] == "research_tracking"
            assert r_null_rt["sql_scope"] == "research_tracking"


async def _coro(val: Any) -> Any:
    return val


# ---------------------------------------------------------------------------
# 6. AU1 Batch A: Config History, Seed v1+v2, Expected Config Version & Concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_au1_batch_a_create_seed_expected_version_and_concurrency_real_db(
    au1_live_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "u-hist-batch-a"
    actor2 = "u-hist-actor-2"
    ws_id = str(uuid.uuid4())
    legacy_patch_id = str(uuid.uuid4())
    legacy_pause_id = str(uuid.uuid4())
    legacy_resume_id = str(uuid.uuid4())
    corrupt_empty_id = str(uuid.uuid4())
    corrupt_missing_id = str(uuid.uuid4())

    async with au1_live_pool.connection() as conn:
        await _insert_user(conn, uid, "hist-a@dalton.test")
        await _insert_user(conn, actor2, "hist-actor2@dalton.test")
        await _insert_workspace(conn, ws_id, uid, "Hist-WS")
        # Seed user_preferences.model_preference with legacy notify routes so snapshot resolution is verified against real DB
        await conn.execute(
            """
            INSERT INTO user_preferences (user_id, model_preference)
            VALUES (%s, %s::jsonb)
            ON CONFLICT (user_id) DO UPDATE SET model_preference = EXCLUDED.model_preference
            """,
            (
                uid,
                json.dumps(
                    {
                        "dalton_notify_enabled": True,
                        "dalton_notify_targets": ["feishu:user:ou_hist_user_1"],
                        "dalton_notify_overrides": {
                            f"automation:{legacy_patch_id}": True,
                        },
                        "dalton_notify_routes": {
                            "default": ["feishu:user:ou_hist_user_1"],
                            f"automation:{legacy_patch_id}": ["feishu:user:ou_hist_user_1"],
                        },
                    }
                ),
            ),
        )
        # Seed 3 legacy empty-history rows (config_version=1, active_version=1, audit_history=[])
        # and 2 corrupt-history rows
        await conn.execute(
            """
            INSERT INTO automations (
                automation_id, user_id, name, file_name, description, trigger_type,
                cron_expression, timezone, instruction, workspace_id,
                status, scope, config_version, active_version, audit_history, metadata
            )
            VALUES
                (%s::uuid, %s, 'Legacy Patch Task', 'leg-patch.json', 'Old desc', 'cron',
                 '0 8 * * 1-5', 'Asia/Shanghai', 'Old instruction', %s::uuid,
                 'active', 'personal', 1, 1, '[]'::jsonb, '{"k": 1}'::jsonb),
                (%s::uuid, %s, 'Legacy Pause Task', 'leg-pause.json', 'Pause desc', 'cron',
                 '0 9 * * 1-5', 'Asia/Shanghai', 'Pause instruction', %s::uuid,
                 'active', 'personal', 1, 1, '[]'::jsonb, '{}'::jsonb),
                (%s::uuid, %s, 'Legacy Resume Task', 'leg-resume.json', 'Resume desc', 'cron',
                 '0 10 * * 1-5', 'Asia/Shanghai', 'Resume instruction', %s::uuid,
                 'paused', 'personal', 1, 1, '[]'::jsonb, '{}'::jsonb),
                (%s::uuid, %s, 'Corrupt Empty History', 'corrupt-empty.json', 'Corrupt', 'cron',
                 '0 11 * * 1-5', 'Asia/Shanghai', 'Corrupt inst', %s::uuid,
                 'active', 'personal', 3, 3, '[]'::jsonb, '{}'::jsonb),
                (%s::uuid, %s, 'Corrupt Missing Active', 'corrupt-miss.json', 'Corrupt2', 'cron',
                 '0 12 * * 1-5', 'Asia/Shanghai', 'Corrupt inst2', %s::uuid,
                 'active', 'personal', 2, 2, '[{"version": 1, "snapshot": {"name": "v1"}}]'::jsonb, '{}'::jsonb)
            """,
            (
                legacy_patch_id,
                uid,
                ws_id,
                legacy_pause_id,
                uid,
                ws_id,
                legacy_resume_id,
                uid,
                ws_id,
                corrupt_empty_id,
                uid,
                ws_id,
                corrupt_missing_id,
                uid,
                ws_id,
            ),
        )

    app = FastAPI()
    app.include_router(automations_app.router)
    app.dependency_overrides[principal_mod.get_principal] = lambda: principal_mod.Principal(
        user_id=uid, auth="session"
    )
    app.dependency_overrides[api_utils.get_current_user_id] = lambda: uid

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Create new automation via POST /api/v1/automations -> v1 history created immediately
        r_create = await client.post(
            "/api/v1/automations",
            json={
                "name": "Fresh Personal Task",
                "description": "Created via API",
                "trigger_type": "cron",
                "cron_expression": "30 8 * * 1-5",
                "timezone": "Asia/Shanghai",
                "instruction": "Summarize morning news",
                "workspace_id": ws_id,
                "metadata": {"tag": "morning"},
            },
        )
        assert r_create.status_code == 201, r_create.text
        created_body = r_create.json()
        new_id = created_body["automation_id"]
        assert created_body["config_version"] == 1
        assert created_body["active_version"] == 1
        assert created_body["created_by"] == uid
        assert created_body["updated_by"] == uid

        r_hist1 = await client.get(f"/api/v1/automations/{new_id}/history")
        assert r_hist1.status_code == 200, r_hist1.text
        hist1 = r_hist1.json()
        assert hist1["config_version"] == 1
        assert hist1["active_version"] == 1
        assert hist1["total"] == 1
        [v1_item] = hist1["entries"]
        assert v1_item["version"] == 1
        assert v1_item["action"] == "create"
        assert v1_item["actor_user_id"] == uid
        assert v1_item["prev_version"] is None
        assert v1_item["changes"] == {}
        assert set(v1_item["snapshot"].keys()) == set(
            company_auto_svc.ALLOWED_AUTOMATION_SNAPSHOT_FIELDS
        )
        assert "metadata" not in v1_item["snapshot"]
        assert v1_item["snapshot"]["name"] == "Fresh Personal Task"
        assert v1_item["snapshot"]["desired_status"] == "active"
        assert v1_item["snapshot"]["notify_enabled"] is False
        assert v1_item["snapshot"]["notify_routes"] == []

        # 2. First modification on legacy empty-history task via PATCH -> seeds v1 + writes v2 atomically
        r_patch_leg = await client.patch(
            f"/api/v1/automations/{legacy_patch_id}",
            json={"instruction": "New instruction v2", "description": "New desc v2"},
        )
        assert r_patch_leg.status_code == 200, r_patch_leg.text
        patched_leg = r_patch_leg.json()
        assert patched_leg["config_version"] == 2
        assert patched_leg["active_version"] == 2
        assert patched_leg["updated_by"] == uid

        r_leg_hist = await client.get(f"/api/v1/automations/{legacy_patch_id}/history")
        assert r_leg_hist.status_code == 200
        leg_items = r_leg_hist.json()["entries"]
        assert [it["version"] for it in leg_items] == [2, 1]
        v2_entry, v1_seed = leg_items[0], leg_items[1]
        assert v1_seed["action"] == "seed"
        assert v1_seed["prev_version"] is None
        assert v1_seed["changes"] == {}
        assert v1_seed["snapshot"]["instruction"] == "Old instruction"
        assert v1_seed["snapshot"]["description"] == "Old desc"
        assert v1_seed["snapshot"]["notify_enabled"] is True
        assert v1_seed["snapshot"]["notify_routes"] == ["feishu:user:ou_hist_user_1"]

        assert v2_entry["action"] == "update"
        assert v2_entry["prev_version"] == 1
        assert v2_entry["actor_user_id"] == uid
        assert v2_entry["changes"] == {
            "instruction": {"old": "Old instruction", "new": "New instruction v2"},
            "description": {"old": "Old desc", "new": "New desc v2"},
        }
        assert v2_entry["snapshot"]["instruction"] == "New instruction v2"

        # 2b. Legacy callers without expected_config_version updating different fields do not overwrite each other
        r_leg_f1, r_leg_f2 = await asyncio.gather(
            client.patch(
                f"/api/v1/automations/{legacy_patch_id}",
                json={"description": "Desc updated without expected_config_version"},
            ),
            client.patch(
                f"/api/v1/automations/{legacy_patch_id}",
                json={"instruction": "Instruction updated without expected_config_version"},
            ),
        )
        assert r_leg_f1.status_code == 200, r_leg_f1.text
        assert r_leg_f2.status_code == 200, r_leg_f2.text
        leg_after_both = (await client.get(f"/api/v1/automations/{legacy_patch_id}")).json()
        assert leg_after_both["config_version"] == 4
        assert leg_after_both["description"] == "Desc updated without expected_config_version"
        assert leg_after_both["instruction"] == "Instruction updated without expected_config_version"

        # 2c. Deterministic concurrent schedule edits without expected_config_version:
        #     Both read v1 (cron='0 9 * * *', timezone='UTC'); cron edit commits first, timezone second;
        #     persisted next_run_at MUST be derived from the locked row ('0 10 * * *' in 'Asia/Shanghai').
        from src.server.services.automation_scheduler import AutomationScheduler

        r_sched_create = await client.post(
            "/api/v1/automations",
            json={
                "name": "Concurrent Schedule Task",
                "trigger_type": "cron",
                "cron_expression": "0 9 * * *",
                "timezone": "UTC",
                "instruction": "Run daily schedule test",
                "workspace_id": ws_id,
            },
        )
        assert r_sched_create.status_code == 201, r_sched_create.text
        sched_conc_id = r_sched_create.json()["automation_id"]

        orig_current_fn = lifecycle_mod._current
        both_read_evt = asyncio.Event()
        cron_read_evt = asyncio.Event()
        first_committed_evt = asyncio.Event()
        read_snapshots: list[tuple[int, str, str]] = []

        async def _coordinated_current(
            a_id: str, u_id: str, c_conn: Any, cur_row: dict[str, Any] | None
        ) -> dict[str, Any] | None:
            row = await orig_current_fn(a_id, u_id, c_conn, cur_row)
            if a_id == sched_conc_id and row is not None:
                idx = len(read_snapshots)
                read_snapshots.append(
                    (
                        int(row.get("config_version") or 1),
                        str(row.get("cron_expression") or ""),
                        str(row.get("timezone") or ""),
                    )
                )
                if idx == 0:
                    cron_read_evt.set()
                if len(read_snapshots) >= 2:
                    both_read_evt.set()
                await both_read_evt.wait()
                if idx == 1:
                    await first_committed_evt.wait()
            return row

        monkeypatch.setattr(lifecycle_mod, "_current", _coordinated_current)

        async def _patch_cron_first() -> Any:
            try:
                return await client.patch(
                    f"/api/v1/automations/{sched_conc_id}",
                    json={"cron_expression": "0 10 * * *"},
                )
            finally:
                first_committed_evt.set()

        async def _patch_tz_second() -> Any:
            await asyncio.wait_for(cron_read_evt.wait(), timeout=10)
            return await client.patch(
                f"/api/v1/automations/{sched_conc_id}",
                json={"timezone": "Asia/Shanghai"},
            )

        r_cron_conc, r_tz_conc = await asyncio.wait_for(
            asyncio.gather(_patch_cron_first(), _patch_tz_second()),
            timeout=30,
        )
        monkeypatch.setattr(lifecycle_mod, "_current", orig_current_fn)
        assert r_cron_conc.status_code == 200, r_cron_conc.text
        assert r_tz_conc.status_code == 200, r_tz_conc.text
        assert read_snapshots == [
            (1, "0 9 * * *", "UTC"),
            (1, "0 9 * * *", "UTC"),
        ]
        sched_final_row = await auto_db.get_automation(sched_conc_id, uid)
        sched_final_hist = await company_auto_db.get_automation_history_row(sched_conc_id)
        assert sched_final_row is not None and sched_final_hist is not None
        assert sched_final_row["config_version"] == 3
        assert sched_final_row["cron_expression"] == "0 10 * * *"
        assert sched_final_row["timezone"] == "Asia/Shanghai"
        expected_final_next = AutomationScheduler.calculate_first_run(
            "0 10 * * *", "Asia/Shanghai"
        )
        assert sched_final_row["next_run_at"] == expected_final_next
        assert len(sched_final_hist["audit_history"]) == 3

        # Also verify seed + v2 on first POST /pause and first POST /resume
        r_pause_leg = await client.post(f"/api/v1/automations/{legacy_pause_id}/pause")
        assert r_pause_leg.status_code == 200, r_pause_leg.text
        assert r_pause_leg.json()["config_version"] == 2
        pause_hist = (await client.get(f"/api/v1/automations/{legacy_pause_id}/history")).json()["entries"]
        assert [it["action"] for it in pause_hist] == ["pause", "seed"]
        assert pause_hist[0]["changes"] == {
            "desired_status": {"old": "active", "new": "paused"}
        }

        r_resume_leg = await client.post(f"/api/v1/automations/{legacy_resume_id}/resume")
        assert r_resume_leg.status_code == 200, r_resume_leg.text
        assert r_resume_leg.json()["config_version"] == 2
        resume_hist = (await client.get(f"/api/v1/automations/{legacy_resume_id}/history")).json()["entries"]
        assert [it["action"] for it in resume_hist] == ["resume", "seed"]
        assert resume_hist[0]["changes"] == {
            "desired_status": {"old": "paused", "new": "active"}
        }

        # 3. expected_config_version: matching -> 200, mismatched -> 409, invalid -> 400 across PATCH/pause/resume
        r_match_patch = await client.patch(
            f"/api/v1/automations/{new_id}",
            json={"description": "v2 matched", "expected_config_version": 1},
        )
        assert r_match_patch.status_code == 200
        assert r_match_patch.json()["config_version"] == 2

        r_conflict_patch = await client.patch(
            f"/api/v1/automations/{new_id}",
            json={"description": "stale write", "expected_config_version": 1},
        )
        assert r_conflict_patch.status_code == 409

        r_conflict_pause = await client.post(
            f"/api/v1/automations/{new_id}/pause",
            json={"expected_config_version": 1},
        )
        assert r_conflict_pause.status_code == 409

        r_match_pause = await client.post(
            f"/api/v1/automations/{new_id}/pause",
            json={"expected_config_version": 2},
        )
        assert r_match_pause.status_code == 200
        assert r_match_pause.json()["config_version"] == 3

        r_conflict_resume = await client.post(
            f"/api/v1/automations/{new_id}/resume",
            json={"expected_config_version": 2},
        )
        assert r_conflict_resume.status_code == 409

        r_match_resume = await client.post(
            f"/api/v1/automations/{new_id}/resume",
            json={"expected_config_version": 3},
        )
        assert r_match_resume.status_code == 200
        assert r_match_resume.json()["config_version"] == 4

        # Invalid expected_config_version values -> 400 on PATCH, pause, resume; DB version stays 4
        for invalid_ver in (0, -1, "4", True, False, 1.5, None):
            assert (
                await client.patch(
                    f"/api/v1/automations/{new_id}",
                    json={"description": "bad ver", "expected_config_version": invalid_ver},
                )
            ).status_code == 400
            assert (
                await client.post(
                    f"/api/v1/automations/{new_id}/pause",
                    json={"expected_config_version": invalid_ver},
                )
            ).status_code == 400
            assert (
                await client.post(
                    f"/api/v1/automations/{new_id}/resume",
                    json={"expected_config_version": invalid_ver},
                )
            ).status_code == 400

        # 4. Concurrent updates carrying the same expected_config_version=4:
        #    SELECT ... FOR UPDATE serializes them so exactly one succeeds (v5) and one fails (409)!
        res_a, res_b = await asyncio.gather(
            client.patch(
                f"/api/v1/automations/{new_id}",
                json={"description": "Concurrent A", "expected_config_version": 4},
            ),
            client.patch(
                f"/api/v1/automations/{new_id}",
                json={"description": "Concurrent B", "expected_config_version": 4},
            ),
        )
        statuses = sorted([res_a.status_code, res_b.status_code])
        assert statuses == [200, 409], (res_a.text, res_b.text)

        after_conc = (await client.get(f"/api/v1/automations/{new_id}/history")).json()
        assert after_conc["config_version"] == 5
        assert after_conc["active_version"] == 5
        assert after_conc["total"] == 5

        # 4b. If history preparation raises an exception, transaction rolls back and row stays at v5
        orig_prep = company_auto_db.prepare_update_history_columns

        async def _boom_prep(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Simulated history snapshot failure")

        monkeypatch.setattr(company_auto_db, "prepare_update_history_columns", _boom_prep)
        failed_update = await client.patch(
            f"/api/v1/automations/{new_id}",
            json={"description": "Should rollback on history failure"},
        )
        assert failed_update.status_code == 500, failed_update.text
        monkeypatch.setattr(company_auto_db, "prepare_update_history_columns", orig_prep)
        after_boom = (await client.get(f"/api/v1/automations/{new_id}/history")).json()
        assert after_boom["config_version"] == 5
        assert after_boom["total"] == 5

        # 5. Corrupt history rows refuse PATCH, pause, resume with 409 without overwriting DB state
        for cid, exp_ver in ((corrupt_empty_id, 3), (corrupt_missing_id, 2)):
            assert (
                await client.patch(
                    f"/api/v1/automations/{cid}",
                    json={"description": "should fail"},
                )
            ).status_code == 409
            assert (await client.post(f"/api/v1/automations/{cid}/pause")).status_code == 409
            assert (await client.post(f"/api/v1/automations/{cid}/resume")).status_code == 409

            async with au1_live_pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT config_version, description FROM automations WHERE automation_id = %s::uuid",
                        (cid,),
                    )
                    unchanged_row = await cur.fetchone()
                    assert unchanged_row["config_version"] == exp_ver
                    assert unchanged_row["description"] != "should fail"


@pytest.mark.asyncio
async def test_au1_batch_a_once_schedule_preservation_and_automation_file_coalescing_real_db(
    au1_live_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    from ptc_agent.agent.backends.automations import AutomationsBackend
    from ptc_agent.agent.backends.db_json_route import UserDataValidationError
    from ptc_agent.core.sandbox.livefs_mount import CallContext
    from src.server.services.automations import file as file_mod

    uid = "u-hist-once-file"
    ws_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    due_once_dt = datetime(2026, 5, 1, 14, 30, tzinfo=UTC)
    future_once_dt = datetime(2030, 6, 15, 10, 0, tzinfo=UTC)
    legacy_claimed_dt = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
    legacy_sched_dt = datetime(2026, 4, 10, 8, 59, 58, tzinfo=UTC)
    legacy_once_single_id = str(uuid.uuid4())
    legacy_once_ambig_id = str(uuid.uuid4())

    async def _hist_row(automation_id: str) -> dict[str, Any]:
        row = await company_auto_db.get_automation_history_row(automation_id)
        assert row is not None
        return row

    async with au1_live_pool.connection() as conn:
        await _insert_user(conn, uid, "once-file@dalton.test")
        await _insert_workspace(conn, ws_id, uid, "Once-File-WS")

        # Seed two legacy pre-upgrade once tasks already claimed before d035 (next_run_at IS NULL, audit_history=[])
        await conn.execute(
            """
            INSERT INTO automations (
                automation_id, user_id, name, file_name, description, trigger_type,
                timezone, next_run_at, last_run_at, instruction, workspace_id,
                status, scope, config_version, active_version, audit_history, metadata
            )
            VALUES
                (%s::uuid, %s, 'Legacy Claimed Once Single', 'leg-once-1.json', 'Before edit', 'once',
                 'Asia/Shanghai', NULL, %s, 'Run once', %s::uuid,
                 'completed', 'personal', 1, 1, '[]'::jsonb, '{}'::jsonb),
                (%s::uuid, %s, 'Legacy Claimed Once Ambiguous', 'leg-once-2.json', 'Before edit', 'once',
                 'Asia/Shanghai', NULL, %s, 'Run once ambig', %s::uuid,
                 'completed', 'personal', 1, 1, '[]'::jsonb, '{}'::jsonb)
            """,
            (
                legacy_once_single_id,
                uid,
                legacy_claimed_dt,
                ws_id,
                legacy_once_ambig_id,
                uid,
                legacy_claimed_dt,
                ws_id,
            ),
        )
        # 1 matching execution row for legacy_once_single_id; 2 matching execution rows for legacy_once_ambig_id
        await conn.execute(
            """
            INSERT INTO automation_executions (
                automation_execution_id, automation_id, status, scheduled_at, started_at, created_at
            )
            VALUES
                (%s::uuid, %s::uuid, 'completed', %s, %s, %s),
                (%s::uuid, %s::uuid, 'completed', %s, %s, %s),
                (%s::uuid, %s::uuid, 'completed', %s, %s, %s)
            """,
            (
                str(uuid.uuid4()),
                legacy_once_single_id,
                legacy_sched_dt,
                legacy_claimed_dt,
                legacy_claimed_dt,
                str(uuid.uuid4()),
                legacy_once_ambig_id,
                legacy_sched_dt,
                legacy_claimed_dt,
                legacy_claimed_dt,
                str(uuid.uuid4()),
                legacy_once_ambig_id,
                datetime(2026, 4, 10, 8, 59, 59, tzinfo=UTC),
                legacy_claimed_dt,
                legacy_claimed_dt,
            ),
        )

    # 1. Create a new once automation and claim it via claim_due_automations
    once_created = await auto_db.create_automation(
        user_id=uid,
        name="New Due Once Task",
        trigger_type="once",
        next_run_at=due_once_dt,
        instruction="Execute one-time analysis",
        workspace_id=ws_id,
        timezone="Asia/Shanghai",
    )
    once_id = str(once_created["automation_id"])
    assert once_created["config_version"] == 1
    assert once_created["active_version"] == 1
    assert once_created["metadata"]["once_schedule_at"] == due_once_dt.isoformat()
    once_hist_1 = await _hist_row(once_id)
    assert once_hist_1["audit_history"][0]["snapshot"]["once_run_at"] == due_once_dt.isoformat()

    claimed_rows = await auto_db.claim_due_automations(
        now=due_once_dt + timedelta(seconds=1),
        server_id="test-server",
        limit=10,
    )
    claimed_target = next(r for r in claimed_rows if str(r["automation_id"]) == once_id)
    assert claimed_target["_execution_id"]
    assert claimed_target["status"] == "active"
    assert claimed_target["next_run_at"] == due_once_dt
    assert claimed_target["metadata"]["once_schedule_at"] == due_once_dt.isoformat()
    # Claiming MUST NOT increment config_version or append history!
    assert claimed_target["config_version"] == 1
    assert claimed_target["active_version"] == 1

    persisted_after_claim = await auto_db.get_automation(once_id, uid)
    assert persisted_after_claim is not None
    assert persisted_after_claim["status"] == "active"
    assert persisted_after_claim["next_run_at"] is None
    assert persisted_after_claim["last_run_at"] is not None
    assert persisted_after_claim["metadata"]["once_schedule_at"] == due_once_dt.isoformat()
    assert persisted_after_claim["config_version"] == 1
    assert persisted_after_claim["active_version"] == 1
    once_hist_claimed = await _hist_row(once_id)
    assert len(once_hist_claimed["audit_history"]) == 1

    async with au1_live_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT status, scheduled_at, server_id
                FROM automation_executions
                WHERE automation_execution_id = %s::uuid
                """,
                (claimed_target["_execution_id"],),
            )
            claimed_exec = await cur.fetchone()
            assert claimed_exec is not None
            assert claimed_exec["status"] == "pending"
            assert claimed_exec["scheduled_at"] == due_once_dt
            assert claimed_exec["server_id"] == "test-server"

    # 2. Non-schedule edit after claim keeps next_run_at IS NULL in DB while snapshot once_run_at is preserved!
    updated_once = await lifecycle_mod.update_automation(
        once_id,
        uid,
        {"description": "Edited after claim"},
    )
    assert updated_once is not None
    assert updated_once["next_run_at"] is None
    assert updated_once["config_version"] == 2
    once_hist_2 = await _hist_row(once_id)
    assert len(once_hist_2["audit_history"]) == 2
    v2_once = once_hist_2["audit_history"][-1]
    assert v2_once["action"] == "update"
    assert v2_once["changes"] == {"description": {"old": None, "new": "Edited after claim"}}
    assert v2_once["snapshot"]["once_run_at"] == due_once_dt.isoformat()

    # 3. Legacy empty-history once task claimed before d035:
    #    - 1 matching execution row -> seed v1 and v2 recover scheduled_at!
    #    - 2 matching execution rows -> refuses to guess -> once_run_at is None!
    upd_leg_single = await lifecycle_mod.update_automation(
        legacy_once_single_id,
        uid,
        {"description": "After edit single"},
    )
    assert upd_leg_single is not None
    assert upd_leg_single["config_version"] == 2
    leg_single_hist = await _hist_row(legacy_once_single_id)
    assert [e["action"] for e in leg_single_hist["audit_history"]] == ["seed", "update"]
    assert leg_single_hist["audit_history"][0]["snapshot"]["once_run_at"] == legacy_sched_dt.isoformat()
    assert leg_single_hist["audit_history"][1]["snapshot"]["once_run_at"] == legacy_sched_dt.isoformat()

    upd_leg_ambig = await lifecycle_mod.update_automation(
        legacy_once_ambig_id,
        uid,
        {"description": "After edit ambig"},
    )
    assert upd_leg_ambig is not None
    assert upd_leg_ambig["config_version"] == 2
    leg_ambig_hist = await _hist_row(legacy_once_ambig_id)
    assert leg_ambig_hist["audit_history"][0]["snapshot"]["once_run_at"] is None
    assert leg_ambig_hist["audit_history"][1]["snapshot"]["once_run_at"] is None

    # 4. AutomationFile create with status="paused" -> single v1 entry (desired_status="paused", config_version=1)
    root_prefix = "/home/workspace/.agents/user/automations"
    backend = AutomationsBackend(
        user_id=uid,
        call=CallContext(workspace_id=ws_id, thread_id=thread_id, timezone="Asia/Shanghai"),
        sandbox_backend=MagicMock(),
        root_prefix=root_prefix,
    )

    async def _livefs_save(file_name: str, doc: dict[str, Any]) -> dict[str, Any]:
        path = f"{root_prefix}/{file_name}"
        await backend.aread_range(path, 0, 2000)
        return await backend.awrite_text(path, json.dumps(doc))

    res_file_create = await _livefs_save(
        "file-created-paused.json",
        {
            "name": "File Created Paused",
            "description": "Created paused from livefs",
            "trigger_type": "cron",
            "cron_expression": "0 9 * * 1-5",
            "timezone": "Asia/Shanghai",
            "status": "paused",
            "instruction": "Do paused check",
        },
    )
    assert "created" in res_file_create["message"].lower()
    async with au1_live_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM automations WHERE user_id = %s AND file_name = 'file-created-paused.json'",
                (uid,),
            )
            file_paused_row = await cur.fetchone()
            assert file_paused_row is not None
            assert file_paused_row["status"] == "paused"
            assert file_paused_row["config_version"] == 1
            assert file_paused_row["active_version"] == 1
            assert len(file_paused_row["audit_history"]) == 1
            assert file_paused_row["audit_history"][0]["action"] == "create"
            assert file_paused_row["audit_history"][0]["snapshot"]["desired_status"] == "paused"

    # 4b. AutomationFile create with status="paused" rolls back create_automation if pause_automation fails
    orig_pause = lifecycle_mod.pause_automation

    async def _boom_pause(*args: Any, **kwargs: Any) -> Any:
        raise HTTPException(status_code=409, detail="Simulated pause failure")

    monkeypatch.setattr(file_mod.lifecycle, "pause_automation", _boom_pause)
    with pytest.raises(UserDataValidationError):
        await _livefs_save(
            "file-rollback-create.json",
            {
                "name": "Should Rollback Create",
                "trigger_type": "cron",
                "cron_expression": "0 9 * * 1-5",
                "timezone": "Asia/Shanghai",
                "status": "paused",
                "instruction": "Never persisted",
            },
        )
    monkeypatch.setattr(file_mod.lifecycle, "pause_automation", orig_pause)

    async with au1_live_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT COUNT(*) AS cnt FROM automations WHERE user_id = %s AND file_name = 'file-rollback-create.json'",
                (uid,),
            )
            assert (await cur.fetchone())["cnt"] == 0

    # 5. AutomationFile simultaneous field edit + status change -> single version bump (v1 -> v2)
    res_file_upd = await _livefs_save(
        "file-created-paused.json",
        {
            "name": "File Created Paused",
            "description": "Updated desc + resumed in one save",
            "trigger_type": "cron",
            "cron_expression": "0 9 * * 1-5",
            "timezone": "Asia/Shanghai",
            "status": "active",
            "instruction": "Do resumed check",
        },
    )
    assert "updated" in res_file_upd["message"].lower() or "resumed" in res_file_upd["message"].lower()
    async with au1_live_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM automations WHERE automation_id = %s::uuid",
                (str(file_paused_row["automation_id"]),),
            )
            coalesced_row = await cur.fetchone()
            assert coalesced_row["status"] == "active"
            assert coalesced_row["config_version"] == 2
            assert coalesced_row["active_version"] == 2
            assert len(coalesced_row["audit_history"]) == 2
            v2_coal = coalesced_row["audit_history"][1]
            assert v2_coal["version"] == 2
            assert v2_coal["prev_version"] == 1
            assert v2_coal["changes"] == {
                "description": {
                    "old": "Created paused from livefs",
                    "new": "Updated desc + resumed in one save",
                },
                "instruction": {
                    "old": "Do paused check",
                    "new": "Do resumed check",
                },
                "desired_status": {
                    "old": "paused",
                    "new": "active",
                },
            }

    # 6. AutomationFile simultaneous field edit + Step 2 failure in commit():
    #    Step 2 raises 409, and outer transaction rolls back Step 1's field edit + history completely!
    paused_future_once = await auto_db.create_automation(
        user_id=uid,
        name="Paused Future Once",
        file_name="paused-future-once.json",
        description="Original description",
        trigger_type="once",
        next_run_at=future_once_dt,
        instruction="Original instruction",
        workspace_id=ws_id,
        timezone="Asia/Shanghai",
        status="paused",
    )
    future_once_id = str(paused_future_once["automation_id"])
    assert paused_future_once["config_version"] == 1

    orig_resume = lifecycle_mod.resume_automation

    async def _boom_resume(*args: Any, **kwargs: Any) -> Any:
        raise HTTPException(status_code=409, detail="Simulated resume failure after field update")

    monkeypatch.setattr(file_mod.lifecycle, "resume_automation", _boom_resume)
    with pytest.raises(UserDataValidationError) as exc_info:
        await _livefs_save(
            "paused-future-once.json",
            {
                "name": "Paused Future Once",
                "description": "Mutated description that MUST rollback",
                "trigger_type": "once",
                "next_run_at": "2030-06-15T18:00:00+08:00",
                "timezone": "Asia/Shanghai",
                "status": "active",
                "instruction": "Mutated instruction that MUST rollback",
            },
        )
    assert "Simulated resume failure" in str(exc_info.value)
    monkeypatch.setattr(file_mod.lifecycle, "resume_automation", orig_resume)

    async with au1_live_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM automations WHERE automation_id = %s::uuid",
                (future_once_id,),
            )
            after_rb = await cur.fetchone()
            assert after_rb["status"] == "paused"
            assert after_rb["description"] == "Original description"
            assert after_rb["instruction"] == "Original instruction"
            assert after_rb["config_version"] == 1
            assert after_rb["active_version"] == 1
            assert len(after_rb["audit_history"]) == 1


@pytest.mark.asyncio
async def test_au1_batch_a_dalton_schedule_system_jobs_tracking_and_runtime_isolation_real_db(
    au1_live_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.autonomy import (
        system_jobs as sys_jobs_mod,
        tracking as tracking_mod,
    )
    from src.server.services.automation_scheduler import AutomationScheduler

    uid = "u-hist-dalton-paths"
    pm_actor = "u-hist-pm-actor"
    ws_nvda = str(uuid.uuid4())
    ws_sector_old = str(uuid.uuid4())
    ws_sector_new = str(uuid.uuid4())
    sector_unit_id = str(uuid.uuid4())
    home_id = get_home_workspace_id(uid)

    async def _hist_row(automation_id: str) -> dict[str, Any]:
        row = await company_auto_db.get_automation_history_row(automation_id)
        assert row is not None
        return row

    async with au1_live_pool.connection() as conn:
        await _insert_user(conn, uid, "dalton-paths@dalton.test")
        await _insert_user(conn, pm_actor, "pm-actor@dalton.test")
        await _insert_workspace(conn, home_id, uid, "Home")
        await _insert_workspace(conn, ws_nvda, uid, "NVDA")
        await _insert_workspace(conn, ws_sector_old, uid, "Old Sector WS")
        await _insert_workspace(conn, ws_sector_new, uid, "New Sector WS")
        await conn.execute(
            """
            INSERT INTO dalton_coverage (user_id, ticker, name, tier)
            VALUES (%s, 'NVDA', 'NVIDIA', 'core')
            """,
            (uid,),
        )
        await conn.execute(
            """
            INSERT INTO dalton_coverage_units (
                unit_id, kind, owner_user_id, slug, display_name, workspace_id, ticker, status
            )
            VALUES (%s::uuid, 'sector', %s, 'semi', 'Semiconductors', %s::uuid, NULL, 'active')
            """,
            (sector_unit_id, uid, ws_sector_new),
        )

    # 1. schedule_dalton_automation_helper: create -> idempotent repeat -> update -> pause -> resume
    c_res = await schedule_dalton_automation_helper(
        name="dalton-nvda-custom-scan",
        workspace_id=ws_nvda,
        spec={
            "description": "Custom scan v1",
            "instruction": "Scan NVDA supply chain v1",
            "cron": "0 9 * * 1-5",
            "timezone": "America/New_York",
        },
        user_id=uid,
        role="chief_of_staff",
    )
    assert c_res["ok"] is True
    assert c_res["action"] == "created"
    custom_id = c_res["automation_id"]

    # Repeat identical upsert -> skip_if_unchanged=True keeps config_version=1!
    c_repeat = await schedule_dalton_automation_helper(
        name="dalton-nvda-custom-scan",
        workspace_id=ws_nvda,
        spec={
            "description": "Custom scan v1",
            "instruction": "Scan NVDA supply chain v1",
            "cron": "0 9 * * 1-5",
            "timezone": "America/New_York",
        },
        user_id=uid,
        role="chief_of_staff",
    )
    assert c_repeat["ok"] is True
    row_after_repeat = await _hist_row(custom_id)
    assert row_after_repeat["config_version"] == 1
    assert len(row_after_repeat["audit_history"]) == 1

    # Upsert with changed instruction -> v2 (action="update")
    await schedule_dalton_automation_helper(
        name="dalton-nvda-custom-scan",
        workspace_id=ws_nvda,
        spec={
            "description": "Custom scan v1",
            "instruction": "Scan NVDA supply chain v2",
            "cron": "0 9 * * 1-5",
            "timezone": "America/New_York",
        },
        user_id=uid,
        role="chief_of_staff",
    )
    # Pause -> v3; repeat pause -> stays v3; Resume -> v4; repeat resume -> stays v4
    await schedule_dalton_automation_helper(
        name="dalton-nvda-custom-scan",
        workspace_id=ws_nvda,
        action="pause",
        user_id=uid,
        role="chief_of_staff",
    )
    await schedule_dalton_automation_helper(
        name="dalton-nvda-custom-scan",
        workspace_id=ws_nvda,
        action="pause",
        user_id=uid,
        role="chief_of_staff",
    )
    await schedule_dalton_automation_helper(
        name="dalton-nvda-custom-scan",
        workspace_id=ws_nvda,
        action="resume",
        user_id=uid,
        role="chief_of_staff",
    )
    await schedule_dalton_automation_helper(
        name="dalton-nvda-custom-scan",
        workspace_id=ws_nvda,
        action="resume",
        user_id=uid,
        role="chief_of_staff",
    )
    row_custom = await _hist_row(custom_id)
    assert row_custom["config_version"] == 4
    assert [e["action"] for e in row_custom["audit_history"]] == [
        "create",
        "update",
        "pause",
        "resume",
    ]

    # 2. toggle_system_job on Home job (`dalton-earnings-calendar`) + idempotent repeat calls
    await sys_jobs_mod.toggle_system_job(
        uid, "dalton-earnings-calendar", enabled=True, actor_user_id=pm_actor
    )
    await sys_jobs_mod.toggle_system_job(
        uid, "dalton-earnings-calendar", enabled=True, actor_user_id=pm_actor
    )
    await sys_jobs_mod.toggle_system_job(
        uid, "dalton-earnings-calendar", enabled=False, actor_user_id=uid
    )
    await sys_jobs_mod.toggle_system_job(
        uid, "dalton-earnings-calendar", enabled=False, actor_user_id=uid
    )
    await sys_jobs_mod.toggle_system_job(
        uid, "dalton-earnings-calendar", enabled=True, actor_user_id=pm_actor
    )
    async with au1_live_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM automations WHERE user_id = %s AND name = 'dalton-earnings-calendar'",
                (uid,),
            )
            cal_row = await cur.fetchone()
            assert cal_row is not None
            assert cal_row["created_by"] == pm_actor
            assert cal_row["updated_by"] == pm_actor
            assert cal_row["config_version"] == 3
            assert [e["action"] for e in cal_row["audit_history"]] == [
                "create",
                "pause",
                "resume",
            ]
            assert [e["actor_user_id"] for e in cal_row["audit_history"]] == [
                pm_actor,
                uid,
                pm_actor,
            ]

    # 3. enable_tracking / disable_tracking / resync_delta_schedule / _pause_stale_sector_rows
    monkeypatch.setattr(tracking_mod, "_resolve_workspace_ticker", lambda _w, _r=None: _coro("NVDA"))
    await tracking_mod.enable_tracking(uid, ws_nvda, tier="core", force=True)
    # Repeat enable_tracking with same tier -> no version bump!
    await tracking_mod.enable_tracking(uid, ws_nvda, tier="core", force=True)
    await tracking_mod.disable_tracking(uid, ws_nvda)
    # Repeat disable_tracking -> no version bump!
    await tracking_mod.disable_tracking(uid, ws_nvda)
    await tracking_mod.enable_tracking(uid, ws_nvda, tier="core", force=True)
    # Resync delta schedule to watch -> bumps delta task to v4; repeat with watch -> stays v4!
    await tracking_mod.resync_delta_schedule(uid, "NVDA", tier="watch")
    await tracking_mod.resync_delta_schedule(uid, "NVDA", tier="watch")

    async with au1_live_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM automations WHERE user_id = %s AND name = 'dalton-nvda-delta'",
                (uid,),
            )
            delta_row = await cur.fetchone()
            assert delta_row is not None
            assert delta_row["config_version"] == 4
            assert [e["action"] for e in delta_row["audit_history"]] == [
                "create",
                "pause",
                "resume",
                "update",
            ]

    # Test _pause_stale_sector_rows: records action="maintenance_pause_stale_binding" and actor_user_id=None
    stale_sector_created = await auto_db.create_automation(
        user_id=uid,
        name="dalton-sector-semi-agenda",
        trigger_type="cron",
        cron_expression="0 */2 * * 1-5",
        timezone="Asia/Shanghai",
        instruction="Old workspace sector agenda",
        workspace_id=ws_sector_old,
        metadata={
            "dalton_managed": True,
            "dalton_kind": "sector_watch",
            "subject_kind": "sector",
            "unit_id": sector_unit_id,
            "sector_slug": "semi",
        },
        scope="research_tracking",
        created_by=uid,
    )
    stale_id = str(stale_sector_created["automation_id"])
    paused_stale = await tracking_mod._pause_stale_sector_rows(
        uid, sector_unit_id, ws_sector_new
    )
    assert paused_stale == ["dalton-sector-semi-agenda"]
    stale_row = await auto_db.get_automation(stale_id, uid)
    assert stale_row is not None
    assert stale_row["status"] == "paused"
    assert stale_row["config_version"] == 2
    assert stale_row["updated_by"] is None
    stale_after = await _hist_row(stale_id)
    assert stale_after["config_version"] == 2
    assert stale_after["audit_history"][-1]["action"] == "maintenance_pause_stale_binding"
    assert stale_after["audit_history"][-1]["actor_user_id"] is None
    assert stale_after["audit_history"][-1]["snapshot"]["desired_status"] == "paused"

    # 4. Runtime execution paths (trigger_automation + executor thread write-back + failure_count/last_run_at) DO NOT bump config_version!
    fake_sched = create_autospec(AutomationScheduler, instance=True)
    fake_sched.server_id = "srv-test"
    monkeypatch.setattr(AutomationScheduler, "get_instance", lambda: fake_sched)

    before_runtime = await _hist_row(custom_id)
    assert before_runtime is not None
    trig_res = await lifecycle_mod.trigger_automation(custom_id, uid)
    assert trig_res["status"] == "triggered"

    # Simulate AutomationExecutor._resolve_thread writing conversation_thread_id with record_history=False
    runtime_thread_id = str(uuid.uuid4())
    with company_auto_svc.automation_history_context(record_history=False):
        await auto_db.update_automation(
            custom_id,
            uid,
            conversation_thread_id=runtime_thread_id,
        )

    # Simulate runtime failure_count / last_run_at update without history context
    await auto_db.update_automation(
        custom_id,
        uid,
        failure_count=1,
        last_run_at=datetime.now(UTC),
    )

    after_runtime_row = await auto_db.get_automation(custom_id, uid)
    assert after_runtime_row is not None
    assert str(after_runtime_row["conversation_thread_id"]) == runtime_thread_id
    assert after_runtime_row["failure_count"] == 1
    assert after_runtime_row["config_version"] == before_runtime["config_version"]
    assert after_runtime_row["active_version"] == before_runtime["active_version"]
    after_runtime = await _hist_row(custom_id)
    assert after_runtime["config_version"] == before_runtime["config_version"]
    assert after_runtime["active_version"] == before_runtime["active_version"]
    assert after_runtime["audit_history"] == before_runtime["audit_history"]
