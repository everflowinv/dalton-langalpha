"""Real PostgreSQL and ASGI coverage for the unified automation read API.

Auth identity is faked. Catalog, execution, and history rows use the migrated
schema and the real routes. The live pool comes from the AU1 foundation
fixture so ``company_data_tasks`` is patched onto the test database.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from typing import Any
from unittest.mock import create_autospec

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import pytest
import pytest_asyncio

from src.dalton import access_tiers
from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.data_jobs.container_runner import LOG_TAIL_MAX_BYTES
from src.dalton.data_jobs.manifest import load_manifest_entries, sync_manifest_to_db
from src.dalton.services import company_automations as company_auto_svc
from src.dalton.services.company_task_read import ALLOWED_SCRIPT_SNAPSHOT_FIELD_SET
from src.server.app import automations as automations_app
from src.server.database.home_workspace import get_home_workspace_id
from src.server.dependencies import principal as principal_mod
from tests.integration.dalton.test_au1_company_foundation_db import (
    _insert_computer,
    _insert_user,
    _insert_workspace,
    au1_live_pool as _foundation_au1_live_pool,
)

pytestmark = pytest.mark.integration

INTERNAL = "u-read-internal"
EXTERNAL = "u-read-external"
OTHER = "u-read-other"
ADMIN = "u-read-admin"
AGENT_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"
DELETED_AGENT_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
ALPHA_ID = "0a00a85c-d5a1-4e79-aee3-54a7f11c7705"
CATCHUP_ID = "5689ebf3-7ffc-48f7-a5d8-3cba1cd219fa"
HEALTH_ID = "71000000-0000-4000-8000-000000000001"
SAME_ID = "72000000-0000-4000-8000-000000000002"
EARNINGS_ID = "73000000-0000-4000-8000-000000000003"
ALPHA_NAME = "AlphaEngine 机构会议增量整理入库"
BBG = "bbg-data-pull-asia"
AH_AM = "ah-premium-am"
AH_PM = "ah-premium-pm"
CN = "cn-leverage-daily"
JP = "jp-leverage-daily"
TSMC = "tsmc-adr-premium"
OUT_KEY = "not-a-company-script"
PLANTED = "PLANTED_SECRET_VALUE"
SECRET_KEY = "sekret-key"
HOOK_SECRET = "hook-secret"
CATALOG_TIE = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
PERSONAL_AT = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
EXEC_NEW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
EXEC_TIE = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
EXEC_OLD = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
EXEC_OLDER = datetime(2026, 11, 10, 7, 0, tzinfo=UTC)
BBG_COMPLETED = datetime(2026, 11, 10, 12, 5, tzinfo=UTC)
RUN_NEW = "11111111-1111-4111-8111-111111111111"
RUN_OLD = "22222222-2222-4222-8222-222222222222"
RUN_FAIL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OUT_RUN = "44444444-4444-4444-8444-444444444444"
AGENT_EXEC_TIE = "ffffffff-ffff-4fff-8fff-fffffffffffe"
AGENT_EXEC_OLD = "12121212-1212-4212-8212-121212121212"
THREAD_ID = "34343434-3434-4434-8434-343434343434"
PERSONAL_EXEC = "99999999-9999-4999-8999-999999999999"
HOME_EXEC = "88888888-8888-4888-8888-888888888888"
RESEARCH_EXEC = "77777777-7777-4777-8777-777777777777"


@dataclass
class Seed:
    comp_a: str
    comp_b: str
    ws_company: str
    keys: list[str]
    bbg_title: str
    bbg_cron: str


@pytest_asyncio.fixture
async def au1_live_pool(
    dalton_schema,
    cleanup_dalton_tables,
    test_db_uri: str,
):
    """Run the foundation pool against this test's database connection."""
    raw = getattr(_foundation_au1_live_pool, "__wrapped__", _foundation_au1_live_pool)
    generator = raw(dalton_schema, cleanup_dalton_tables, test_db_uri)
    pool = await anext(generator)
    try:
        yield pool
    finally:
        await anext(generator, None)


def _script(job_key: str) -> str:
    return f"data:{job_key}"


