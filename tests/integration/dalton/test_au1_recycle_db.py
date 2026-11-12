"""Database coverage for soft delete, restore, rollback, listing, and purge."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
import pytest

from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.database import automation_recycle as automation_recycle_db
from src.dalton.database import company_automations as company_automations_db
from src.dalton.database import company_data_tasks as company_data_tasks_db
from src.dalton.notify import dispatch as notify_dispatch
from src.dalton.services import automation_recycle as automation_recycle
from src.dalton.services import company_automations as company_automations
from src.dalton.services import company_script_writes as company_script_writes
from src.dalton.services import company_task_read as company_task_read
from src.server.database import automation as automation_db
from src.server.database import user as user_db
from src.server.dependencies.principal import Principal

# Loaded before the test fixture replaces get_db_connection on already-imported modules.
_POOL_HOLDERS = (
    automation_recycle_db,
    company_automations_db,
    company_data_tasks_db,
    notify_dispatch,
    automation_recycle,
    company_automations,
    company_script_writes,
    company_task_read,
    automation_db,
    user_db,
)

pytestmark = pytest.mark.integration

JOB_KEY = "bbg-data-pull-asia"


def _principal(user_id: str) -> Principal:
    return Principal(user_id=user_id, auth="session")


def _cell(row, key: str, index: int):
    if isinstance(row, dict):
        return row[key]
    return row[index]


async def _fetch(sql: str, params: tuple):
    async with automation_recycle.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()


async def _execute(sql: str, params: tuple) -> None:
    async with automation_recycle.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)


async def _create(user_id: str, **kwargs):
    from src.server.database.automation import create_automation

    return await create_automation(
        user_id=user_id,
        name=kwargs.pop("name", "Recycle task"),
        trigger_type=kwargs.pop("trigger_type", "cron"),
        instruction="Recycle the row",
        cron_expression=kwargs.pop("cron_expression", "0 9 * * 1-5"),
        **kwargs,
    )


async def _insert_execution(automation_id: str, status: str) -> str:
    execution_id = str(uuid4())
    await _execute(
        """
        INSERT INTO automation_executions (
            automation_execution_id, automation_id, status, scheduled_at, created_at
        ) VALUES (%s::uuid, %s::uuid, %s, NOW(), NOW())
        """,
        (execution_id, automation_id, status),
    )
    return execution_id


@pytest.mark.asyncio
async def test_soft_delete_skips_unstarted_runs_and_keeps_running(
    seed_user, patched_get_db_connection
):
    from src.dalton.services.automation_recycle import delete_task

    auto = await _create(seed_user["user_id"], name="Delete me")
    automation_id = str(auto["automation_id"])
    pending_id = await _insert_execution(automation_id, "pending")
    running_id = await _insert_execution(automation_id, "running")

    result = await delete_task(_principal(seed_user["user_id"]), automation_id, 1)

    assert result.deleted_at is not None
    assert result.deleted_by == seed_user["user_id"]
    assert result.status == "paused"
    pending = await _fetch(
        "SELECT status, skip_reason FROM automation_executions WHERE automation_execution_id = %s::uuid",
        (pending_id,),
    )
    running = await _fetch(
        "SELECT status FROM automation_executions WHERE automation_execution_id = %s::uuid",
        (running_id,),
    )
    assert _cell(pending, "status", 0) == "skipped"
    assert _cell(pending, "skip_reason", 1) == "task_deleted"
    assert _cell(running, "status", 0) == "running"

    with pytest.raises(HTTPException) as exc:
        await delete_task(_principal("someone-else"), automation_id, 2)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_research_delete_restore_and_include_deleted(
    seed_user, patched_get_db_connection
):
    from src.dalton.services.automation_recycle import delete_task, restore_task
    from src.dalton.services.company_automations import list_automations_for_scope
    from src.server.database.automation import list_automations

    auto = await _create(
        seed_user["user_id"],
        name="Research book",
        scope="research_tracking",
    )
    live = await _create(seed_user["user_id"], name="Still live")
    automation_id = str(auto["automation_id"])
    principal = _principal(seed_user["user_id"])

    await delete_task(principal, automation_id, 1)
    hidden, _total = await list_automations(seed_user["user_id"])
    hidden_ids = {str(row["automation_id"]) for row in hidden}
    assert automation_id not in hidden_ids
    assert str(live["automation_id"]) in hidden_ids

    deleted_rows, deleted_total = await list_automations(
        seed_user["user_id"],
        include_deleted=True,
    )
    assert deleted_total == 1
    assert str(deleted_rows[0]["automation_id"]) == automation_id
    assert deleted_rows[0]["deleted_at"] is not None

    scoped = await list_automations_for_scope(
        principal,
        scope="research_tracking",
        include_deleted=True,
    )
    assert scoped.total == 1
    assert str(scoped.automations[0].automation_id) == automation_id

    stored = await _fetch(
        "SELECT config_version FROM automations WHERE automation_id = %s::uuid",
        (automation_id,),
    )
    restored = await restore_task(
        principal,
        automation_id,
        int(_cell(stored, "config_version", 0)),
    )
    assert restored.deleted_at is None
    assert restored.status == "active"


@pytest.mark.asyncio
async def test_restore_renames_a_taken_file_and_pauses_an_expired_once(
    seed_user, patched_get_db_connection
):
    from src.dalton.services.automation_recycle import delete_task, restore_task

    user_id = seed_user["user_id"]
    principal = _principal(user_id)
    shared = await _create(user_id, name="Shared", file_name="shared-name.json")
    once = await _create(
        user_id,
        name="Old once",
        trigger_type="once",
        cron_expression=None,
        next_run_at=datetime.now(UTC) - timedelta(days=2),
        file_name="old-once.json",
    )
    await delete_task(principal, str(shared["automation_id"]), 1)
    await delete_task(principal, str(once["automation_id"]), 1)
    await _create(user_id, name="Taken", file_name="shared-name.json")

    shared_row = await _fetch(
        "SELECT config_version FROM automations WHERE automation_id = %s::uuid",
        (str(shared["automation_id"]),),
    )
    restored = await restore_task(
        principal,
        str(shared["automation_id"]),
        int(_cell(shared_row, "config_version", 0)),
    )
    renamed = await _fetch(
        "SELECT file_name, deleted_at FROM automations WHERE automation_id = %s::uuid",
        (str(shared["automation_id"]),),
    )
    assert restored.deleted_at is None
    assert _cell(renamed, "deleted_at", 1) is None
    assert _cell(renamed, "file_name", 0) != "shared-name.json"
    assert "-restored-" in _cell(renamed, "file_name", 0)

    once_row = await _fetch(
        "SELECT config_version FROM automations WHERE automation_id = %s::uuid",
        (str(once["automation_id"]),),
    )
    paused = await restore_task(
        principal,
        str(once["automation_id"]),
        int(_cell(once_row, "config_version", 0)),
    )
    assert paused.status == "paused"
    assert paused.next_run_at is None


@pytest.mark.asyncio
async def test_rollback_one_step_and_rejects_other_targets(
    seed_user, patched_get_db_connection
):
    from src.dalton.services.automation_recycle import rollback_task
    from src.dalton.services.company_automations import AutomationVersionConflictError
    from src.server.database.automation import update_automation

    auto = await _create(seed_user["user_id"], name="Original name")
    automation_id = str(auto["automation_id"])
    updated = await update_automation(
        automation_id,
        seed_user["user_id"],
        name="Renamed",
    )
    version = int(updated["config_version"])
    principal = _principal(seed_user["user_id"])

    with pytest.raises(HTTPException) as wrong_target:
        await rollback_task(
            principal,
            automation_id,
            expected_config_version=version,
            target_version=version,
        )
    assert wrong_target.value.status_code == 400

    with pytest.raises(AutomationVersionConflictError):
        await rollback_task(
            principal,
            automation_id,
            expected_config_version=version + 3,
            target_version=1,
        )

    rolled = await rollback_task(
        principal,
        automation_id,
        expected_config_version=version,
        target_version=1,
    )
    assert rolled.name == "Original name"
    assert rolled.config_version == version + 1


@pytest.mark.asyncio
async def test_company_human_can_delete_and_external_cannot(
    seed_user, patched_get_db_connection, monkeypatch
):
    from src.dalton.services.automation_recycle import delete_task

    async def _allowed(user_id: str | None) -> bool:
        return str(user_id or "") == seed_user["user_id"]

    monkeypatch.setattr(
        "src.dalton.services.company_automations.is_internal_or_admin",
        _allowed,
    )

    await _execute(
        """
        INSERT INTO users (user_id, email, name, created_at, updated_at)
        VALUES (%s, %s, %s, NOW(), NOW())
        ON CONFLICT (user_id) DO NOTHING
        """,
        (COMPANY_SYSTEM_USER_ID, "company-recycle@dalton.test", "company"),
    )
    agent_id = str(uuid4())
    await _execute(
        """
        INSERT INTO automations (
            automation_id, user_id, name, file_name, description, trigger_type,
            cron_expression, timezone, instruction, status, thread_strategy,
            scope, notify_enabled, notify_routes, notify_storage_mode,
            delivery_config, metadata, config_version, active_version,
            audit_history, created_by, updated_by, created_at, updated_at
        ) VALUES (
            %s::uuid, %s, 'Company book', 'company-recycle.json', 'desk', 'cron',
            '0 9 * * 1-5', 'UTC', 'Collect', 'active', 'new',
            'company', TRUE, '[]'::jsonb, 'task_row',
            '{}'::jsonb, '{}'::jsonb, 1, 1,
            '[]'::jsonb, %s, %s, NOW(), NOW()
        )
        """,
        (agent_id, COMPANY_SYSTEM_USER_ID, seed_user["user_id"], seed_user["user_id"]),
    )

    deleted = await delete_task(_principal(seed_user["user_id"]), agent_id, 1)
    assert deleted.deleted_by == seed_user["user_id"]
    assert deleted.deleted_at is not None

    with pytest.raises(HTTPException) as external:
        await delete_task(_principal("external-recycle-user"), agent_id, 2)
    assert external.value.status_code == 403


@pytest.mark.asyncio
async def test_data_task_delete_restore_and_purge_leaves_it(
    seed_user, patched_get_db_connection, monkeypatch
):
    from src.dalton.services.automation_recycle import (
        delete_task,
        purge_expired_automations_if_due,
        restore_task,
    )

    async def _allowed(user_id: str | None) -> bool:
        return str(user_id or "") == seed_user["user_id"]

    monkeypatch.setattr(
        "src.dalton.services.company_automations.is_internal_or_admin",
        _allowed,
    )

    async def _allow_read(principal):
        if principal.user_id != seed_user["user_id"]:
            raise HTTPException(status_code=403, detail="Forbidden")

    monkeypatch.setattr(
        "src.dalton.services.company_script_writes.require_company_scope_read_access",
        _allow_read,
    )
    monkeypatch.setattr(
        "src.dalton.services.company_task_read.require_company_scope_read_access",
        _allow_read,
    )

    await _execute(
        "DELETE FROM dalton_data_task_runs WHERE job_key = %s",
        (JOB_KEY,),
    )
    await _execute(
        "DELETE FROM dalton_data_task_defs WHERE job_key = %s",
        (JOB_KEY,),
    )
    await _execute(
        """
        INSERT INTO dalton_data_task_defs (
            job_key, task_code, title, batch_no, runner,
            cron_expr, timezone, command_argv, workdir,
            required_secrets, cos_mode, notify_enabled, enabled, paused_by_user,
            consecutive_failures, config_version, active_version, audit_history,
            updated_at
        ) VALUES (
            %s, 'BBG', 'BBG Asia', 1, 'container',
            '15 8 * * 1-5', 'Asia/Shanghai', '["echo"]'::jsonb, '/tmp',
            '[]'::jsonb, 'skip', FALSE, TRUE, FALSE,
            0, 1, 1, '[]'::jsonb, NOW()
        )
        """,
        (JOB_KEY,),
    )
    queued_id = str(uuid4())
    running_id = str(uuid4())
    for run_id, status, slot_offset in (
        (queued_id, "queued", "0 hours"),
        (running_id, "running", "2 hours"),
    ):
        await _execute(
            """
            INSERT INTO dalton_data_task_runs (
                run_id, job_key, trigger_source, scheduled_slot_utc,
                deadline_utc, status, created_at, updated_at
            ) VALUES (
                %s::uuid, %s, 'cron', NOW() + %s::interval,
                NOW() + INTERVAL '1 hour',
                %s, NOW(), NOW()
            )
            """,
            (run_id, JOB_KEY, slot_offset, status),
        )

    principal = _principal(seed_user["user_id"])
    deleted = await delete_task(principal, f"data:{JOB_KEY}", 1)
    assert deleted.enabled is False
    assert deleted.paused_by_user is True
    assert deleted.deleted_at is not None
    queued = await _fetch(
        "SELECT status, failure_reason FROM dalton_data_task_runs WHERE run_id = %s::uuid",
        (queued_id,),
    )
    running = await _fetch(
        "SELECT status FROM dalton_data_task_runs WHERE run_id = %s::uuid",
        (running_id,),
    )
    assert _cell(queued, "status", 0) == "cancelled"
    assert _cell(queued, "failure_reason", 1) == "task_deleted"
    assert _cell(running, "status", 0) == "running"

    await _execute(
        """
        UPDATE dalton_data_task_defs
        SET deleted_at = NOW() - INTERVAL '31 days'
        WHERE job_key = %s
        """,
        (JOB_KEY,),
    )
    await purge_expired_automations_if_due(datetime.now(UTC), force=True)
    still = await _fetch(
        "SELECT job_key FROM dalton_data_task_defs WHERE job_key = %s",
        (JOB_KEY,),
    )
    assert still is not None

    current = await _fetch(
        "SELECT config_version FROM dalton_data_task_defs WHERE job_key = %s",
        (JOB_KEY,),
    )
    restored = await restore_task(
        principal,
        f"data:{JOB_KEY}",
        int(_cell(current, "config_version", 0)),
    )
    assert restored.enabled is True
    assert restored.paused_by_user is False
    assert restored.deleted_at is None
    queued_after = await _fetch(
        "SELECT status FROM dalton_data_task_runs WHERE run_id = %s::uuid",
        (queued_id,),
    )
    assert _cell(queued_after, "status", 0) == "cancelled"

    await _execute("DELETE FROM dalton_data_task_runs WHERE job_key = %s", (JOB_KEY,))
    await _execute("DELETE FROM dalton_data_task_defs WHERE job_key = %s", (JOB_KEY,))


@pytest.mark.asyncio
async def test_purge_deletes_only_old_rows_and_one_session_holds_the_lock(
    seed_user, patched_get_db_connection, test_db_pool
):
    from src.dalton.database.automation_recycle import (
        PURGE_LOCK_NAME,
        PURGE_LOCK_NAMESPACE,
    )
    from src.dalton.services.automation_recycle import (
        delete_task,
        purge_expired_automations_if_due,
    )

    user_id = seed_user["user_id"]
    principal = _principal(user_id)
    old = await _create(user_id, name="Old deleted", file_name="old-deleted.json")
    recent = await _create(user_id, name="Recent deleted", file_name="recent-deleted.json")
    busy = await _create(user_id, name="Busy deleted", file_name="busy-deleted.json")
    await delete_task(principal, str(old["automation_id"]), 1)
    await delete_task(principal, str(recent["automation_id"]), 1)
    await delete_task(principal, str(busy["automation_id"]), 1)
    await _insert_execution(str(busy["automation_id"]), "running")
    await _execute(
        """
        UPDATE automations
        SET deleted_at = NOW() - INTERVAL '31 days'
        WHERE automation_id = ANY(%s::uuid[])
        """,
        ([str(old["automation_id"]), str(busy["automation_id"])],),
    )
    await _execute(
        """
        UPDATE automations
        SET deleted_at = NOW() - INTERVAL '1 day'
        WHERE automation_id = %s::uuid
        """,
        (str(recent["automation_id"]),),
    )

    async with test_db_pool.connection() as holder:
        async with holder.transaction():
            async with holder.cursor() as cur:
                await cur.execute(
                    """
                    SELECT pg_try_advisory_xact_lock(hashtext(%s), hashtext(%s)) AS locked
                    """,
                    (PURGE_LOCK_NAMESPACE, PURGE_LOCK_NAME),
                )
                held = await cur.fetchone()
            assert _cell(held, "locked", 0) is True
            blocked = await purge_expired_automations_if_due(datetime.now(UTC), force=True)
            assert blocked == 0
            still_old = await _fetch(
                "SELECT automation_id FROM automations WHERE automation_id = %s::uuid",
                (str(old["automation_id"]),),
            )
            assert still_old is not None

    removed = await purge_expired_automations_if_due(datetime.now(UTC), force=True)
    assert removed >= 1
    gone = await _fetch(
        "SELECT automation_id FROM automations WHERE automation_id = %s::uuid",
        (str(old["automation_id"]),),
    )
    assert gone is None
    kept_recent = await _fetch(
        "SELECT automation_id FROM automations WHERE automation_id = %s::uuid",
        (str(recent["automation_id"]),),
    )
    kept_busy = await _fetch(
        "SELECT automation_id FROM automations WHERE automation_id = %s::uuid",
        (str(busy["automation_id"]),),
    )
    assert kept_recent is not None
    assert kept_busy is not None
