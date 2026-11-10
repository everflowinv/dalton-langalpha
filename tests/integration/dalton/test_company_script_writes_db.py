"""Real PostgreSQL coverage for company script schedule, pause, and resume.

The pool is the AU1 foundation pool, function-scoped so it stays on this test's
event loop. Routes, services, row locks, and history use that database. No
script, host bridge, or external send is started.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
import json
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import pytest
import pytest_asyncio

from src.dalton.autonomy.system_jobs import toggle_system_job
from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.data_jobs.manifest import sync_manifest_to_db
from src.dalton.data_jobs.scheduler import enqueue_due_cron_tasks
from src.dalton.services import company_automations as company_auto_svc
from src.dalton.services.company_script_writes import (
    pause_company_script,
    update_company_script,
)
from src.server.app import automations as automations_app
from src.server.dependencies import principal as principal_mod
from src.server.models.automation import CompanyScriptUpdate
from tests.integration.dalton.test_au1_company_foundation_db import (
    _insert_user,
    _insert_workspace,
    au1_live_pool as _foundation_au1_live_pool,
)

pytestmark = pytest.mark.integration

INTERNAL = "u-script-a"
OTHER = "u-script-b"
ADMIN = "u-script-admin"
EXTERNAL = "u-script-external"
JOB = "cn-leverage-daily"
SENTINEL = "SCRIPT_SECRET_SENTINEL"
LEGACY_ROUTE = "feishu:chat:oc_legacy_only"
TASK_ROUTE = "feishu:chat:oc_task_row"
OTHER_ROUTE = "feishu:chat:oc_pref_other"
NEW_CRON = "15 18 * * 1-5"
MATCH_CRON = "0 9 * * 1-5"
OFF_CRON = "0 10 * * 1-5"
ZONE = "Asia/Shanghai"
MATCH_SLOT = datetime(2026, 11, 6, 1, 0, tzinfo=UTC)
ALLOWED = frozenset({INTERNAL, OTHER, ADMIN})
AGENT_ID = "c0ffee00-1111-4111-8111-111111111111"


@pytest_asyncio.fixture
async def au1_live_pool(
    dalton_schema,
    cleanup_dalton_tables,
    test_db_uri: str,
):
    """One foundation pool for this test, on the test's loop."""
    raw = getattr(_foundation_au1_live_pool, "__wrapped__", _foundation_au1_live_pool)
    generator = raw(dalton_schema, cleanup_dalton_tables, test_db_uri)
    pool = await anext(generator)
    try:
        yield pool
    finally:
        await anext(generator, None)


@pytest.fixture
def internal_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _allowed(user_id: str | None) -> bool:
        return str(user_id or "") in ALLOWED

    monkeypatch.setattr(company_auto_svc, "is_internal_or_admin", _allowed)


@pytest_asyncio.fixture
async def script_db(au1_live_pool: AsyncConnectionPool, internal_tier: None):
    async with au1_live_pool.connection() as conn:
        await _users(conn)
        await sync_manifest_to_db(conn)
    return au1_live_pool


@pytest_asyncio.fixture
async def script_api(script_db: AsyncConnectionPool):
    holder: dict[str, principal_mod.Principal] = {"principal": _principal(INTERNAL)}
    app = _app(holder)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, holder, script_db


def _principal(
    user_id: str,
    *,
    computer_id: str | None = None,
) -> principal_mod.Principal:
    if computer_id is not None:
        return principal_mod.Principal(
            user_id=user_id,
            auth="computer",
            computer_id=computer_id,
            scopes=("workspace:write",),
        )
    return principal_mod.Principal(user_id=user_id, auth="session")


def _app(holder: dict[str, principal_mod.Principal]) -> FastAPI:
    app = FastAPI()
    app.include_router(automations_app.router)
    app.dependency_overrides[principal_mod.get_principal] = lambda: holder["principal"]
    return app


def _json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