def _parsed(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _ids(body: dict[str, Any]) -> list[str]:
    return [item["automation_id"] for item in body["automations"]]


def _session(user_id: str) -> principal_mod.Principal:
    return principal_mod.Principal(user_id=user_id, auth="session")


def _computer(
    computer_id: str,
    user_id: str = INTERNAL,
) -> principal_mod.Principal:
    return principal_mod.Principal(
        user_id=user_id,
        auth="computer",
        scopes=("agent:read",),
        computer_id=computer_id,
    )


@asynccontextmanager
async def _frozen_updated_at(conn: psycopg.AsyncConnection):
    await conn.execute(
        "ALTER TABLE automations DISABLE TRIGGER trg_automations_updated_at"
    )
    try:
        yield
    finally:
        await conn.execute(
            "ALTER TABLE automations ENABLE TRIGGER trg_automations_updated_at"
        )


async def _insert_automation(
    conn: psycopg.AsyncConnection,
    *,
    automation_id: str,
    user_id: str,
    name: str,
    workspace_id: str,
    scope: str,
    created_by: str,
    created_at: datetime,
    notify_storage_mode: str,
    metadata: dict[str, Any] | None = None,
    delivery_config: dict[str, Any] | None = None,
    notify_routes: list[str] | None = None,
    notify_enabled: bool = False,
    audit_history: list[dict[str, Any]] | None = None,
    config_version: int = 1,
    description: str = "desc",
    deleted_at: datetime | None = None,
    updated_by: str | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO automations (
            automation_id, user_id, name, file_name, description, trigger_type,
            cron_expression, timezone, instruction, workspace_id,
            thread_strategy, status, max_failures, failure_count,
            delivery_config, metadata,
            scope, notify_enabled, notify_routes, notify_storage_mode,
            config_version, active_version, created_by, updated_by,
            audit_history, deleted_at, created_at, updated_at
        )
        VALUES (
            %s::uuid, %s, %s, %s, %s, 'cron',
            '0 9 * * *', 'Asia/Shanghai', 'Run', %s::uuid,
            'new', 'active', 3, 0,
            %s::jsonb, %s::jsonb,
            %s, %s, %s::jsonb, %s,
            %s, %s, %s, %s,
            %s::jsonb, %s, %s, %s
        )
        """,
        (
            automation_id,
            user_id,
            name,
            f"{automation_id}.json",
            description,
            workspace_id,
            json.dumps(delivery_config or {"methods": []}),
            json.dumps(metadata or {}),
            scope,
            notify_enabled,
            json.dumps(notify_routes or []),
            notify_storage_mode,
            config_version,
            config_version,
            created_by,
            updated_by,
            json.dumps(audit_history or []),
            deleted_at,
            created_at,
            created_at,
        ),
    )


async def _insert_execution(
    conn: psycopg.AsyncConnection,
    *,
    execution_id: str,
    automation_id: str,
    status: str,
    created_at: datetime,
    thread_id: str | None = None,
    excerpt: str | None = None,
    error_message: str | None = None,
    delivery_result: list[dict[str, Any]] | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO automation_executions (
            automation_execution_id, automation_id, status,
            conversation_thread_id, scheduled_at, created_at,
            result_excerpt, error_message, delivery_result
        )
        VALUES (%s::uuid, %s::uuid, %s, %s::uuid, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            execution_id,
            automation_id,
            status,
            thread_id,
            created_at,
            created_at,
            excerpt,
            error_message,
            json.dumps(delivery_result) if delivery_result is not None else None,
        ),
    )


async def _insert_thread(
    conn: psycopg.AsyncConnection,
    *,
    thread_id: str,
    workspace_id: str,
    thread_index: int,
) -> None:
    await conn.execute(
        """
        INSERT INTO conversation_threads (
            conversation_thread_id, workspace_id, current_status, thread_index
        )
        VALUES (%s::uuid, %s::uuid, 'completed', %s)
        """,
        (thread_id, workspace_id, thread_index),
    )


async def _insert_run(
    conn: psycopg.AsyncConnection,
    *,
    run_id: str,
    job_key: str,
    status: str,
    created_at: datetime,
    slot: datetime,
    triggered_by: str | None = None,
    log_tail: str | None = None,
    exit_code: int = 0,
    failure_reason: str | None = None,
    completed_at: datetime | None = None,
    notify_status: str | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO dalton_data_task_runs (
            run_id, job_key, trigger_source, triggered_by_user_id,
            scheduled_slot_utc, deadline_utc, status, started_at, completed_at,
            exit_code, failure_reason, log_tail, notify_status, created_at
        )
        VALUES (
            %s::uuid, %s, 'cron', %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s
        )
        """,
        (
            run_id,
            job_key,
            triggered_by,
            slot,
            slot + timedelta(hours=1),
            status,
            created_at,
            completed_at,
            exit_code,
            failure_reason,
            log_tail,
            notify_status,
            created_at,
        ),
    )


async def _history_bytes(
    pool: AsyncConnectionPool,
    *,
    table: str,
    key_column: str,
    key: str,
    cast_uuid: bool = False,
) -> dict[str, Any]:
    compare = "%s::uuid" if cast_uuid else "%s"
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT config_version, active_version, audit_history, updated_at
                FROM {table}
                WHERE {key_column} = {compare}
                """,
                (key,),
            )
            row = await cur.fetchone()
    assert row is not None
    return dict(row)


async def _seed_identity(pool: AsyncConnectionPool) -> Seed:
    entries = load_manifest_entries()
    keys = [entry.job_key for entry in entries]
    assert len(keys) == 28
    for required in (BBG, AH_AM, AH_PM, CN, JP, TSMC):
        assert required in keys
    comp_a = "11111111-1111-4111-8111-111111111111"
    comp_b = "22222222-2222-4222-8222-222222222222"
    ws_a = "33333333-3333-4333-8333-333333333333"
    ws_b = "44444444-4444-4444-8444-444444444444"
    ws_company = "55555555-5555-4555-8555-555555555555"
    home = get_home_workspace_id(INTERNAL)
    bbg = next(entry for entry in entries if entry.job_key == BBG)
    agent_history = [
        {
            "version": 1,
            "prev_version": None,
            "actor_user_id": OTHER,
            "changed_at": "2026-11-08T00:00:00+00:00",
            "action": "seed",
            "changes": {},
            "snapshot": {
                "name": "Company Catalog Agent",
                "desired_status": "active",
                "trigger_type": "cron",
                "cron_expression": "0 9 * * *",
                "timezone": "Asia/Shanghai",
                "instruction": "Run",
                "notify_enabled": False,
                "notify_routes": ["feishu:chat:oc_company"],
                "metadata": {"api_key": SECRET_KEY},
            },
        }
    ]
    async with pool.connection() as conn:
        await _insert_user(conn, INTERNAL, "internal@dalton.test")
        await _insert_user(conn, EXTERNAL, "external@guest.test")
        await _insert_user(conn, OTHER, "other@dalton.test")
        await _insert_user(conn, ADMIN, "admin@dalton.test")
        await _insert_computer(conn, comp_a, INTERNAL, "Comp A")
        await _insert_computer(conn, comp_b, INTERNAL, "Comp B")
        await _insert_workspace(
            conn, home, INTERNAL, "Home", status="running", computer_id=comp_a
        )
        await _insert_workspace(
            conn, ws_a, INTERNAL, "WS A", status="running", computer_id=comp_a
        )
        await _insert_workspace(
            conn, ws_b, INTERNAL, "WS B", status="running", computer_id=comp_b
        )
        await _insert_workspace(
            conn, ws_company, COMPANY_SYSTEM_USER_ID, "Company WS", status="running"
        )
        await _insert_automation(
            conn,
            automation_id=AGENT_ID,
            user_id=COMPANY_SYSTEM_USER_ID,
            name="Company Catalog Agent",
            workspace_id=ws_company,
            scope="company",
            created_by=OTHER,
            updated_by=INTERNAL,
            created_at=CATALOG_TIE,
            notify_storage_mode="task_row",
            notify_enabled=False,
            notify_routes=["feishu:chat:oc_company"],
            metadata={
                "note": "visible-note",
                "api_key": SECRET_KEY,
                "pre_company_migration": {"user_id": INTERNAL},
            },
            delivery_config={"methods": ["slack"], "webhook_secret": HOOK_SECRET},
            audit_history=agent_history,
            config_version=1,
        )
        await _insert_automation(
            conn,
            automation_id=DELETED_AGENT_ID,
            user_id=COMPANY_SYSTEM_USER_ID,
            name="Deleted Company Agent",
            workspace_id=ws_company,
            scope="company",
            created_by=OTHER,
            created_at=CATALOG_TIE - timedelta(days=2),
            notify_storage_mode="task_row",
            deleted_at=CATALOG_TIE,
            audit_history=agent_history,
        )
        await _insert_automation(
            conn,
            automation_id=ALPHA_ID,
            user_id=INTERNAL,
            name=ALPHA_NAME,
            workspace_id=ws_a,
            scope="personal",
            created_by=INTERNAL,
            created_at=PERSONAL_AT,
            notify_storage_mode="legacy",
            description="alpha-personal",
        )
        await _insert_automation(
            conn,
            automation_id=CATCHUP_ID,
            user_id=INTERNAL,
            name="AlphaEngine historical catch-up",
            workspace_id=ws_a,
            scope="personal",
            created_by=INTERNAL,
            created_at=PERSONAL_AT,
            notify_storage_mode="legacy",
        )
        await _insert_automation(
            conn,
            automation_id=HEALTH_ID,
            user_id=INTERNAL,
            name="dalton-data-health",
            workspace_id=home,
            scope="personal",
            created_by=INTERNAL,
            created_at=PERSONAL_AT,
            notify_storage_mode="legacy",
        )
        await _insert_automation(
            conn,
            automation_id=SAME_ID,
            user_id=INTERNAL,
            name=ALPHA_NAME,
            workspace_id=ws_a,
            scope="personal",
            created_by=INTERNAL,
            created_at=PERSONAL_AT,
            notify_storage_mode="legacy",
        )
        await _insert_automation(
            conn,
            automation_id=EARNINGS_ID,
            user_id=INTERNAL,
            name="dalton-earnings-calendar",
            workspace_id=ws_b,
            scope="research_tracking",
            created_by=INTERNAL,
            created_at=PERSONAL_AT,
            notify_storage_mode="legacy",
        )
    return Seed(
        comp_a=comp_a,
        comp_b=comp_b,
        ws_company=ws_company,
        keys=keys,
        bbg_title=bbg.title,
        bbg_cron=bbg.cron_expr,
    )


async def _configure_scripts(pool: AsyncConnectionPool) -> None:
    bbg_history = [
        {
            "version": 1,
            "prev_version": None,
            "actor_user_id": "system",
            "changed_at": "2026-11-01T00:00:00Z",
            "action": "seed",
            "changes": {},
            "snapshot": {"enabled": False, "cron_expr": "0 1 * * *"},
        },
        {
            "version": 2,
            "prev_version": 1,
            "actor_user_id": OTHER,
            "changed_at": "2026-11-02T00:00:00Z",
            "action": "update",
            "changes": {"enabled": {"old": False, "new": True}},
            "snapshot": {"enabled": True, "cron_expr": "0 2 * * *"},
        },
        {
            "version": 3,
            "prev_version": 2,
            "actor_user_id": INTERNAL,
            "changed_at": "2026-11-03T00:00:00Z",
            "action": "update",
            "changes": {
                "cron_expr": {"old": "0 2 * * *", "new": "15 8 * * 1-5"},
                "enabled": {"old": True, "new": True},
                "command_argv": {"old": ["echo"], "new": ["echo", PLANTED]},
                "notify_routes": {
                    "old": [{"bot_token": "tok", "route": "feishu:chat:oc_ok"}],
                    "new": ["feishu:chat:oc_script_row"],
                },
            },
            "snapshot": {
                "enabled": True,
                "paused_by_user": False,
                "notify_routes": ["feishu:chat:oc_script_row"],
                "cron_expr": "15 8 * * 1-5",
                "timezone": "Asia/Hong_Kong",
                "notify_enabled": True,
                "cos_mode": "skip",
                "notify_storage_mode": "task_row",
                "command_argv": ["echo", PLANTED],
                "required_secrets": [PLANTED],
            },
        },
    ]
    async with pool.connection() as conn:
        await sync_manifest_to_db(conn)
        await conn.execute(
            """
            UPDATE dalton_data_task_defs
            SET enabled = FALSE,
                paused_by_user = FALSE,
                notify_storage_mode = 'task_row',
                notify_enabled = FALSE,
                notify_routes = '[]'::jsonb,
                migration_state = '{}'::jsonb,
                updated_at = %s
            """,
            (CATALOG_TIE - timedelta(days=1),),
        )
        await conn.execute(
            """
            UPDATE dalton_data_task_defs
            SET title = 'DB BBG Title',
                cron_expr = '15 8 * * 1-5',
                enabled = TRUE,
                paused_by_user = FALSE,
                notify_enabled = TRUE,
                notify_routes = '["feishu:chat:oc_script_row"]'::jsonb,
                notify_storage_mode = 'task_row',
                config_version = 3,
                active_version = 3,
                updated_by = %s,
                audit_history = %s::jsonb,
                updated_at = %s
            WHERE job_key = %s
            """,
            (INTERNAL, json.dumps(bbg_history), CATALOG_TIE + timedelta(hours=1), BBG),
        )
        await conn.execute(
            """
            UPDATE dalton_data_task_defs
            SET enabled = TRUE,
                paused_by_user = FALSE,
                notify_enabled = TRUE,
                notify_storage_mode = 'legacy',
                migration_state = %s::jsonb,
                updated_at = %s
            WHERE job_key = %s
            """,
            (
                json.dumps({"legacy_notify_owner_user_id": INTERNAL}),
                CATALOG_TIE,
                AH_AM,
            ),
        )
        await conn.execute(
            """
            UPDATE dalton_data_task_defs
            SET enabled = TRUE, paused_by_user = FALSE, updated_at = %s
            WHERE job_key = %s
            """,
            (CATALOG_TIE, AH_PM),
        )
        await conn.execute(
            """
            UPDATE dalton_data_task_defs
            SET enabled = TRUE, paused_by_user = TRUE, updated_at = %s
            WHERE job_key = %s
            """,
            (CATALOG_TIE - timedelta(days=1), CN),
        )
        await conn.execute(
            """
            UPDATE dalton_data_task_defs
            SET enabled = FALSE,
                paused_by_user = FALSE,
                audit_history = '[]'::jsonb,
                updated_at = %s
            WHERE job_key = %s
            """,
            (CATALOG_TIE - timedelta(days=1), JP),
        )
        await conn.execute(
            """
            UPDATE dalton_data_task_defs
            SET deleted_at = NOW(), deleted_by = %s
            WHERE job_key = %s
            """,
            (INTERNAL, TSMC),
        )
        await conn.execute(
            """
            INSERT INTO dalton_data_task_defs (
                job_key, task_code, title, batch_no, runner, cron_expr, timezone,
                command_argv, required_secrets, audit_history
            )
            VALUES (
                %s, 'TX', 'Out of manifest', 1, 'container', '0 0 * * *', 'UTC',
                '["echo"]'::jsonb, %s::jsonb, %s::jsonb
            )
            """,
            (
                OUT_KEY,
                json.dumps([PLANTED]),
                json.dumps([{"snapshot": {"command_argv": [PLANTED]}}]),
            ),
        )
        await conn.execute(
            """
            INSERT INTO user_preferences (user_id, model_preference)
            VALUES (%s, %s::jsonb)
            """,
            (
                INTERNAL,
                json.dumps(
                    {
                        "dalton_notify_routes": {
                            _script(AH_AM): ["feishu:chat:oc_legacy_am"]
                        }
                    }
                ),
            ),
        )
        await _insert_run(
            conn,
            run_id=RUN_NEW,
            job_key=BBG,
            status="succeeded",
            created_at=EXEC_NEW,
            slot=EXEC_NEW,
            triggered_by=INTERNAL,
            completed_at=BBG_COMPLETED,
            exit_code=0,
            notify_status="dispatched",
            log_tail=("A" * 20_000) + "ENDMARKER",
        )
        await _insert_run(
            conn,
            run_id=RUN_OLD,
            job_key=BBG,
            status="succeeded",
            created_at=EXEC_OLDER,
            slot=EXEC_OLDER,
            exit_code=0,
            notify_status="skipped",
        )
        await _insert_run(
            conn,
            run_id=RUN_FAIL,
            job_key=AH_AM,
            status="failed",
            created_at=EXEC_TIE,
            slot=EXEC_TIE,
            exit_code=1,
            failure_reason="boom",
            notify_status="failed",
        )
        await _insert_run(
            conn,
            run_id=OUT_RUN,
            job_key=OUT_KEY,
            status="succeeded",
            created_at=EXEC_NEW + timedelta(hours=3),
            slot=EXEC_NEW + timedelta(hours=3),
            log_tail=PLANTED,
        )
        await _insert_execution(
            conn,
            execution_id=AGENT_EXEC_TIE,
            automation_id=AGENT_ID,
            status="completed",
            created_at=EXEC_TIE,
            thread_id=THREAD_ID,
        )
        await _insert_execution(
            conn,
            execution_id=AGENT_EXEC_OLD,
            automation_id=AGENT_ID,
            status="completed",
            created_at=EXEC_OLD,
        )
        await _insert_execution(
            conn,
            execution_id=PERSONAL_EXEC,
            automation_id=ALPHA_ID,
            status="completed",
            created_at=datetime(2026, 11, 10, 15, 0, tzinfo=UTC),
        )
        await _insert_execution(
            conn,
            execution_id=HOME_EXEC,
            automation_id=HEALTH_ID,
            status="completed",
            created_at=datetime(2026, 11, 10, 13, 0, tzinfo=UTC),
        )
        await _insert_execution(
            conn,
            execution_id=RESEARCH_EXEC,
            automation_id=EARNINGS_ID,
            status="completed",
            created_at=datetime(2026, 11, 10, 14, 0, tzinfo=UTC),
        )


def _patch_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        access_tiers,
        "get_dalton_allowed_emails",
        create_autospec(
            access_tiers.get_dalton_allowed_emails,
            return_value=frozenset({"internal@dalton.test", "other@dalton.test"}),
        ),
    )
    monkeypatch.setattr(
        access_tiers,
        "is_invite_pm",
        create_autospec(
            access_tiers.is_invite_pm,
            side_effect=lambda user_id: user_id == ADMIN,
        ),
    )


def _client_app(holder: dict[str, principal_mod.Principal]) -> FastAPI:
    app = FastAPI()
    app.include_router(automations_app.router)
    app.dependency_overrides[principal_mod.get_principal] = lambda: holder["principal"]
    return app


async def _expect(response: Response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    if response.status_code == 204 or not response.content:
        return {}
    return response.json()


@pytest.mark.asyncio
async def test_unified_read_api_real_db(
    au1_live_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = await _seed_identity(au1_live_pool)
    _patch_access(monkeypatch)
    holder = {"principal": _session(INTERNAL)}
    visible_scripts = {_script(key) for key in seed.keys if key != TSMC}
    company_ids = {AGENT_ID, *visible_scripts}
    private_ids = {ALPHA_ID, CATCHUP_ID, HEALTH_ID, SAME_ID, EARNINGS_ID}

    async with AsyncClient(
        transport=ASGITransport(app=_client_app(holder)),
        base_url="http://test",
    ) as client:
        holder["principal"] = _session(EXTERNAL)
        denied = await _expect(await client.get("/api/v1/automations?scope=company"), 403)
        assert denied["detail"] == "Forbidden"

        holder["principal"] = _session(INTERNAL)
        incomplete = await _expect(
            await client.get("/api/v1/automations?scope=company"), 503
        )
        assert "incomplete" in incomplete["detail"]
        assert BBG in incomplete["detail"]
        assert "automations" not in incomplete
        assert AGENT_ID not in incomplete["detail"]

        await _configure_scripts(au1_live_pool)
        listed = await _expect(
            await client.get("/api/v1/automations?scope=company&limit=100"), 200
        )
        assert listed["total"] == len(company_ids)
        assert set(_ids(listed)) == company_ids
        assert private_ids.isdisjoint(_ids(listed))
        assert _script(OUT_KEY) not in _ids(listed)
        assert DELETED_AGENT_ID not in _ids(listed)
        assert PLANTED not in str(listed)
        assert SECRET_KEY not in str(listed)
        bbg = next(item for item in listed["automations"] if item["automation_id"] == _script(BBG))
        assert bbg["name"] == "DB BBG Title"
        assert bbg["name"] != seed.bbg_title
        assert bbg["cron_expression"] == "15 8 * * 1-5"
        assert bbg["cron_expression"] != seed.bbg_cron
        assert bbg["engine_kind"] == "script"
        assert bbg["scope"] == "company"
        assert bbg["description"] is None
        assert bbg["status"] == "active"
        assert "command_argv" not in bbg
        assert "user_id" not in bbg
        assert "created_at" not in bbg

        first = await _expect(
            await client.get("/api/v1/automations?scope=company&limit=2&offset=0"), 200
        )
        second = await _expect(
            await client.get("/api/v1/automations?scope=company&limit=2&offset=2"), 200
        )
        empty = await _expect(
            await client.get("/api/v1/automations?scope=company&limit=2&offset=1000"), 200
        )
        assert _ids(first) == [_script(BBG), AGENT_ID]
        assert _ids(second) == [_script(AH_AM), _script(AH_PM)]
        assert empty["automations"] == []
        assert empty["total"] == first["total"] == second["total"] == len(company_ids)

        active = await _expect(
            await client.get("/api/v1/automations?scope=company&status=active&limit=100"),
            200,
        )
        paused = await _expect(
            await client.get("/api/v1/automations?scope=company&status=paused"), 200
        )
        disabled = await _expect(
            await client.get(
                "/api/v1/automations?scope=company&status=disabled&limit=100"
            ),
            200,
        )
        run_status = await _expect(
            await client.get("/api/v1/automations?scope=company&status=succeeded"), 200
        )
        disabled_scripts = visible_scripts - {_script(BBG), _script(AH_AM), _script(AH_PM), _script(CN)}
        assert set(_ids(active)) == {AGENT_ID, _script(BBG), _script(AH_AM), _script(AH_PM)}
        assert _ids(paused) == [_script(CN)]
        assert set(_ids(disabled)) == disabled_scripts
        assert run_status["total"] == 0

        detail = await _expect(await client.get(f"/api/v1/automations/{_script(BBG)}"), 200)
        assert detail["last_execution"]["status"] == "succeeded"
        assert detail["last_execution"]["automation_execution_id"] == RUN_NEW
        assert detail["last_execution"]["conversation_thread_id"] is None
        assert detail["last_execution"]["triggered_by_user_id"] == INTERNAL
        assert detail["last_execution"]["log_tail"].endswith("ENDMARKER")
        assert len(detail["last_execution"]["log_tail"].encode()) <= LOG_TAIL_MAX_BYTES
        assert _parsed(detail["last_run_at"]) == BBG_COMPLETED
        assert detail["next_run_at"] is not None
        assert detail["dalton_notify_source"] == "task_row"
        assert detail["dalton_notify_effective"] is True
        assert detail["dalton_notify_route_targets"] == ["feishu:chat:oc_script_row"]
        assert "migration_state" not in detail

        legacy = await _expect(await client.get(f"/api/v1/automations/{_script(AH_AM)}"), 200)
        assert legacy["dalton_notify_source"] == "legacy"
        assert legacy["dalton_notify_route_targets"] == ["feishu:chat:oc_legacy_am"]
        assert legacy["next_run_at"] is not None
        paused_detail = await _expect(
            await client.get(f"/api/v1/automations/{_script(CN)}"), 200
        )
        assert paused_detail["status"] == "paused"
        assert paused_detail["paused_by_user"] is True
        assert paused_detail["next_run_at"] is None
        disabled_detail = await _expect(
            await client.get(f"/api/v1/automations/{_script(JP)}"), 200
        )
        assert disabled_detail["status"] == "disabled"
        assert disabled_detail["next_run_at"] is None

        agent = await _expect(
            await client.get(f"/api/v1/automations/{AGENT_ID.upper()}"), 200
        )
        assert agent["engine_kind"] == "agent"
        assert agent["scope"] == "company"
        assert agent["user_id"] == COMPANY_SYSTEM_USER_ID
        assert agent["created_by"] == OTHER
        assert agent["metadata"] == {"note": "visible-note"}
        assert agent["dalton_notify_effective"] is False
        assert agent["dalton_notify_source"] == "task_row"
        assert agent["dalton_notify_route_targets"] == ["feishu:chat:oc_company"]
        assert SECRET_KEY not in str(agent)
        assert HOOK_SECRET not in str(agent)
        assert "pre_company_migration" not in str(agent)

        missing_script = await _expect(
            await client.get(f"/api/v1/automations/{_script(OUT_KEY)}"), 404
        )
        assert missing_script["detail"] == "Automation not found"
        assert PLANTED not in str(missing_script)
        deleted = await _expect(
            await client.get(f"/api/v1/automations/{DELETED_AGENT_ID}"), 404
        )
        assert deleted["detail"] == "Automation not found"
        malformed = await _expect(await client.get("/api/v1/automations/data:"), 422)
        assert malformed["detail"] == "Invalid automation id"

        feed = await _expect(
            await client.get("/api/v1/automations/executions?scope=company&limit=10"),
            200,
        )
        feed_ids = [item["automation_execution_id"] for item in feed["executions"]]
        assert feed_ids == [RUN_NEW, AGENT_EXEC_TIE, RUN_FAIL, AGENT_EXEC_OLD, RUN_OLD]
        assert feed["has_more"] is False
        assert PERSONAL_EXEC not in feed_ids
        assert OUT_RUN not in feed_ids
        assert PLANTED not in str(feed)
        newest = feed["executions"][0]
        assert newest["status"] == "succeeded"
        assert newest["engine_kind"] == "script"
        assert newest["conversation_thread_id"] is None
        assert newest["log_tail"].endswith("ENDMARKER")
        tied = feed["executions"][1]
        assert tied["engine_kind"] == "agent"
        assert tied["automation_execution_id"] == AGENT_EXEC_TIE
        assert tied["conversation_thread_id"] == THREAD_ID

        one = await _expect(
            await client.get("/api/v1/automations/executions?scope=company&limit=1"),
            200,
        )
        assert one["has_more"] is True
        assert one["executions"][0]["automation_execution_id"] == RUN_NEW
        past = await _expect(
            await client.get(
                "/api/v1/automations/executions?scope=company&limit=2&offset=100"
            ),
            200,
        )
        assert past == {"executions": [], "has_more": False}
        succeeded = await _expect(
            await client.get(
                "/api/v1/automations/executions?scope=company&status=succeeded&limit=10"
            ),
            200,
        )
        completed = await _expect(
            await client.get(
                "/api/v1/automations/executions?scope=company&status=completed&limit=10"
            ),
            200,
        )
        failed = await _expect(
            await client.get("/api/v1/automations/executions?scope=company&status=failed"),
            200,
        )
        threaded = await _expect(
            await client.get(
                f"/api/v1/automations/executions?scope=company&thread_id={THREAD_ID}"
            ),
            200,
        )
        assert [item["automation_execution_id"] for item in succeeded["executions"]] == [
            RUN_NEW,
            RUN_OLD,
        ]
        assert [item["engine_kind"] for item in succeeded["executions"]] == ["script", "script"]
        assert [item["automation_execution_id"] for item in completed["executions"]] == [
            AGENT_EXEC_TIE,
            AGENT_EXEC_OLD,
        ]
        assert [item["automation_execution_id"] for item in failed["executions"]] == [RUN_FAIL]
        assert [item["automation_execution_id"] for item in threaded["executions"]] == [
            AGENT_EXEC_TIE
        ]

        script_page = await _expect(
            await client.get(f"/api/v1/automations/{_script(BBG)}/executions?limit=1"),
            200,
        )
        script_rest = await _expect(
            await client.get(
                f"/api/v1/automations/{_script(BBG)}/executions?limit=1&offset=1"
            ),
            200,
        )
        assert script_page["has_more"] is True
        assert script_page["executions"][0]["automation_execution_id"] == RUN_NEW
        assert script_page["executions"][0]["conversation_thread_id"] is None
        assert script_rest["executions"][0]["automation_execution_id"] == RUN_OLD
        agent_runs = await _expect(
            await client.get(f"/api/v1/automations/{AGENT_ID}/executions"), 200
        )
        assert [item["automation_execution_id"] for item in agent_runs["executions"]] == [
            AGENT_EXEC_TIE,
            AGENT_EXEC_OLD,
        ]
        deleted_runs = await _expect(
            await client.get(f"/api/v1/automations/{DELETED_AGENT_ID}/executions"), 404
        )
        assert deleted_runs["detail"] == "Automation not found"

        before_bbg = await _history_bytes(
            au1_live_pool,
            table="dalton_data_task_defs",
            key_column="job_key",
            key=BBG,
        )
        before_jp = await _history_bytes(
            au1_live_pool,
            table="dalton_data_task_defs",
            key_column="job_key",
            key=JP,
        )
        before_agent = await _history_bytes(
            au1_live_pool,
            table="automations",
            key_column="automation_id",
            key=AGENT_ID,
            cast_uuid=True,
        )
        history = await _expect(
            await client.get(f"/api/v1/automations/{_script(BBG)}/history?limit=1"),
            200,
        )
        assert history["automation_id"] == _script(BBG)
        assert history["engine_kind"] == "script"
        assert history["scope"] == "company"
        assert history["total"] == 3
        assert [entry["version"] for entry in history["entries"]] == [3]
        page_two = await _expect(
            await client.get(f"/api/v1/automations/{_script(BBG)}/history?limit=1&offset=1"),
            200,
        )
        page_three = await _expect(
            await client.get(f"/api/v1/automations/{_script(BBG)}/history?limit=1&offset=2"),
            200,
        )
        assert [entry["version"] for entry in page_two["entries"]] == [2]
        assert [entry["version"] for entry in page_three["entries"]] == [1]
        current = history["entries"][0]
        assert current["snapshot"]["cron_expr"] == "15 8 * * 1-5"
        assert current["snapshot"]["enabled"] is True
        assert set(current["snapshot"]) <= ALLOWED_SCRIPT_SNAPSHOT_FIELD_SET
        assert "command_argv" not in current["snapshot"]
        assert "command_argv" not in current["changes"]
        assert "bot_token" not in str(current)
        assert PLANTED not in str(history)
        empty_history = await _expect(
            await client.get(f"/api/v1/automations/{_script(JP)}/history"), 200
        )
        assert empty_history["entries"] == []
        assert empty_history["total"] == 0
        agent_history = await _expect(
            await client.get(f"/api/v1/automations/{AGENT_ID}/history"), 200
        )
        assert agent_history["engine_kind"] == "agent"
        assert "api_key" not in str(agent_history)
        assert agent_history["entries"][0]["snapshot"]["cron_expression"] == "0 9 * * *"
        for field_name in agent_history["entries"][0]["snapshot"]:
            assert field_name in company_auto_svc.ALLOWED_AUTOMATION_SNAPSHOT_FIELD_SET
        assert await _history_bytes(
            au1_live_pool, table="dalton_data_task_defs", key_column="job_key", key=BBG
        ) == before_bbg
        assert await _history_bytes(
            au1_live_pool, table="dalton_data_task_defs", key_column="job_key", key=JP
        ) == before_jp
        assert await _history_bytes(
            au1_live_pool,
            table="automations",
            key_column="automation_id",
            key=AGENT_ID,
            cast_uuid=True,
        ) == before_agent

        holder["principal"] = _session(ADMIN)
        assert (await client.get(f"/api/v1/automations/{_script(BBG)}")).status_code == 200
        holder["principal"] = _session(OTHER)
        shared = await _expect(await client.get(f"/api/v1/automations/{AGENT_ID}"), 200)
        assert shared["created_by"] == OTHER
        hidden = await _expect(await client.get(f"/api/v1/automations/{ALPHA_ID}"), 404)
        assert hidden["detail"] == "Automation not found"
        forbidden_history = await _expect(
            await client.get(f"/api/v1/automations/{ALPHA_ID}/history"), 403
        )
        assert forbidden_history["detail"] == "Forbidden"
        foreign_runs = await _expect(
            await client.get(f"/api/v1/automations/{ALPHA_ID}/executions"), 200
        )
        assert foreign_runs == {"executions": [], "has_more": False}

        company_paths = [
            "/api/v1/automations?scope=company",
            f"/api/v1/automations/{AGENT_ID}",
            f"/api/v1/automations/{_script(BBG)}",
            f"/api/v1/automations/{_script(OUT_KEY)}",
            f"/api/v1/automations/{DELETED_AGENT_ID}",
            "/api/v1/automations/executions?scope=company",
            f"/api/v1/automations/executions?scope=company&thread_id={THREAD_ID}",
            f"/api/v1/automations/{AGENT_ID}/executions",
            f"/api/v1/automations/{_script(BBG)}/executions",
            f"/api/v1/automations/{AGENT_ID}/history",
            f"/api/v1/automations/{_script(BBG)}/history",
        ]
        for principal in (_session(EXTERNAL), _computer(seed.comp_a)):
            holder["principal"] = principal
            for path in company_paths:
                response = await client.get(path)
                assert response.status_code == 403, (path, response.text)
                assert response.json()["detail"] == "Forbidden"
                assert PLANTED not in response.text
            for path in ("/api/v1/automations/not-a-uuid", "/api/v1/automations/data:"):
                response = await client.get(path)
                assert response.status_code == 422, (path, response.text)
                assert response.json()["detail"] == "Invalid automation id"

        holder["principal"] = _session(INTERNAL)
        own = await _expect(await client.get("/api/v1/automations?limit=100"), 200)
        assert set(_ids(own)) == private_ids
        assert company_ids.isdisjoint(_ids(own))
        personal = await _expect(
            await client.get("/api/v1/automations?scope=personal&limit=2&offset=0"), 200
        )
        personal_next = await _expect(
            await client.get("/api/v1/automations?scope=personal&limit=2&offset=2"), 200
        )
        assert _ids(personal) == [SAME_ID, HEALTH_ID]
        assert _ids(personal_next) == [CATCHUP_ID, ALPHA_ID]
        research = await _expect(
            await client.get("/api/v1/automations?scope=research_tracking"), 200
        )
        assert _ids(research) == [EARNINGS_ID]
        assert research["automations"][0]["scope"] == "research_tracking"
        assert research["automations"][0]["name"] == "dalton-earnings-calendar"
        own_feed = await _expect(await client.get("/api/v1/automations/executions"), 200)
        own_exec_ids = [item["automation_execution_id"] for item in own_feed["executions"]]
        assert PERSONAL_EXEC in own_exec_ids
        assert HOME_EXEC in own_exec_ids
        assert RESEARCH_EXEC in own_exec_ids
        assert RUN_NEW not in own_exec_ids
        assert AGENT_EXEC_TIE not in own_exec_ids

        holder["principal"] = _computer(seed.comp_a)
        computer_list = await _expect(
            await client.get("/api/v1/automations?limit=100"), 200
        )
        assert _ids(computer_list) == [SAME_ID, CATCHUP_ID, ALPHA_ID]
        computer_feed = await _expect(
            await client.get("/api/v1/automations/executions"), 200
        )
        computer_exec_ids = [
            item["automation_execution_id"] for item in computer_feed["executions"]
        ]
        assert computer_exec_ids == [PERSONAL_EXEC]
        research_feed = await _expect(
            await client.get("/api/v1/automations/executions?scope=research_tracking"),
            200,
        )
        assert research_feed == {"executions": [], "has_more": False}

        holder["principal"] = _session(INTERNAL)
        for method, path in (
            ("patch", f"/api/v1/automations/{_script(BBG)}"),
            ("post", f"/api/v1/automations/{_script(BBG)}/trigger"),
            ("delete", f"/api/v1/automations/{_script(BBG)}"),
        ):
            response = await client.request(method.upper(), path, json={"name": "nope"})
            assert response.status_code == 422, (method, path, response.text)
        invalid_write = await client.patch(
            f"/api/v1/automations/{ALPHA_ID}",
            json={"trigger_type": "weekly"},
        )
        assert invalid_write.status_code == 422, invalid_write.text
        static_feed = await _expect(await client.get("/api/v1/automations/executions"), 200)
        assert "executions" in static_feed
        async with au1_live_pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT description, config_version FROM automations WHERE automation_id = %s::uuid",
                    (ALPHA_ID,),
                )
                alpha_row = await cur.fetchone()
                await cur.execute(
                    "SELECT config_version FROM dalton_data_task_defs WHERE job_key = %s",
                    (BBG,),
                )
                bbg_row = await cur.fetchone()
        assert alpha_row["description"] == "alpha-personal"
        assert alpha_row["config_version"] == 1
        assert bbg_row["config_version"] == 3

        async with au1_live_pool.connection() as conn:
            async with _frozen_updated_at(conn):
                await conn.execute(
                    """
                    UPDATE automations
                    SET user_id = %s,
                        scope = 'company',
                        notify_storage_mode = 'task_row',
                        workspace_id = %s::uuid
                    WHERE automation_id = %s::uuid
                    """,
                    (COMPANY_SYSTEM_USER_ID, seed.ws_company, ALPHA_ID),
                )
        migrated = await _expect(
            await client.get("/api/v1/automations?scope=company&limit=100"), 200
        )
        assert ALPHA_ID in _ids(migrated)
        assert CATCHUP_ID not in _ids(migrated)
        assert HEALTH_ID not in _ids(migrated)
        assert SAME_ID not in _ids(migrated)
        assert migrated["total"] == len(company_ids) + 1
        owner_list = await _expect(await client.get("/api/v1/automations?limit=100"), 200)
        assert HEALTH_ID in _ids(owner_list)
        assert EARNINGS_ID in _ids(owner_list)
        assert ALPHA_ID not in _ids(owner_list)
        assert CATCHUP_ID in _ids(owner_list)
        assert SAME_ID in _ids(owner_list)

        async with au1_live_pool.connection() as conn:
            await conn.execute(
                "DELETE FROM dalton_data_task_runs WHERE job_key = %s",
                (BBG,),
            )
            await conn.execute(
                "DELETE FROM dalton_data_task_defs WHERE job_key = %s",
                (BBG,),
            )
        gap = await _expect(await client.get("/api/v1/automations?scope=company"), 503)
        assert BBG in gap["detail"]
        assert "DB BBG Title" not in str(gap)
        gap_feed = await _expect(
            await client.get("/api/v1/automations/executions?scope=company"), 503
        )
        assert BBG in gap_feed["detail"]
        gap_detail = await _expect(
            await client.get(f"/api/v1/automations/{_script(BBG)}"), 503
        )
        assert gap_detail["detail"] == f"Company data task definition is missing: {BBG}"
        still_there = await _expect(
            await client.get(f"/api/v1/automations/{_script(CN)}"), 200
        )
        assert still_there["automation_id"] == _script(CN)
        assert still_there["status"] == "paused"


def _run_ids(body: dict[str, Any]) -> list[str]:
    return [item["automation_execution_id"] for item in body["executions"]]


def _paged(url: str, *, limit: int, offset: int) -> str:
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}limit={limit}&offset={offset}"


async def _assert_execution_pages(
    client: AsyncClient,
    url: str,
    ids: list[str],
    banned: tuple[str, ...],
) -> None:
    """Pages are taken after the computer filter, so a forbidden newer row cannot occupy a slot."""
    full = await _expect(await client.get(_paged(url, limit=20, offset=0)), 200)
    assert _run_ids(full) == ids
    assert full["has_more"] is False
    rendered = str(full)
    for secret in banned:
        assert secret not in rendered
    for index, execution_id in enumerate(ids):
        page = await _expect(await client.get(_paged(url, limit=1, offset=index)), 200)
        assert _run_ids(page) == [execution_id]
        assert page["has_more"] is (index < len(ids) - 1)
        rendered = str(page)
        for secret in banned:
            assert secret not in rendered
    empty = await _expect(await client.get(_paged(url, limit=1, offset=len(ids))), 200)
    assert empty == {"executions": [], "has_more": False}


async def _execution_rows(pool: AsyncConnectionPool) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT automation_execution_id::text AS automation_execution_id,
                       automation_id::text AS automation_id,
                       conversation_thread_id::text AS conversation_thread_id,
                       result_excerpt, error_message, delivery_result, status, created_at
                FROM automation_executions
                ORDER BY created_at DESC, automation_execution_id DESC
                """
            )
            return [dict(row) for row in await cur.fetchall()]


