"""Real PostgreSQL + ASGI HTTP integration tests for AU1 Batch 1 (`d035_company_automations`)."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime
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
from fastapi import FastAPI, WebSocket, WebSocketException
from httpx import ASGITransport, AsyncClient
import psycopg
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import pytest
import pytest_asyncio
from sqlalchemy import create_engine, pool

from src.dalton import access_tiers, auth as dalton_auth
from src.dalton.autonomy.classification import classify_automation_scope
from src.dalton.autonomy.schedule import schedule_dalton_automation_helper
from src.dalton.budget.service import resolve_user_budget_config
from src.dalton.company_identity import (
    COMPANY_SYSTEM_EMAIL,
    COMPANY_SYSTEM_USER_ID,
    ensure_company_system_identity,
    reject_company_system_auth,
)
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
    from src.dalton import company_identity as company_identity_mod
    from src.dalton.autonomy import schedule as dalton_schedule_mod
    from src.dalton.database import (
        budget as dalton_budget_db,
        company_automations as company_auto_db,
        members as dalton_members_db,
    )
    from src.dalton.notify import dispatch as dalton_notify_mod
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
        company_identity_mod,
        dalton_schedule_mod,
        dalton_budget_db,
        company_auto_db,
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

    async with au1_live_pool.connection() as db_conn:
        await db_conn.execute(
            "UPDATE dalton_data_task_defs SET migration_state = '{}'::jsonb WHERE job_key = 'cn-leverage-daily'"
        )


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

    orig_decode_token = jwt_bearer._decode_token

    async def _decode_company(_tok: str) -> jwt_bearer.AuthInfo:
        reject_company_system_auth(COMPANY_SYSTEM_USER_ID)
        return jwt_bearer.AuthInfo(user_id=COMPANY_SYSTEM_USER_ID)

    monkeypatch.setattr(ws_auth, "HOST_MODE", "saas")
    monkeypatch.setattr(
        ws_auth,
        "_decode_token",
        create_autospec(orig_decode_token, side_effect=_decode_company),
    )
    ws = create_autospec(WebSocket, instance=True)
    ws.headers = {"authorization": "Bearer company_jwt"}
    ws.query_params = {}
    with pytest.raises(WebSocketException) as ws_exc:
        await ws_auth.authenticate_websocket(ws)
    assert ws_exc.value.code == 1008


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