async def _users(conn: psycopg.AsyncConnection) -> None:
    for user_id, email in (
        (INTERNAL, "script-a@dalton.test"),
        (OTHER, "script-b@dalton.test"),
        (ADMIN, "script-admin@dalton.test"),
        (EXTERNAL, "script-external@dalton.test"),
    ):
        await _insert_user(conn, user_id, email)


async def _read_def(pool: AsyncConnectionPool, job_key: str = JOB) -> dict[str, Any]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT cron_expr, timezone, enabled, paused_by_user,
                       notify_enabled, notify_routes, notify_storage_mode, cos_mode,
                       config_version, active_version, updated_by, audit_history,
                       migration_state, deleted_at, updated_at, command_argv,
                       required_secrets
                FROM dalton_data_task_defs
                WHERE job_key = %s
                """,
                (job_key,),
            )
            row = await cur.fetchone()
    assert row is not None
    row["notify_routes"] = _json(row["notify_routes"])
    row["audit_history"] = _json(row["audit_history"])
    row["migration_state"] = _json(row["migration_state"])
    row["command_argv"] = _json(row["command_argv"])
    row["required_secrets"] = _json(row["required_secrets"])
    return row


async def _runs(pool: AsyncConnectionPool, job_key: str = JOB) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT run_id::text AS run_id, status, trigger_source,
                       failure_reason, notify_status, completed_at,
                       frozen_snapshot_dir
                FROM dalton_data_task_runs
                WHERE job_key = %s
                ORDER BY created_at ASC, run_id ASC
                """,
                (job_key,),
            )
            return list(await cur.fetchall())


async def _set_def(pool: AsyncConnectionPool, **fields: Any) -> None:
    assignments: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key in {"notify_routes", "migration_state", "command_argv", "required_secrets", "audit_history"}:
            assignments.append(f"{key} = %s::jsonb")
            params.append(None if value is None else json.dumps(value))
        else:
            assignments.append(f"{key} = %s")
            params.append(value)
    params.append(JOB)
    async with pool.connection() as conn:
        await conn.execute(
            f"UPDATE dalton_data_task_defs SET {', '.join(assignments)} WHERE job_key = %s",
            tuple(params),
        )


async def _insert_run(
    pool: AsyncConnectionPool,
    *,
    status: str,
    trigger_source: str,
    slot: datetime,
) -> str:
    run_id = str(uuid4())
    started = slot if status in {"starting", "running"} else None
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO dalton_data_task_runs (
                run_id, job_key, trigger_source, scheduled_slot_utc, deadline_utc,
                status, started_at, created_at, updated_at
            ) VALUES (
                %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                run_id,
                JOB,
                trigger_source,
                slot,
                slot + timedelta(hours=1),
                status,
                started,
                slot,
                slot,
            ),
        )
    return run_id


async def _def_count(pool: AsyncConnectionPool) -> int:
    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT COUNT(*) FROM dalton_data_task_defs")).fetchone()
    assert row is not None
    return int(row[0])


def _script_body(**extra: Any) -> dict[str, Any]:
    body = {"expected_config_version": 1}
    body.update(extra)
    return body