async def _thread_places(pool: AsyncConnectionPool) -> list[tuple[str, str]]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT conversation_thread_id::text, workspace_id::text
                FROM conversation_threads
                ORDER BY conversation_thread_id
                """
            )
            return [(row[0], row[1]) for row in await cur.fetchall()]


def _tasks(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["automation_id"]: item for item in body["automations"]}


def _assert_last(
    task: dict[str, Any],
    *,
    execution_id: str | None,
    excerpt: str | None = None,
    error_message: str | None = None,
    thread_id: str | None = None,
    status: str | None = None,
    delivery_note: str | None = None,
) -> None:
    last = task["last_execution"]
    if execution_id is None:
        assert last is None
        return
    assert last["automation_execution_id"] == execution_id
    assert last["excerpt"] == excerpt
    assert last["error_message"] == error_message
    assert last["conversation_thread_id"] == thread_id
    assert last["status"] == status
    assert last["delivery_result"] == [{"note": delivery_note}]


def _assert_secrets_absent(payload: object, secrets: tuple[str, ...]) -> None:
    rendered = str(payload)
    for secret in secrets:
        assert secret not in rendered


async def _automation_version(
    pool: AsyncConnectionPool,
    automation_id: str,
) -> dict[str, Any]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT workspace_id::text AS workspace_id, scope,
                       config_version, active_version
                FROM automations
                WHERE automation_id = %s::uuid
                """,
                (automation_id,),
            )
            row = await cur.fetchone()
    assert row is not None
    return dict(row)