async def _wait_for_def_lock(pool: AsyncConnectionPool, pid: int, blocker_pid: int) -> None:
    """Wait until enqueue is blocked on the definition row, not a preference lock."""
    deadline = asyncio.get_running_loop().time() + 8
    while asyncio.get_running_loop().time() < deadline:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT pg_blocking_pids(pid), query
                    FROM pg_stat_activity
                    WHERE pid = %s
                    """,
                    (pid,),
                )
                waiting = await cur.fetchone()
                await cur.execute(
                    """
                    SELECT locktype
                    FROM pg_locks
                    WHERE pid = %s
                      AND locktype = 'advisory'
                    """,
                    (pid,),
                )
                advisory = await cur.fetchall()
        if waiting and blocker_pid in waiting[0]:
            assert "dalton_data_task_defs" in waiting[1]
            assert "FOR UPDATE" in waiting[1]
            assert advisory == []
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"enqueue pid {pid} did not wait on dalton_data_task_defs")


async def _race_enqueue(pool: AsyncConnectionPool, mutate_sql: str, mutate_params: tuple[Any, ...]) -> list[str]:
    async with pool.connection() as conn_b:
        pid_row = await (await conn_b.execute("SELECT pg_backend_pid()")).fetchone()
        assert pid_row is not None
        pid = int(pid_row[0])
        async with pool.connection() as conn_a:
            task = None
            try:
                async with conn_a.transaction():
                    await conn_a.execute(
                        """
                        SELECT job_key
                        FROM dalton_data_task_defs
                        WHERE job_key = %s
                        FOR UPDATE
                        """,
                        (JOB,),
                    )
                    task = asyncio.create_task(enqueue_due_cron_tasks(conn_b, now_utc=MATCH_SLOT))
                    await _wait_for_def_lock(pool, pid, conn_a.info.backend_pid)
                    await conn_a.execute(mutate_sql, mutate_params)
                return await asyncio.wait_for(task, timeout=5)
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


@pytest.mark.asyncio
async def test_schedule_pause_resume_actor_history_and_reads(
    script_api: tuple[AsyncClient, dict[str, principal_mod.Principal], AsyncConnectionPool],
) -> None:
    client, holder, pool = script_api
    await _set_def(
        pool,
        required_secrets=[SENTINEL],
        command_argv=["echo", SENTINEL],
        cos_mode="prod",
        notify_enabled=True,
        notify_routes=None,
        notify_storage_mode="legacy",
        migration_state={"legacy_notify_owner_user_id": INTERNAL, "note": "keep-me"},
    )
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO user_preferences (user_id, model_preference)
            VALUES (%s, %s::jsonb)
            """,
            (
                INTERNAL,
                json.dumps({"dalton_notify_routes": {f"data:{JOB}": [LEGACY_ROUTE]}}),
            ),
        )
    before = await _read_def(pool)
    queued = await _insert_run(
        pool,
        status="queued",
        trigger_source="cron",
        slot=datetime(2026, 11, 2, 1, 0, tzinfo=UTC),
    )

    same = await client.patch(
        f"/api/v1/automations/data:{JOB}",
        json=_script_body(cron_expression=before["cron_expr"], timezone=before["timezone"]),
    )
    assert same.status_code == 200, same.text
    unchanged = await _read_def(pool)
    assert unchanged["config_version"] == before["config_version"]
    assert unchanged["updated_at"] == before["updated_at"]
    assert unchanged["audit_history"] == before["audit_history"]
    assert (await _runs(pool))[0]["status"] == "queued"

    holder["principal"] = _principal(ADMIN)
    paused = await client.post(
        f"/api/v1/automations/data:{JOB}/pause",
        json={"expected_config_version": 1},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["paused_by_user"] is True
    assert paused.json()["enabled"] is False
    assert paused.json()["updated_by"] == ADMIN
    assert SENTINEL not in paused.text
    paused_row = await _read_def(pool)
    assert paused_row["config_version"] == 2
    assert paused_row["updated_by"] == ADMIN
    assert paused_row["notify_routes"] is None
    assert paused_row["notify_storage_mode"] == "legacy"
    assert paused_row["notify_enabled"] is True
    assert paused_row["cos_mode"] == "prod"
    assert paused_row["migration_state"]["note"] == "keep-me"
    assert paused_row["required_secrets"] == [SENTINEL]
    assert paused_row["audit_history"][0] == before["audit_history"][0]
    assert paused_row["audit_history"][1]["actor_user_id"] == ADMIN
    assert paused_row["audit_history"][1]["action"] == "pause"
    assert paused_row["audit_history"][1]["prev_version"] == 1
    assert paused_row["audit_history"][1]["snapshot"]["notify_routes"] is None
    queued_row = next(row for row in await _runs(pool) if row["run_id"] == queued)
    assert queued_row["status"] == "cancelled"
    assert queued_row["failure_reason"] == "paused_before_start"
    assert queued_row["notify_status"] == "skipped"
    assert queued_row["completed_at"] is not None
    assert queued_row["frozen_snapshot_dir"] is None

    again = await client.post(
        f"/api/v1/automations/data:{JOB}/pause",
        json={"expected_config_version": 2},
    )
    assert again.status_code == 200, again.text
    assert (await _read_def(pool))["config_version"] == 2

    holder["principal"] = _principal(INTERNAL)
    resumed = await client.post(
        f"/api/v1/automations/data:{JOB}/resume",
        json={"expected_config_version": 2},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["enabled"] is True
    assert resumed.json()["paused_by_user"] is False
    assert resumed.json()["updated_by"] == INTERNAL
    assert (await _read_def(pool))["config_version"] == 3

    changed = await client.patch(
        f"/api/v1/automations/data:{JOB}",
        json=_script_body(cron_expression=NEW_CRON, expected_config_version=3),
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["cron_expression"] == NEW_CRON
    assert changed.json()["engine_kind"] == "script"
    detail = await client.get(f"/api/v1/automations/data:{JOB}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["cron_expression"] == NEW_CRON
    listed = await client.get("/api/v1/automations", params={"scope": "company", "limit": 100})
    assert listed.status_code == 200, listed.text
    match = next(item for item in listed.json()["automations"] if item["automation_id"] == f"data:{JOB}")
    assert match["cron_expression"] == NEW_CRON
    newest = await client.get(
        f"/api/v1/automations/data:{JOB}/history",
        params={"limit": 1, "offset": 0},
    )
    assert newest.status_code == 200, newest.text
    body = newest.json()
    assert body["entries"][0]["version"] == 4
    assert body["entries"][0]["actor_user_id"] == INTERNAL
    assert body["entries"][0]["action"] == "update"
    assert SENTINEL not in newest.text
    older = await client.get(
        f"/api/v1/automations/data:{JOB}/history",
        params={"limit": 1, "offset": 3},
    )
    assert older.json()["entries"][0]["version"] == 1
    assert older.json()["total"] == 4


@pytest.mark.asyncio
async def test_task_row_notify_columns_stay_on_the_definition(
    script_api: tuple[AsyncClient, dict[str, principal_mod.Principal], AsyncConnectionPool],
) -> None:
    client, _holder, pool = script_api
    await _set_def(
        pool,
        notify_storage_mode="task_row",
        notify_routes=[TASK_ROUTE],
        notify_enabled=False,
        cos_mode="skip",
    )
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO user_preferences (user_id, model_preference)
            VALUES (%s, %s::jsonb)
            """,
            (
                INTERNAL,
                json.dumps({"dalton_notify_routes": {f"data:{JOB}": [OTHER_ROUTE]}}),
            ),
        )
    response = await client.patch(
        f"/api/v1/automations/data:{JOB}",
        json=_script_body(timezone="Asia/Hong_Kong"),
    )
    assert response.status_code == 200, response.text
    row = await _read_def(pool)
    assert row["notify_storage_mode"] == "task_row"
    assert row["notify_routes"] == [TASK_ROUTE]
    assert row["notify_enabled"] is False
    assert row["cos_mode"] == "skip"
    assert row["timezone"] == "Asia/Hong_Kong"
    snapshot = row["audit_history"][-1]["snapshot"]
    assert snapshot["notify_routes"] == [TASK_ROUTE]
    assert snapshot["notify_storage_mode"] == "task_row"
    assert OTHER_ROUTE not in json.dumps(snapshot)


@pytest.mark.asyncio
async def test_pause_keeps_starting_and_running_and_schedule_keeps_manual(
    script_api: tuple[AsyncClient, dict[str, principal_mod.Principal], AsyncConnectionPool],
) -> None:
    client, _holder, pool = script_api
    await _set_def(pool, enabled=True, paused_by_user=False)
    queued = await _insert_run(
        pool,
        status="queued",
        trigger_source="cron",
        slot=datetime(2026, 11, 2, 1, 0, tzinfo=UTC),
    )
    starting = await _insert_run(
        pool,
        status="starting",
        trigger_source="cron",
        slot=datetime(2026, 11, 3, 1, 0, tzinfo=UTC),
    )
    running = await _insert_run(
        pool,
        status="running",
        trigger_source="cron",
        slot=datetime(2026, 11, 4, 1, 0, tzinfo=UTC),
    )
    paused = await client.post(
        f"/api/v1/automations/data:{JOB}/pause",
        json={"expected_config_version": 1},
    )
    assert paused.status_code == 200, paused.text
    by_id = {row["run_id"]: row for row in await _runs(pool)}
    assert by_id[queued]["status"] == "cancelled"
    assert by_id[queued]["failure_reason"] == "paused_before_start"
    assert by_id[starting]["status"] == "starting"
    assert by_id[starting]["failure_reason"] is None
    assert by_id[starting]["completed_at"] is None
    assert by_id[running]["status"] == "running"
    assert by_id[running]["frozen_snapshot_dir"] is None

    manual = await _insert_run(
        pool,
        status="queued",
        trigger_source="manual",
        slot=datetime(2026, 11, 5, 1, 0, tzinfo=UTC),
    )
    moved = await client.patch(
        f"/api/v1/automations/data:{JOB}",
        json=_script_body(cron_expression=NEW_CRON, expected_config_version=2),
    )
    assert moved.status_code == 200, moved.text
    manual_row = next(row for row in await _runs(pool) if row["run_id"] == manual)
    assert manual_row["status"] == "queued"
    assert manual_row["failure_reason"] is None
    assert by_id[starting]["status"] == (await _runs(pool) and next(
        row for row in await _runs(pool) if row["run_id"] == starting
    ))["status"]


@pytest.mark.asyncio
async def test_schedule_change_cancels_queued_cron_only(
    script_api: tuple[AsyncClient, dict[str, principal_mod.Principal], AsyncConnectionPool],
) -> None:
    client, _holder, pool = script_api
    queued = await _insert_run(
        pool,
        status="queued",
        trigger_source="cron",
        slot=datetime(2026, 11, 2, 1, 0, tzinfo=UTC),
    )
    starting = await _insert_run(
        pool,
        status="starting",
        trigger_source="manual",
        slot=datetime(2026, 11, 3, 1, 0, tzinfo=UTC),
    )
    response = await client.patch(
        f"/api/v1/automations/data:{JOB}",
        json=_script_body(cron_expression=NEW_CRON),
    )
    assert response.status_code == 200, response.text
    by_id = {row["run_id"]: row for row in await _runs(pool)}
    assert by_id[queued]["status"] == "cancelled"
    assert by_id[queued]["failure_reason"] == "schedule_changed_before_start"
    assert by_id[queued]["notify_status"] == "skipped"
    assert by_id[starting]["status"] == "starting"
    assert by_id[starting]["completed_at"] is None


@pytest.mark.asyncio
async def test_resume_rejects_active_manual_trial_and_stays_disabled(
    script_api: tuple[AsyncClient, dict[str, principal_mod.Principal], AsyncConnectionPool],
) -> None:
    client, _holder, pool = script_api
    run_id = await _insert_run(
        pool,
        status="queued",
        trigger_source="manual",
        slot=datetime(2026, 11, 2, 1, 0, tzinfo=UTC),
    )
    refused = await client.post(
        f"/api/v1/automations/data:{JOB}/resume",
        json={"expected_config_version": 1},
    )
    assert refused.status_code == 409, refused.text
    assert run_id in refused.text
    row = await _read_def(pool)
    assert row["enabled"] is False
    assert row["config_version"] == 1
    assert (await _runs(pool))[0]["status"] == "queued"

    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE dalton_data_task_runs SET status = 'succeeded', completed_at = NOW() WHERE run_id = %s::uuid",
            (run_id,),
        )
    allowed = await client.post(
        f"/api/v1/automations/data:{JOB}/resume",
        json={"expected_config_version": 1},
    )
    assert allowed.status_code == 200, allowed.text
    assert (await _read_def(pool))["enabled"] is True


@pytest.mark.asyncio
async def test_identity_manifest_deleted_and_frozen_refusals(
    script_api: tuple[AsyncClient, dict[str, principal_mod.Principal], AsyncConnectionPool],
) -> None:
    client, holder, pool = script_api
    before_count = await _def_count(pool)
    missing_manifest = await client.patch(
        "/api/v1/automations/data:not-a-manifest-task",
        json=_script_body(cron_expression=NEW_CRON),
    )
    assert missing_manifest.status_code == 404, missing_manifest.text
    assert await _def_count(pool) == before_count

    malformed = await client.post("/api/v1/automations/not-a-uuid/pause", json={"name": "X"})
    assert malformed.status_code == 422, malformed.text
    bad_version = await client.patch(
        f"/api/v1/automations/data:{JOB}",
        json={"cron_expression": NEW_CRON, "expected_config_version": True, "enabled": False},
    )
    assert bad_version.status_code == 422, bad_version.text
    assert (await _read_def(pool))["config_version"] == 1

    holder["principal"] = _principal(EXTERNAL)
    external = await client.patch(
        f"/api/v1/automations/data:{JOB}",
        json=_script_body(cron_expression=NEW_CRON),
    )
    assert external.status_code == 403, external.text
    holder["principal"] = _principal(INTERNAL, computer_id=str(uuid4()))
    computer = await client.post(
        f"/api/v1/automations/data:{JOB}/pause",
        json={"expected_config_version": 1},
    )
    assert computer.status_code == 403, computer.text
    assert "Forbidden" in computer.text
    holder["principal"] = _principal(COMPANY_SYSTEM_USER_ID)
    system = await client.post(
        f"/api/v1/automations/data:{JOB}/resume",
        json={"expected_config_version": 1},
    )
    assert system.status_code == 401, system.text
    assert "Company system identity cannot authenticate externally" in system.text

    with pytest.raises(HTTPException) as direct_system:
        await pause_company_script(
            _principal(COMPANY_SYSTEM_USER_ID),
            JOB,
            expected_config_version=1,
        )
    assert direct_system.value.status_code == 403
    with pytest.raises(HTTPException) as direct_computer:
        await pause_company_script(
            _principal(INTERNAL, computer_id=str(uuid4())),
            JOB,
            expected_config_version=1,
        )
    assert direct_computer.value.status_code == 403

    holder["principal"] = _principal(INTERNAL)
    await _set_def(pool, deleted_at=datetime.now(UTC), deleted_by=INTERNAL)
    deleted = await client.patch(
        f"/api/v1/automations/data:{JOB}",
        json=_script_body(cron_expression=NEW_CRON),
    )
    assert deleted.status_code == 404, deleted.text
    await _set_def(pool, deleted_at=None, deleted_by=None)

    for stage in ("frozen", "rollback_frozen", "switching"):
        await _set_def(pool, migration_state={"company_migration": {"stage": stage}})
        frozen = await client.patch(
            f"/api/v1/automations/data:{JOB}",
            json=_script_body(cron_expression=NEW_CRON),
        )
        assert frozen.status_code == 409, frozen.text
        assert (await _read_def(pool))["config_version"] == 1
    await _set_def(pool, migration_state={"company_migration": {"stage": 1}})
    malformed_state = await client.patch(
        f"/api/v1/automations/data:{JOB}",
        json=_script_body(cron_expression=NEW_CRON),
    )
    assert malformed_state.status_code == 409, malformed_state.text
    await _set_def(pool, migration_state={"company_migration": {"stage": "running"}})
    allowed = await client.patch(
        f"/api/v1/automations/data:{JOB}",
        json=_script_body(cron_expression=NEW_CRON),
    )
    assert allowed.status_code == 200, allowed.text
    assert (await _read_def(pool))["migration_state"]["company_migration"]["stage"] == "running"


@pytest.mark.asyncio
async def test_missing_definition_is_not_created(
    script_api: tuple[AsyncClient, dict[str, principal_mod.Principal], AsyncConnectionPool],
) -> None:
    client, _holder, pool = script_api
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM dalton_data_task_defs WHERE job_key = %s", (JOB,))
    missing = await client.patch(
        f"/api/v1/automations/data:{JOB}",
        json=_script_body(cron_expression=NEW_CRON),
    )
    assert missing.status_code == 503, missing.text
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT 1 FROM dalton_data_task_defs WHERE job_key = %s",
                (JOB,),
            )
        ).fetchone()
    assert row is None


@pytest.mark.asyncio
async def test_two_actors_one_version_and_old_settings_conflict(
    script_api: tuple[AsyncClient, dict[str, principal_mod.Principal], AsyncConnectionPool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _client, _holder, pool = script_api
    request = CompanyScriptUpdate.model_validate(
        {"cron_expression": NEW_CRON, "expected_config_version": 1}
    )

    async def _once(user_id: str) -> int:
        try:
            await update_company_script(_principal(user_id), JOB, request)
        except HTTPException as exc:
            return exc.status_code
        return 200

    results = await asyncio.gather(_once(INTERNAL), _once(OTHER))
    assert sorted(results) == [200, 409]
    row = await _read_def(pool)
    assert row["config_version"] == 2
    assert row["cron_expr"] == NEW_CRON
    assert len(row["audit_history"]) == 2
    assert row["audit_history"][-1]["actor_user_id"] in {INTERNAL, OTHER}

    async def _tier(_user_id: str) -> str:
        return "internal"

    monkeypatch.setattr(
        "src.dalton.autonomy.system_jobs.get_configured_pm_user_ids",
        lambda: [INTERNAL],
    )
    monkeypatch.setattr("src.dalton.autonomy.system_jobs.get_user_tier", _tier)
    await toggle_system_job(INTERNAL, f"data:{JOB}", cron_expr="30 18 * * 1-5")
    after_toggle = await _read_def(pool)
    assert after_toggle["config_version"] == 3
    stale = CompanyScriptUpdate.model_validate(
        {"cron_expression": "45 18 * * 1-5", "expected_config_version": 2}
    )
    with pytest.raises(HTTPException) as conflict:
        await update_company_script(_principal(INTERNAL), JOB, stale)
    assert conflict.value.status_code == 409
    assert (await _read_def(pool))["config_version"] == 3
    assert (await _read_def(pool))["cron_expr"] == "30 18 * * 1-5"


@pytest.mark.asyncio
async def test_manifest_sync_keeps_schedule_pause_and_version(
    script_api: tuple[AsyncClient, dict[str, principal_mod.Principal], AsyncConnectionPool],
) -> None:
    client, _holder, pool = script_api
    paused = await client.post(
        f"/api/v1/automations/data:{JOB}/pause",
        json={"expected_config_version": 1},
    )
    assert paused.status_code == 200, paused.text
    changed = await client.patch(
        f"/api/v1/automations/data:{JOB}",
        json=_script_body(cron_expression=NEW_CRON, expected_config_version=2),
    )
    assert changed.status_code == 200, changed.text
    before = await _read_def(pool)
    async with pool.connection() as conn:
        await sync_manifest_to_db(conn)
    after = await _read_def(pool)
    assert after["cron_expr"] == NEW_CRON
    assert after["paused_by_user"] is True
    assert after["enabled"] is False
    assert after["config_version"] == before["config_version"]
    assert after["active_version"] == before["active_version"]
    assert after["audit_history"] == before["audit_history"]


@pytest.mark.asyncio
async def test_uuid_agent_patch_does_not_change_the_script(
    script_api: tuple[AsyncClient, dict[str, principal_mod.Principal], AsyncConnectionPool],
) -> None:
    client, holder, pool = script_api
    workspace_id = str(uuid4())
    async with pool.connection() as conn:
        await _insert_workspace(conn, workspace_id, COMPANY_SYSTEM_USER_ID, "Script Neighbor")
        await conn.execute(
            """
            INSERT INTO automations (
                automation_id, user_id, name, file_name, description, trigger_type,
                cron_expression, timezone, instruction, workspace_id,
                status, thread_strategy, scope, notify_enabled, notify_routes,
                notify_storage_mode, metadata,
                config_version, active_version, audit_history, created_by, updated_by
            ) VALUES (
                %s::uuid, %s, 'Neighbor', 'script-neighbor.md', 'before', 'cron',
                '0 9 * * 1-5', 'Asia/Shanghai', 'Read the book', %s::uuid,
                'active', 'new', 'company', TRUE, %s::jsonb,
                'task_row', %s::jsonb,
                1, 1, '[]'::jsonb, %s, %s
            )
            """,
            (
                AGENT_ID,
                COMPANY_SYSTEM_USER_ID,
                workspace_id,
                json.dumps(["feishu:chat:oc_company"]),
                json.dumps({"company_migration": {"stage": "running"}}),
                INTERNAL,
                INTERNAL,
            ),
        )
    script_before = await _read_def(pool)
    holder["principal"] = _principal(INTERNAL)
    agent = await client.patch(
        f"/api/v1/automations/{AGENT_ID.upper()}",
        json={"description": "after-agent", "expected_config_version": 1},
    )
    assert agent.status_code == 200, agent.text
    assert agent.json()["description"] == "after-agent"
    assert (await _read_def(pool))["cron_expr"] == script_before["cron_expr"]
    assert (await _read_def(pool))["config_version"] == script_before["config_version"]

    script = await client.patch(
        f"/api/v1/automations/data:{JOB}",
        json=_script_body(cron_expression=NEW_CRON),
    )
    assert script.status_code == 200, script.text
    async with pool.connection() as conn:
        description = await (
            await conn.execute(
                "SELECT description FROM automations WHERE automation_id = %s::uuid",
                (AGENT_ID,),
            )
        ).fetchone()
    assert description is not None
    assert description[0] == "after-agent"
    bad_agent = await client.patch(
        f"/api/v1/automations/{AGENT_ID}",
        json={"description": "nope", "expected_config_version": True},
    )
    assert bad_agent.status_code == 400, bad_agent.text
    unknown = await client.patch(
        f"/api/v1/automations/{uuid4()}",
        json={"description": "missing", "expected_config_version": 1},
    )
    assert unknown.status_code == 404, unknown.text


@pytest.mark.asyncio
async def test_enqueue_waits_for_pause_or_schedule_then_inserts_nothing(
    script_db: AsyncConnectionPool,
) -> None:
    pool = script_db
    await _set_def(
        pool,
        enabled=True,
        paused_by_user=False,
        cron_expr=MATCH_CRON,
        timezone=ZONE,
    )
    paused = await _race_enqueue(
        pool,
        """
        UPDATE dalton_data_task_defs
        SET enabled = FALSE, paused_by_user = TRUE
        WHERE job_key = %s
        """,
        (JOB,),
    )
    assert paused == []
    assert await _runs(pool) == []

    await _set_def(
        pool,
        enabled=True,
        paused_by_user=False,
        cron_expr=MATCH_CRON,
        timezone=ZONE,
    )
    rescheduled = await _race_enqueue(
        pool,
        """
        UPDATE dalton_data_task_defs
        SET cron_expr = %s
        WHERE job_key = %s
        """,
        (OFF_CRON, JOB),
    )
    assert rescheduled == []
    assert await _runs(pool) == []


@pytest.mark.asyncio
async def test_pause_does_not_take_the_profile_advisory_lock(
    script_db: AsyncConnectionPool,
) -> None:
    pool = script_db
    async with pool.connection() as holder:
        async with holder.transaction():
            await holder.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"userdata:profile:{INTERNAL}",),
            )
            await asyncio.wait_for(
                pause_company_script(
                    _principal(INTERNAL),
                    JOB,
                    expected_config_version=1,
                ),
                timeout=5,
            )
    row = await _read_def(pool)
    assert row["paused_by_user"] is True
    assert row["updated_by"] == INTERNAL
    assert row["config_version"] == 2