@pytest.mark.asyncio
async def test_moved_task_execution_reads_stay_on_the_token_computer(
    au1_live_pool: AsyncConnectionPool,
) -> None:
    """A computer token does not read runs left on another computer after a move."""
    owner = "u-move-owner"
    comp_a = "a1111111-1111-4111-8111-111111111111"
    comp_b = "b2222222-2222-4222-8222-222222222222"
    ws_a = "a3333333-3333-4333-8333-333333333333"
    ws_b = "b4444444-4444-4444-8444-444444444444"
    personal_id = "c5555555-5555-4555-8555-555555555555"
    research_id = "d6666666-6666-4666-8666-666666666666"
    foreign_only_id = "a9999999-9999-4999-8999-999999999999"
    other_id = "ab000000-0000-4000-8000-0000000000c2"
    company_id = "ab000000-0000-4000-8000-0000000000c1"
    other_user = "u-move-other"
    thread_a = "e7777777-7777-4777-8777-777777777771"
    thread_home = "e7777777-7777-4777-8777-777777777772"
    thread_a_mid = "e7777777-7777-4777-8777-777777777773"
    thread_b_first = "e7777777-7777-4777-8777-777777777774"
    thread_b_second = "e7777777-7777-4777-8777-777777777775"
    thread_research_a = "e7777777-7777-4777-8777-777777777776"
    thread_research_b = "e7777777-7777-4777-8777-777777777777"
    run_research_a = "f1000000-0000-4000-8000-000000000001"
    run_a_top = "f1000000-0000-4000-8000-000000000002"
    run_research_b = "f1000000-0000-4000-8000-000000000003"
    run_home = "f1000000-0000-4000-8000-000000000004"
    run_research_none = "f1000000-0000-4000-8000-000000000005"
    run_b_first = "f1000000-0000-4000-8000-000000000006"
    run_a_mid = "f1000000-0000-4000-8000-000000000007"
    run_b_second = "f1000000-0000-4000-8000-000000000008"
    run_none = "f1000000-0000-4000-8000-000000000009"
    run_research_inflight = "f1000000-0000-4000-8000-000000000011"
    run_personal_inflight = "f1000000-0000-4000-8000-000000000012"
    run_foreign_done = "f1000000-0000-4000-8000-000000000013"
    run_foreign_live = "f1000000-0000-4000-8000-000000000014"
    old_a = "OLD-A-EXCERPT"
    old_a_error = "OLD-A-ERROR"
    old_a_delivery = "OLD-A-DELIVERY"
    old_home = "OLD-HOME-EXCERPT"
    old_home_error = "OLD-HOME-ERROR"
    old_home_delivery = "OLD-HOME-DELIVERY"
    old_mid = "OLD-A-MID-EXCERPT"
    old_mid_error = "OLD-A-MID-ERROR"
    old_mid_delivery = "OLD-A-MID-DELIVERY"
    old_research = "OLD-A-RESEARCH-EXCERPT"
    old_research_error = "OLD-A-RESEARCH-ERROR"
    old_research_delivery = "OLD-A-RESEARCH-DELIVERY"
    old_research_live = "OLD-RESEARCH-INFLIGHT"
    old_research_live_error = "OLD-RESEARCH-INFLIGHT-ERROR"
    old_research_live_delivery = "OLD-RESEARCH-INFLIGHT-DELIVERY"
    old_personal_live = "OLD-A-INFLIGHT"
    old_personal_live_error = "OLD-A-INFLIGHT-ERROR"
    old_personal_live_delivery = "OLD-A-INFLIGHT-DELIVERY"
    foreign_done = "FOREIGN-ONLY-DONE"
    foreign_done_error = "FOREIGN-ONLY-DONE-ERROR"
    foreign_done_delivery = "FOREIGN-ONLY-DONE-DELIVERY"
    foreign_live = "FOREIGN-ONLY-LIVE"
    foreign_live_error = "FOREIGN-ONLY-LIVE-ERROR"
    foreign_live_delivery = "FOREIGN-ONLY-LIVE-DELIVERY"
    other_excerpt = "OTHER-OWNER-EXCERPT"
    other_error = "OTHER-OWNER-ERROR"
    other_delivery = "OTHER-OWNER-DELIVERY"
    company_excerpt = "COMPANY-TASK-EXCERPT"
    company_error = "COMPANY-TASK-ERROR"
    company_delivery = "COMPANY-TASK-DELIVERY"
    b_first_delivery = "B-FIRST-DELIVERY"
    b_research_delivery = "B-RESEARCH-NOTHREAD-DELIVERY"
    banned = (
        old_a,
        old_a_error,
        old_a_delivery,
        old_home,
        old_home_error,
        old_home_delivery,
        old_mid,
        old_mid_error,
        old_mid_delivery,
        old_research,
        old_research_error,
        old_research_delivery,
        old_research_live,
        old_research_live_error,
        old_research_live_delivery,
        old_personal_live,
        old_personal_live_error,
        old_personal_live_delivery,
        foreign_done,
        foreign_done_error,
        foreign_done_delivery,
        foreign_live,
        foreign_live_error,
        foreign_live_delivery,
        other_excerpt,
        other_error,
        other_delivery,
        company_excerpt,
        company_error,
        company_delivery,
        thread_a,
        thread_home,
        thread_a_mid,
        thread_research_a,
    )
    day = datetime(2026, 11, 10, tzinfo=UTC)
    home = get_home_workspace_id(owner)
    feed = "/api/v1/automations/executions"
    personal_feed = f"{feed}?scope=personal"
    research_feed = f"{feed}?scope=research_tracking"
    personal_history = f"/api/v1/automations/{personal_id}/executions"
    research_history = f"/api/v1/automations/{research_id}/executions"
    personal_detail = f"/api/v1/automations/{personal_id}"
    research_detail = f"/api/v1/automations/{research_id}"
    foreign_detail = f"/api/v1/automations/{foreign_only_id}"
    task_list = "/api/v1/automations?limit=100"
    personal_list = "/api/v1/automations?scope=personal&limit=100"
    research_list = "/api/v1/automations?scope=research_tracking&limit=100"
    visible = [
        run_research_b,
        run_research_none,
        run_b_first,
        run_b_second,
        run_none,
    ]
    personal_visible = [run_b_first, run_b_second, run_none]
    research_visible = [run_research_b, run_research_none]
    owner_task_ids = [research_id, personal_id, foreign_only_id]
    session_order = [
        run_research_a,
        run_research_inflight,
        run_a_top,
        run_research_b,
        run_home,
        run_research_none,
        run_personal_inflight,
        run_b_first,
        run_a_mid,
        run_b_second,
        run_none,
        run_foreign_done,
        run_foreign_live,
    ]

    async with au1_live_pool.connection() as conn:
        await _insert_user(conn, owner, "move@dalton.test")
        await _insert_computer(conn, comp_a, owner, "Move A")
        await _insert_computer(conn, comp_b, owner, "Move B")
        await _insert_workspace(
            conn, home, owner, "Home", status="running", computer_id=comp_b
        )
        await _insert_workspace(
            conn, ws_a, owner, "Move WS A", status="running", computer_id=comp_a
        )
        await _insert_workspace(
            conn, ws_b, owner, "Move WS B", status="running", computer_id=comp_b
        )
        await _insert_automation(
            conn,
            automation_id=personal_id,
            user_id=owner,
            name="Moved Personal Task",
            workspace_id=ws_a,
            scope="personal",
            created_by=owner,
            created_at=day,
            notify_storage_mode="legacy",
        )
        await _insert_automation(
            conn,
            automation_id=research_id,
            user_id=owner,
            name="dalton-earnings-calendar",
            workspace_id=ws_a,
            scope="research_tracking",
            created_by=owner,
            created_at=day,
            notify_storage_mode="legacy",
            description="research-before-move",
        )
        await _insert_automation(
            conn,
            automation_id=foreign_only_id,
            user_id=owner,
            name="Foreign Only Task",
            workspace_id=ws_a,
            scope="personal",
            created_by=owner,
            created_at=day,
            notify_storage_mode="legacy",
        )
        await _insert_user(conn, other_user, "other-move@dalton.test")
        await _insert_automation(
            conn,
            automation_id=other_id,
            user_id=other_user,
            name="Unrelated Owner Task",
            workspace_id=ws_b,
            scope="personal",
            created_by=other_user,
            created_at=day,
            notify_storage_mode="legacy",
        )
        await _insert_automation(
            conn,
            automation_id=company_id,
            user_id=COMPANY_SYSTEM_USER_ID,
            name="Unrelated Company Task",
            workspace_id=ws_b,
            scope="company",
            created_by=COMPANY_SYSTEM_USER_ID,
            created_at=day,
            notify_storage_mode="task_row",
        )
        await _insert_thread(conn, thread_id=thread_a, workspace_id=ws_a, thread_index=1)
        await _insert_thread(
            conn, thread_id=thread_a_mid, workspace_id=ws_a, thread_index=2
        )
        await _insert_thread(
            conn, thread_id=thread_research_a, workspace_id=ws_a, thread_index=3
        )
        await _insert_thread(
            conn, thread_id=thread_home, workspace_id=home, thread_index=1
        )
        await _insert_execution(
            conn,
            execution_id=run_research_a,
            automation_id=research_id,
            status="completed",
            created_at=day.replace(hour=19),
            thread_id=thread_research_a,
            excerpt=old_research,
            error_message=old_research_error,
            delivery_result=[{"note": old_research_delivery}],
        )
        await _insert_execution(
            conn,
            execution_id=run_a_top,
            automation_id=personal_id,
            status="completed",
            created_at=day.replace(hour=18, minute=30),
            thread_id=thread_a,
            excerpt=old_a,
            error_message=old_a_error,
            delivery_result=[{"note": old_a_delivery}],
        )
        await _insert_execution(
            conn,
            execution_id=run_home,
            automation_id=personal_id,
            status="failed",
            created_at=day.replace(hour=17, minute=30),
            thread_id=thread_home,
            excerpt=old_home,
            error_message=old_home_error,
            delivery_result=[{"note": old_home_delivery}],
        )
        await _insert_execution(
            conn,
            execution_id=run_a_mid,
            automation_id=personal_id,
            status="completed",
            created_at=day.replace(hour=15),
            thread_id=thread_a_mid,
            excerpt=old_mid,
            error_message=old_mid_error,
            delivery_result=[{"note": old_mid_delivery}],
        )
    before_move = await _execution_rows(au1_live_pool)
    places_before = await _thread_places(au1_live_pool)

    holder = {"principal": _session(owner)}
    async with AsyncClient(
        transport=ASGITransport(app=_client_app(holder)),
        base_url="http://test",
    ) as client:
        for automation_id, scope in (
            (personal_id, "personal"),
            (research_id, "research_tracking"),
            (foreign_only_id, "personal"),
        ):
            moved = await _expect(
                await client.patch(
                    f"/api/v1/automations/{automation_id}",
                    json={"workspace_id": ws_b},
                ),
                200,
            )
            assert moved["workspace_id"] == ws_b
            assert moved["scope"] == scope
        assert await _execution_rows(au1_live_pool) == before_move
        assert await _thread_places(au1_live_pool) == places_before

        async with au1_live_pool.connection() as conn:
            await _insert_thread(
                conn, thread_id=thread_b_first, workspace_id=ws_b, thread_index=1
            )
            await _insert_thread(
                conn, thread_id=thread_b_second, workspace_id=ws_b, thread_index=2
            )
            await _insert_thread(
                conn, thread_id=thread_research_b, workspace_id=ws_b, thread_index=3
            )
            await _insert_execution(
                conn,
                execution_id=run_research_inflight,
                automation_id=research_id,
                status="running",
                created_at=day.replace(hour=18, minute=45),
                thread_id=thread_research_a,
                excerpt=old_research_live,
                error_message=old_research_live_error,
                delivery_result=[{"note": old_research_live_delivery}],
            )
            await _insert_execution(
                conn,
                execution_id=run_research_b,
                automation_id=research_id,
                status="completed",
                created_at=day.replace(hour=18),
                thread_id=thread_research_b,
                excerpt="B-RESEARCH-EXCERPT",
            )
            await _insert_execution(
                conn,
                execution_id=run_research_none,
                automation_id=research_id,
                status="running",
                created_at=day.replace(hour=17),
                excerpt="B-RESEARCH-NOTHREAD",
                delivery_result=[{"note": b_research_delivery}],
            )
            await _insert_execution(
                conn,
                execution_id=run_personal_inflight,
                automation_id=personal_id,
                status="running",
                created_at=day.replace(hour=16, minute=30),
                thread_id=thread_a,
                excerpt=old_personal_live,
                error_message=old_personal_live_error,
                delivery_result=[{"note": old_personal_live_delivery}],
            )
            await _insert_execution(
                conn,
                execution_id=run_b_first,
                automation_id=personal_id,
                status="completed",
                created_at=day.replace(hour=16),
                thread_id=thread_b_first,
                excerpt="B-FIRST-EXCERPT",
                delivery_result=[{"note": b_first_delivery}],
            )
            await _insert_execution(
                conn,
                execution_id=run_b_second,
                automation_id=personal_id,
                status="completed",
                created_at=day.replace(hour=14),
                thread_id=thread_b_second,
                excerpt="B-SECOND-EXCERPT",
            )
            await _insert_execution(
                conn,
                execution_id=run_none,
                automation_id=personal_id,
                status="completed",
                created_at=day.replace(hour=13),
                excerpt="B-NOTHREAD-EXCERPT",
            )
            await _insert_execution(
                conn,
                execution_id=run_foreign_done,
                automation_id=foreign_only_id,
                status="completed",
                created_at=day.replace(hour=12),
                thread_id=thread_a,
                excerpt=foreign_done,
                error_message=foreign_done_error,
                delivery_result=[{"note": foreign_done_delivery}],
            )
            await _insert_execution(
                conn,
                execution_id=run_foreign_live,
                automation_id=foreign_only_id,
                status="running",
                created_at=day.replace(hour=11),
                thread_id=thread_a,
                excerpt=foreign_live,
                error_message=foreign_live_error,
                delivery_result=[{"note": foreign_live_delivery}],
            )
            await _insert_execution(
                conn,
                execution_id="f1000000-0000-4000-8000-000000000015",
                automation_id=other_id,
                status="running",
                created_at=day.replace(hour=9),
                thread_id=None,
                excerpt=other_excerpt,
                error_message=other_error,
                delivery_result=[{"note": other_delivery}],
            )
            await _insert_execution(
                conn,
                execution_id="f1000000-0000-4000-8000-000000000016",
                automation_id=company_id,
                status="completed",
                created_at=day.replace(hour=8),
                thread_id=None,
                excerpt=company_excerpt,
                error_message=company_error,
                delivery_result=[{"note": company_delivery}],
            )
        stored_runs = await _execution_rows(au1_live_pool)
        stored_threads = await _thread_places(au1_live_pool)
        personal_version = await _automation_version(au1_live_pool, personal_id)
        research_version = await _automation_version(au1_live_pool, research_id)
        foreign_version = await _automation_version(au1_live_pool, foreign_only_id)
        assert personal_version["workspace_id"] == ws_b
        assert research_version["workspace_id"] == ws_b
        assert foreign_version["workspace_id"] == ws_b

        holder["principal"] = _computer(comp_b, owner)
        await _assert_execution_pages(client, feed, visible, banned)
        await _assert_execution_pages(client, personal_feed, personal_visible, banned)
        await _assert_execution_pages(client, research_feed, research_visible, banned)
        await _assert_execution_pages(
            client, personal_history, personal_visible, banned
        )
        await _assert_execution_pages(
            client, research_history, research_visible, banned
        )
        opened = await _expect(await client.get(_paged(feed, limit=20, offset=0)), 200)
        by_id = {item["automation_execution_id"]: item for item in opened["executions"]}
        assert by_id[run_none]["conversation_thread_id"] is None
        assert by_id[run_none]["workspace_id"] == ws_b
        assert by_id[run_none]["excerpt"] == "B-NOTHREAD-EXCERPT"
        assert by_id[run_b_first]["conversation_thread_id"] == thread_b_first
        assert by_id[run_b_first]["workspace_id"] == ws_b
        assert by_id[run_research_none]["conversation_thread_id"] is None

        off_computer = await client.get(f"{feed}?thread_id={thread_a}")
        assert off_computer.status_code == 403, off_computer.text
        assert off_computer.json()["detail"]["message"] == (
            "A computer's token reaches only what that computer holds"
        )
        home_thread = await client.get(f"{feed}?thread_id={thread_home}")
        assert home_thread.status_code == 403, home_thread.text
        assert home_thread.json()["detail"]["message"] == (
            "A computer's token never reaches Home"
        )
        for secret in banned:
            assert secret not in off_computer.text
            assert secret not in home_thread.text

        b_default = await _expect(await client.get(task_list), 200)
        assert _ids(b_default) == owner_task_ids
        assert b_default["total"] == len(owner_task_ids)
        _assert_secrets_absent(b_default, banned)
        b_tasks = _tasks(b_default)
        _assert_last(
            b_tasks[personal_id],
            execution_id=run_b_first,
            excerpt="B-FIRST-EXCERPT",
            thread_id=thread_b_first,
            status="completed",
            delivery_note=b_first_delivery,
        )
        _assert_last(
            b_tasks[research_id],
            execution_id=run_research_none,
            excerpt="B-RESEARCH-NOTHREAD",
            status="running",
            delivery_note=b_research_delivery,
        )
        _assert_last(b_tasks[foreign_only_id], execution_id=None)
        b_personal = await _expect(await client.get(personal_list), 200)
        assert _ids(b_personal) == [personal_id, foreign_only_id]
        _assert_secrets_absent(b_personal, banned)
        _assert_last(
            _tasks(b_personal)[personal_id],
            execution_id=run_b_first,
            excerpt="B-FIRST-EXCERPT",
            thread_id=thread_b_first,
            status="completed",
            delivery_note=b_first_delivery,
        )
        _assert_last(_tasks(b_personal)[foreign_only_id], execution_id=None)
        b_research = await _expect(await client.get(research_list), 200)
        assert _ids(b_research) == [research_id]
        _assert_secrets_absent(b_research, banned)
        _assert_last(
            _tasks(b_research)[research_id],
            execution_id=run_research_none,
            excerpt="B-RESEARCH-NOTHREAD",
            status="running",
            delivery_note=b_research_delivery,
        )
        personal_body = await _expect(await client.get(personal_detail), 200)
        research_body = await _expect(await client.get(research_detail), 200)
        foreign_body = await _expect(await client.get(foreign_detail), 200)
        _assert_secrets_absent(personal_body, banned)
        _assert_secrets_absent(research_body, banned)
        _assert_secrets_absent(foreign_body, banned)
        _assert_last(
            personal_body,
            execution_id=run_b_first,
            excerpt="B-FIRST-EXCERPT",
            thread_id=thread_b_first,
            status="completed",
            delivery_note=b_first_delivery,
        )
        _assert_last(
            research_body,
            execution_id=run_research_none,
            excerpt="B-RESEARCH-NOTHREAD",
            status="running",
            delivery_note=b_research_delivery,
        )
        _assert_last(foreign_body, execution_id=None)
        await _assert_execution_pages(
            client,
            f"/api/v1/automations/{foreign_only_id}/executions",
            [],
            banned,
        )
        unrelated = await client.get(f"/api/v1/automations/{other_id}")
        assert unrelated.status_code == 404, unrelated.text
        _assert_secrets_absent(unrelated.text, banned)
        company_hidden = await client.get(f"/api/v1/automations/{company_id}")
        assert company_hidden.status_code == 403, company_hidden.text
        assert company_hidden.json()["detail"] == "Forbidden"
        _assert_secrets_absent(company_hidden.text, banned)

        holder["principal"] = _computer(comp_a, owner)
        for url in (feed, personal_feed, research_feed):
            hidden = await _expect(await client.get(_paged(url, limit=20, offset=0)), 200)
            assert hidden == {"executions": [], "has_more": False}
        for url in (task_list, personal_list, research_list):
            hidden_tasks = await _expect(await client.get(url), 200)
            assert hidden_tasks["automations"] == []
            assert hidden_tasks["total"] == 0
            _assert_secrets_absent(hidden_tasks, banned)
        for url in (
            personal_history,
            research_history,
            personal_detail,
            research_detail,
            foreign_detail,
        ):
            denied = await client.get(url)
            assert denied.status_code == 403, denied.text
            assert denied.json()["detail"]["message"] == (
                "A computer's token reaches only what that computer holds"
            )
            for secret in banned:
                assert secret not in denied.text

        holder["principal"] = _session(owner)
        own = await _expect(await client.get(_paged(feed, limit=20, offset=0)), 200)
        assert _run_ids(own) == session_order
        assert old_a in str(own)
        assert old_a_delivery in str(own)
        assert old_home in str(own)
        assert old_research in str(own)
        assert other_excerpt not in str(own)
        assert company_excerpt not in str(own)
        personal_own = await _expect(
            await client.get(_paged(personal_history, limit=20, offset=0)),
            200,
        )
        assert _run_ids(personal_own) == [
            run_a_top,
            run_home,
            run_personal_inflight,
            run_b_first,
            run_a_mid,
            run_b_second,
            run_none,
        ]
        research_own = await _expect(
            await client.get(_paged(research_feed, limit=20, offset=0)),
            200,
        )
        assert _run_ids(research_own) == [
            run_research_a,
            run_research_inflight,
            run_research_b,
            run_research_none,
        ]
        session_default = await _expect(await client.get(task_list), 200)
        assert _ids(session_default) == owner_task_ids
        assert other_id not in _ids(session_default)
        assert company_id not in _ids(session_default)
        session_tasks = _tasks(session_default)
        _assert_last(
            session_tasks[personal_id],
            execution_id=run_personal_inflight,
            excerpt=old_personal_live,
            error_message=old_personal_live_error,
            thread_id=thread_a,
            status="running",
            delivery_note=old_personal_live_delivery,
        )
        _assert_last(
            session_tasks[research_id],
            execution_id=run_research_inflight,
            excerpt=old_research_live,
            error_message=old_research_live_error,
            thread_id=thread_research_a,
            status="running",
            delivery_note=old_research_live_delivery,
        )
        _assert_last(
            session_tasks[foreign_only_id],
            execution_id=run_foreign_live,
            excerpt=foreign_live,
            error_message=foreign_live_error,
            thread_id=thread_a,
            status="running",
            delivery_note=foreign_live_delivery,
        )
        session_personal = await _expect(await client.get(personal_list), 200)
        assert _ids(session_personal) == [personal_id, foreign_only_id]
        session_research = await _expect(await client.get(research_list), 200)
        assert _ids(session_research) == [research_id]
        _assert_last(
            _tasks(session_personal)[personal_id],
            execution_id=run_personal_inflight,
            excerpt=old_personal_live,
            error_message=old_personal_live_error,
            thread_id=thread_a,
            status="running",
            delivery_note=old_personal_live_delivery,
        )
        _assert_last(
            _tasks(session_research)[research_id],
            execution_id=run_research_inflight,
            excerpt=old_research_live,
            error_message=old_research_live_error,
            thread_id=thread_research_a,
            status="running",
            delivery_note=old_research_live_delivery,
        )
        session_personal_detail = await _expect(await client.get(personal_detail), 200)
        session_research_detail = await _expect(await client.get(research_detail), 200)
        session_foreign_detail = await _expect(await client.get(foreign_detail), 200)
        _assert_last(
            session_personal_detail,
            execution_id=run_personal_inflight,
            excerpt=old_personal_live,
            error_message=old_personal_live_error,
            thread_id=thread_a,
            status="running",
            delivery_note=old_personal_live_delivery,
        )
        _assert_last(
            session_research_detail,
            execution_id=run_research_inflight,
            excerpt=old_research_live,
            error_message=old_research_live_error,
            thread_id=thread_research_a,
            status="running",
            delivery_note=old_research_live_delivery,
        )
        _assert_last(
            session_foreign_detail,
            execution_id=run_foreign_live,
            excerpt=foreign_live,
            error_message=foreign_live_error,
            thread_id=thread_a,
            status="running",
            delivery_note=foreign_live_delivery,
        )

    assert await _execution_rows(au1_live_pool) == stored_runs
    assert await _thread_places(au1_live_pool) == stored_threads
    assert await _automation_version(au1_live_pool, personal_id) == personal_version
    assert await _automation_version(au1_live_pool, research_id) == research_version
    assert await _automation_version(au1_live_pool, foreign_only_id) == foreign_version
