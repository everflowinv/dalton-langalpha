"""Real PostgreSQL coverage for company agent update, pause, and resume.

Auth tiers are a narrow predicate. Routes, services, and the database stay
real. The pool is the AU1 foundation pool, forwarded onto this test database.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
import json
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import pytest
import pytest_asyncio

from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.database import company_automations as company_auto_db
from src.dalton.services import company_automations as company_auto_svc
from src.server.app import automations as automations_app
from src.server.dependencies import principal as principal_mod
from src.server.services.automations import lifecycle
from tests.integration.dalton.test_au1_company_foundation_db import (
    _insert_user,
    _insert_workspace,
    au1_live_pool as _foundation_au1_live_pool,
)

pytestmark = pytest.mark.integration

INTERNAL_A = "u-co-write-a"
INTERNAL_B = "u-co-write-b"
ADMIN = "u-co-write-admin"
EXTERNAL = "u-co-write-external"
OTHER = "u-co-write-other"
SEED_ACTOR = "seed-keeper"
ROUTES = ["feishu:chat:oc_company"]
SECRET = "TOP_SECRET"
LEGACY_OWNER = "legacy-owner"
ONCE_AT = datetime(2030, 6, 15, 10, 0, tzinfo=UTC)
CRON_AT = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
ALLOWED_TIER = frozenset({INTERNAL_A, INTERNAL_B, ADMIN})

CRON_ID = "c1111111-1111-4111-8111-111111111111"
ONCE_ID = "c2222222-2222-4222-8222-222222222222"
REFUSE_ID = "c3333333-3333-4333-8333-333333333333"
RACE_ID = "c4444444-4444-4444-8444-444444444444"
FREEZE_ID = "c5555555-5555-4555-8555-555555555555"
DELETE_ID = "c6666666-6666-4666-8666-666666666666"
TRANSFER_ID = "c7777777-7777-4777-8777-777777777777"
BOOM_ID = "c8888888-8888-4888-8888-888888888888"
META_ID = "c9999999-9999-4999-8999-999999999999"
TARGET_ID = "caaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PRIVATE_ID = "cbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


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


def _json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _instant(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _metadata() -> dict[str, Any]:
    return {
        "label": "desk",
        "note": "ordinary",
        "pre_company_migration": {"user_id": LEGACY_OWNER},
        "company_migration": {"stage": "running", "batch_id": "batch-9"},
        "company_migration_batches": [{"batch_id": "batch-9"}],
    }


def _principal(user_id: str, *, computer_id: str | None = None) -> principal_mod.Principal:
    if computer_id is not None:
        return principal_mod.Principal(
            user_id=user_id, auth="computer", computer_id=computer_id
        )
    return principal_mod.Principal(user_id=user_id, auth="session")


def _app(holder: dict[str, principal_mod.Principal]) -> FastAPI:
    app = FastAPI()
    app.include_router(automations_app.router)
    app.dependency_overrides[principal_mod.get_principal] = lambda: holder["principal"]
    return app


@pytest.fixture
def internal_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _allowed(user_id: str | None) -> bool:
        return str(user_id or "") in ALLOWED_TIER

    monkeypatch.setattr(company_auto_svc, "is_internal_or_admin", _allowed)


async def _users(conn: psycopg.AsyncConnection) -> None:
    for user_id, email in (
        (INTERNAL_A, "co-write-a@dalton.test"),
        (INTERNAL_B, "co-write-b@dalton.test"),
        (ADMIN, "co-write-admin@dalton.test"),
        (EXTERNAL, "co-write-external@dalton.test"),
        (OTHER, "co-write-other@dalton.test"),
    ):
        await _insert_user(conn, user_id, email)


async def _insert_agent(
    conn: psycopg.AsyncConnection,
    *,
    automation_id: str,
    name: str,
    file_name: str,
    workspace_id: str,
    trigger_type: str = "cron",
    description: str = "start",
    status: str = "active",
    cron_expression: str | None = "0 9 * * 1-5",
    next_run_at: datetime | None = CRON_AT,
    user_id: str = COMPANY_SYSTEM_USER_ID,
    scope: str = "company",
    notify_storage_mode: str = "task_row",
    metadata: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO automations (
            automation_id, user_id, name, file_name, description, trigger_type,
            cron_expression, timezone, next_run_at, instruction, workspace_id,
            status, thread_strategy, scope, notify_enabled, notify_routes,
            notify_storage_mode, delivery_config, metadata,
            config_version, active_version, audit_history, created_by, updated_by
        )
        VALUES (
            %s::uuid, %s, %s, %s, %s, %s,
            %s, 'Asia/Shanghai', %s, 'Collect the company book', %s::uuid,
            %s, 'new', %s, TRUE, %s::jsonb,
            %s, %s::jsonb, %s::jsonb,
            1, 1, '[]'::jsonb, %s, %s
        )
        """,
        (
            automation_id,
            user_id,
            name,
            file_name,
            description,
            trigger_type,
            cron_expression,
            next_run_at,
            workspace_id,
            status,
            scope,
            json.dumps(ROUTES),
            notify_storage_mode,
            json.dumps({"methods": [], "webhook_secret": SECRET}),
            json.dumps(metadata if metadata is not None else _metadata()),
            SEED_ACTOR,
            SEED_ACTOR,
        ),
    )


async def _insert_thread(
    conn: psycopg.AsyncConnection, thread_id: str, workspace_id: str
) -> None:
    await conn.execute(
        """
        INSERT INTO conversation_threads (
            conversation_thread_id, workspace_id, current_status, thread_index
        )
        VALUES (%s::uuid, %s::uuid, 'completed', 1)
        """,
        (thread_id, workspace_id),
    )


async def _read_row(pool: AsyncConnectionPool, automation_id: str) -> dict[str, Any]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT automation_id::text AS automation_id, user_id, scope, description,
                       status, trigger_type, cron_expression, timezone, next_run_at,
                       workspace_id::text AS workspace_id,
                       conversation_thread_id::text AS conversation_thread_id,
                       notify_enabled, notify_routes, delivery_config, notify_storage_mode,
                       metadata, config_version, active_version, created_by, updated_by,
                       audit_history, deleted_at
                FROM automations
                WHERE automation_id = %s::uuid
                """,
                (automation_id,),
            )
            row = await cur.fetchone()
    assert row is not None
    row["notify_routes"] = _json(row["notify_routes"])
    row["delivery_config"] = _json(row["delivery_config"])
    row["metadata"] = _json(row["metadata"])
    row["audit_history"] = _json(row["audit_history"])
    return row


def _assert_public(body: dict[str, Any]) -> None:
    text = json.dumps(body)
    assert SECRET not in text
    assert LEGACY_OWNER not in text
    assert "pre_company_migration" not in text
    assert "company_migration_batches" not in text
    assert "batch-9" not in text
    assert body["user_id"] == COMPANY_SYSTEM_USER_ID
    assert body["scope"] == "company"
    assert body["notify_storage_mode"] == "task_row"
    assert body["dalton_notify_source"] == "task_row"
    assert body["dalton_notify_effective"] is True
    assert body["dalton_notify_route_targets"] == ROUTES
    assert "notify_enabled" not in body
    assert "notify_routes" not in body
    assert body["created_by"] == SEED_ACTOR


def _assert_stored_fixed(row: dict[str, Any], *, description: str | None = None) -> None:
    assert row["user_id"] == COMPANY_SYSTEM_USER_ID
    assert row["scope"] == "company"
    assert row["notify_storage_mode"] == "task_row"
    assert row["notify_enabled"] is True
    assert row["notify_routes"] == ROUTES
    assert row["delivery_config"]["webhook_secret"] == SECRET
    assert row["delivery_config"]["methods"] == []
    assert row["metadata"]["pre_company_migration"] == {"user_id": LEGACY_OWNER}
    assert row["metadata"]["company_migration"]["stage"] == "running"
    assert row["metadata"]["company_migration_batches"] == [{"batch_id": "batch-9"}]
    if description is not None:
        assert row["description"] == description


async def _history(client: AsyncClient, automation_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/automations/{automation_id}/history")
    assert response.status_code == 200, response.text
    body = response.json()
    text = json.dumps(body)
    assert SECRET not in text
    assert LEGACY_OWNER not in text
    assert "pre_company_migration" not in text
    return body


@pytest.mark.asyncio
async def test_company_patch_pause_resume_keeps_owner_notify_and_schedule(
    au1_live_pool: AsyncConnectionPool,
    internal_tier: None,
) -> None:
    ws_id = str(uuid4())
    holder = {"principal": _principal(INTERNAL_A)}
    async with au1_live_pool.connection() as conn:
        await _users(conn)
        await _insert_workspace(conn, ws_id, COMPANY_SYSTEM_USER_ID, "Company Book")
        await _insert_agent(
            conn,
            automation_id=CRON_ID,
            name="Company Cron",
            file_name="company-cron.json",
            workspace_id=ws_id,
            description="cron-start",
        )
        await _insert_agent(
            conn,
            automation_id=ONCE_ID,
            name="Company Once",
            file_name="company-once.json",
            workspace_id=ws_id,
            trigger_type="once",
            cron_expression=None,
            next_run_at=ONCE_AT,
            description="once-start",
        )

    app = _app(holder)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for automation_id, description in (
            (CRON_ID, "cron-edited"),
            (ONCE_ID, "once-edited"),
        ):
            patched = await client.patch(
                f"/api/v1/automations/{automation_id}",
                json={"description": description, "expected_config_version": 1},
            )
            assert patched.status_code == 200, patched.text
            body = patched.json()
            _assert_public(body)
            assert body["description"] == description
            assert body["config_version"] == 2
            assert body["active_version"] == 2
            assert body["updated_by"] == INTERNAL_A
            assert body["status"] == "active"

        holder["principal"] = _principal(INTERNAL_B)
        for automation_id in (CRON_ID, ONCE_ID):
            paused = await client.post(
                f"/api/v1/automations/{automation_id}/pause",
                json={"expected_config_version": 2},
            )
            assert paused.status_code == 200, paused.text
            body = paused.json()
            _assert_public(body)
            assert body["status"] == "paused"
            assert body["config_version"] == 3
            assert body["updated_by"] == INTERNAL_B

        cron_paused = await _read_row(au1_live_pool, CRON_ID)
        once_paused = await _read_row(au1_live_pool, ONCE_ID)
        assert cron_paused["next_run_at"] is None
        assert cron_paused["cron_expression"] == "0 9 * * 1-5"
        assert _instant(once_paused["next_run_at"]) == ONCE_AT
        _assert_stored_fixed(cron_paused, description="cron-edited")
        _assert_stored_fixed(once_paused, description="once-edited")

        holder["principal"] = _principal(ADMIN)
        for automation_id in (CRON_ID, ONCE_ID):
            resumed = await client.post(
                f"/api/v1/automations/{automation_id}/resume",
                json={"expected_config_version": 3},
            )
            assert resumed.status_code == 200, resumed.text
            body = resumed.json()
            _assert_public(body)
            assert body["status"] == "active"
            assert body["config_version"] == 4
            assert body["active_version"] == 4
            assert body["updated_by"] == ADMIN

        holder["principal"] = _principal(INTERNAL_A)
        for automation_id, actors in (
            (CRON_ID, [ADMIN, INTERNAL_B, INTERNAL_A, SEED_ACTOR]),
            (ONCE_ID, [ADMIN, INTERNAL_B, INTERNAL_A, SEED_ACTOR]),
        ):
            hist = await _history(client, automation_id)
            assert hist["config_version"] == 4
            assert hist["active_version"] == 4
            assert hist["total"] == 4
            assert [item["action"] for item in hist["entries"]] == [
                "resume",
                "pause",
                "update",
                "seed",
            ]
            assert [item["actor_user_id"] for item in hist["entries"]] == actors
            assert [item["version"] for item in hist["entries"]] == [4, 3, 2, 1]

    cron = await _read_row(au1_live_pool, CRON_ID)
    once = await _read_row(au1_live_pool, ONCE_ID)
    assert cron["next_run_at"] is not None
    assert cron["cron_expression"] == "0 9 * * 1-5"
    assert cron["timezone"] == "Asia/Shanghai"
    assert _instant(once["next_run_at"]) == ONCE_AT
    assert once["cron_expression"] is None
    for row, description in ((cron, "cron-edited"), (once, "once-edited")):
        _assert_stored_fixed(row, description=description)
        assert row["status"] == "active"
        assert row["config_version"] == 4
        assert row["active_version"] == 4
        assert row["updated_by"] == ADMIN
        assert [item["action"] for item in row["audit_history"]] == [
            "seed",
            "update",
            "pause",
            "resume",
        ]
        assert [item["actor_user_id"] for item in row["audit_history"]] == [
            SEED_ACTOR,
            INTERNAL_A,
            INTERNAL_B,
            ADMIN,
        ]


@pytest.mark.asyncio
async def test_company_write_rejects_bad_input_and_foreign_callers_without_mutation(
    au1_live_pool: AsyncConnectionPool,
    internal_tier: None,
) -> None:
    ws_id = str(uuid4())
    holder = {"principal": _principal(INTERNAL_A)}
    async with au1_live_pool.connection() as conn:
        await _users(conn)
        await _insert_workspace(conn, ws_id, COMPANY_SYSTEM_USER_ID, "Refuse WS")
        await _insert_agent(
            conn,
            automation_id=REFUSE_ID,
            name="Company Refuse",
            file_name="company-refuse.json",
            workspace_id=ws_id,
            description="refuse-start",
        )
        await _insert_agent(
            conn,
            automation_id=PRIVATE_ID,
            name="Private Other",
            file_name="private-other.json",
            workspace_id=ws_id,
            user_id=OTHER,
            scope="personal",
            notify_storage_mode="legacy",
            description="private-start",
        )

    before = await _read_row(au1_live_pool, REFUSE_ID)
    private_before = await _read_row(au1_live_pool, PRIVATE_ID)
    app = _app(holder)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.patch(
            f"/api/v1/automations/{REFUSE_ID}",
            json={"description": "missing-version"},
        )
        assert missing.status_code == 400, missing.text
        assert "expected_config_version" in missing.text

        for raw in (0, -1, "1", True, 1.5):
            invalid = await client.patch(
                f"/api/v1/automations/{REFUSE_ID}",
                json={"description": "bad-version", "expected_config_version": raw},
            )
            assert invalid.status_code == 400, invalid.text
            paused = await client.post(
                f"/api/v1/automations/{REFUSE_ID}/pause",
                json={"expected_config_version": raw},
            )
            assert paused.status_code == 400, paused.text

        omitted_pause = await client.post(
            f"/api/v1/automations/{REFUSE_ID}/pause",
            json={},
        )
        assert omitted_pause.status_code == 400, omitted_pause.text

        for raw_delivery in (None, {}):
            forged = await client.patch(
                f"/api/v1/automations/{REFUSE_ID}",
                json={
                    "description": "forged-delivery",
                    "expected_config_version": 1,
                    "delivery_config": {
                        "methods": ["webhook"],
                        "webhook_url": "https://evil.example/hook",
                    },
                    "delivery_config_raw": raw_delivery,
                },
            )
            assert forged.status_code == 400, forged.text
            assert "delivery_config" in forged.text

        protected = await client.patch(
            f"/api/v1/automations/{REFUSE_ID}",
            json={
                "expected_config_version": 1,
                "metadata": {
                    "label": "forged",
                    "company_migration": {"stage": "done"},
                    "pre_company_migration": {"user_id": "forged"},
                },
            },
        )
        assert protected.status_code == 400, protected.text
        assert "migration" in protected.text

        stale = await client.patch(
            f"/api/v1/automations/{REFUSE_ID}",
            json={"description": "stale", "expected_config_version": 2},
        )
        assert stale.status_code == 409, stale.text

        holder["principal"] = _principal(EXTERNAL)
        external = await client.patch(
            f"/api/v1/automations/{REFUSE_ID}",
            json={"description": "external", "expected_config_version": 1},
        )
        assert external.status_code == 403, external.text

        holder["principal"] = _principal(INTERNAL_A, computer_id=str(uuid4()))
        computer = await client.patch(
            f"/api/v1/automations/{REFUSE_ID}",
            json={"description": "computer", "expected_config_version": 1},
        )
        assert computer.status_code == 403, computer.text

        holder["principal"] = _principal(COMPANY_SYSTEM_USER_ID)
        system = await client.patch(
            f"/api/v1/automations/{REFUSE_ID}",
            json={"description": "system", "expected_config_version": 1},
        )
        assert system.status_code == 401, system.text
        assert "Company system identity cannot authenticate externally" in system.text

        holder["principal"] = _principal(INTERNAL_A)
        private = await client.patch(
            f"/api/v1/automations/{PRIVATE_ID}",
            json={"description": "stolen", "expected_config_version": 1},
        )
        assert private.status_code == 404, private.text
        missing_row = await client.patch(
            f"/api/v1/automations/{uuid4()}",
            json={"description": "missing", "expected_config_version": 1},
        )
        assert missing_row.status_code == 404, missing_row.text

        hist = await _history(client, REFUSE_ID)
        assert hist["entries"] == []
        assert hist["config_version"] == 1

    after = await _read_row(au1_live_pool, REFUSE_ID)
    private_after = await _read_row(au1_live_pool, PRIVATE_ID)
    assert after == before
    assert private_after["description"] == private_before["description"]
    assert private_after["config_version"] == 1
    assert private_after["audit_history"] == []


@pytest.mark.asyncio
async def test_company_concurrent_same_version_commits_exactly_one(
    au1_live_pool: AsyncConnectionPool,
    internal_tier: None,
) -> None:
    ws_id = str(uuid4())
    holder = {"principal": _principal(INTERNAL_A)}
    async with au1_live_pool.connection() as conn:
        await _users(conn)
        await _insert_workspace(conn, ws_id, COMPANY_SYSTEM_USER_ID, "Race WS")
        await _insert_agent(
            conn,
            automation_id=RACE_ID,
            name="Company Race",
            file_name="company-race.json",
            workspace_id=ws_id,
            description="race-start",
        )

    app = _app(holder)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        left, right = await asyncio.gather(
            client.patch(
                f"/api/v1/automations/{RACE_ID}",
                json={"description": "race-left", "expected_config_version": 1},
            ),
            client.patch(
                f"/api/v1/automations/{RACE_ID}",
                json={"description": "race-right", "expected_config_version": 1},
            ),
        )
    statuses = sorted(item.status_code for item in (left, right))
    assert statuses == [200, 409], (left.text, right.text)
    winner = left if left.status_code == 200 else right
    assert winner.json()["description"] in {"race-left", "race-right"}
    assert winner.json()["config_version"] == 2
    assert winner.json()["updated_by"] == INTERNAL_A
    _assert_public(winner.json())

    row = await _read_row(au1_live_pool, RACE_ID)
    assert row["description"] == winner.json()["description"]
    assert row["config_version"] == 2
    assert row["active_version"] == 2
    assert row["updated_by"] == INTERNAL_A
    assert [item["action"] for item in row["audit_history"]] == ["seed", "update"]
    assert row["audit_history"][1]["actor_user_id"] == INTERNAL_A
    _assert_stored_fixed(row, description=row["description"])


async def _change_before_lock(
    monkeypatch: pytest.MonkeyPatch,
    pool: AsyncConnectionPool,
    client: AsyncClient,
    automation_id: str,
    statement: str,
    params: tuple[Any, ...],
) -> Response:
    """Commit a row change after prepare's read and before the writer locks."""
    started = asyncio.Event()
    release = asyncio.Event()
    real_prepare = lifecycle.prepare_owned_automation_update
    task: asyncio.Task[Response] | None = None

    async def _wrap(*args: Any, **kwargs: Any) -> Any:
        prepared = await real_prepare(*args, **kwargs)
        started.set()
        await release.wait()
        return prepared

    monkeypatch.setattr(lifecycle, "prepare_owned_automation_update", _wrap)
    try:
        async with pool.connection() as hold:
            async with hold.transaction():
                await hold.execute(
                    """
                    SELECT automation_id
                    FROM automations
                    WHERE automation_id = %s::uuid
                    FOR UPDATE
                    """,
                    (automation_id,),
                )
                await hold.execute(statement, params)
                task = asyncio.create_task(
                    client.patch(
                        f"/api/v1/automations/{automation_id}",
                        json={
                            "description": "writer-must-not-land",
                            "expected_config_version": 1,
                        },
                    )
                )
                await asyncio.wait_for(started.wait(), timeout=20)
        release.set()
        assert task is not None
        return await task
    finally:
        release.set()
        monkeypatch.setattr(lifecycle, "prepare_owned_automation_update", real_prepare)
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_company_lock_time_change_and_history_failure_roll_back(
    au1_live_pool: AsyncConnectionPool,
    internal_tier: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = str(uuid4())
    holder = {"principal": _principal(INTERNAL_A)}
    async with au1_live_pool.connection() as conn:
        await _users(conn)
        await _insert_workspace(conn, ws_id, COMPANY_SYSTEM_USER_ID, "Lock WS")
        for automation_id, file_name in (
            (FREEZE_ID, "company-freeze.json"),
            (DELETE_ID, "company-delete.json"),
            (TRANSFER_ID, "company-transfer.json"),
            (BOOM_ID, "company-boom.json"),
        ):
            await _insert_agent(
                conn,
                automation_id=automation_id,
                name="Company Lock",
                file_name=file_name,
                workspace_id=ws_id,
                description="lock-start",
            )

    app = _app(holder)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        frozen = await _change_before_lock(
            monkeypatch,
            au1_live_pool,
            client,
            FREEZE_ID,
            """
            UPDATE automations
            SET metadata = jsonb_set(
                metadata, '{company_migration,stage}', '"frozen"'::jsonb, true
            )
            WHERE automation_id = %s::uuid
            """,
            (FREEZE_ID,),
        )
        assert frozen.status_code == 409, frozen.text
        assert "migration-frozen" in frozen.text

        deleted = await _change_before_lock(
            monkeypatch,
            au1_live_pool,
            client,
            DELETE_ID,
            "UPDATE automations SET deleted_at = NOW() WHERE automation_id = %s::uuid",
            (DELETE_ID,),
        )
        assert deleted.status_code == 404, deleted.text

        transferred = await _change_before_lock(
            monkeypatch,
            au1_live_pool,
            client,
            TRANSFER_ID,
            """
            UPDATE automations
            SET scope = 'personal', user_id = %s
            WHERE automation_id = %s::uuid
            """,
            (OTHER, TRANSFER_ID),
        )
        assert transferred.status_code == 404, transferred.text

        real_history = company_auto_db.prepare_update_history_columns

        async def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Simulated history snapshot failure")

        monkeypatch.setattr(company_auto_db, "prepare_update_history_columns", _boom)
        failed = await client.patch(
            f"/api/v1/automations/{BOOM_ID}",
            json={"description": "history-must-not-land", "expected_config_version": 1},
        )
        assert failed.status_code == 500, failed.text
        monkeypatch.setattr(
            company_auto_db, "prepare_update_history_columns", real_history
        )

    freeze_row = await _read_row(au1_live_pool, FREEZE_ID)
    delete_row = await _read_row(au1_live_pool, DELETE_ID)
    transfer_row = await _read_row(au1_live_pool, TRANSFER_ID)
    boom_row = await _read_row(au1_live_pool, BOOM_ID)
    assert freeze_row["description"] == "lock-start"
    assert freeze_row["config_version"] == 1
    assert freeze_row["audit_history"] == []
    assert freeze_row["metadata"]["company_migration"]["stage"] == "frozen"
    assert freeze_row["metadata"]["pre_company_migration"] == {"user_id": LEGACY_OWNER}
    assert delete_row["description"] == "lock-start"
    assert delete_row["config_version"] == 1
    assert delete_row["audit_history"] == []
    assert delete_row["deleted_at"] is not None
    assert transfer_row["description"] == "lock-start"
    assert transfer_row["config_version"] == 1
    assert transfer_row["audit_history"] == []
    assert transfer_row["user_id"] == OTHER
    assert transfer_row["scope"] == "personal"
    assert boom_row["description"] == "lock-start"
    assert boom_row["config_version"] == 1
    assert boom_row["active_version"] == 1
    assert boom_row["audit_history"] == []
    assert boom_row["updated_by"] == SEED_ACTOR


@pytest.mark.asyncio
async def test_company_metadata_replace_preserves_migration_and_company_targets(
    au1_live_pool: AsyncConnectionPool,
    internal_tier: None,
) -> None:
    home_ws = str(uuid4())
    other_ws = str(uuid4())
    human_ws = str(uuid4())
    home_thread = str(uuid4())
    other_thread = str(uuid4())
    human_thread = str(uuid4())
    missing_ws = str(uuid4())
    missing_thread = str(uuid4())
    holder = {"principal": _principal(INTERNAL_A)}
    async with au1_live_pool.connection() as conn:
        await _users(conn)
        await _insert_workspace(conn, home_ws, COMPANY_SYSTEM_USER_ID, "Company Home")
        await _insert_workspace(conn, other_ws, COMPANY_SYSTEM_USER_ID, "Company Other")
        await _insert_workspace(conn, human_ws, INTERNAL_A, "Human WS")
        await _insert_thread(conn, home_thread, home_ws)
        await _insert_thread(conn, other_thread, other_ws)
        await _insert_thread(conn, human_thread, human_ws)
        await _insert_agent(
            conn,
            automation_id=META_ID,
            name="Company Meta",
            file_name="company-meta.json",
            workspace_id=home_ws,
            description="meta-start",
        )
        await _insert_agent(
            conn,
            automation_id=TARGET_ID,
            name="Company Target",
            file_name="company-target.json",
            workspace_id=home_ws,
            description="target-start",
        )

    app = _app(holder)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        replaced = await client.patch(
            f"/api/v1/automations/{META_ID}",
            json={
                "expected_config_version": 1,
                "metadata": {"label": "replaced"},
            },
        )
        assert replaced.status_code == 200, replaced.text
        meta_body = replaced.json()
        _assert_public(meta_body)
        assert meta_body["metadata"] == {"label": "replaced"}
        assert meta_body["config_version"] == 2

        foreign_ws = await client.patch(
            f"/api/v1/automations/{TARGET_ID}",
            json={"expected_config_version": 1, "workspace_id": human_ws},
        )
        assert foreign_ws.status_code == 403, foreign_ws.text
        absent_ws = await client.patch(
            f"/api/v1/automations/{TARGET_ID}",
            json={"expected_config_version": 1, "workspace_id": missing_ws},
        )
        assert absent_ws.status_code == 404, absent_ws.text
        assert "Workspace not found" in absent_ws.text
        foreign_thread = await client.patch(
            f"/api/v1/automations/{TARGET_ID}",
            json={
                "expected_config_version": 1,
                "conversation_thread_id": human_thread,
            },
        )
        assert foreign_thread.status_code == 403, foreign_thread.text
        absent_thread = await client.patch(
            f"/api/v1/automations/{TARGET_ID}",
            json={
                "expected_config_version": 1,
                "conversation_thread_id": missing_thread,
            },
        )
        assert absent_thread.status_code == 404, absent_thread.text
        assert "Thread not found" in absent_thread.text
        misplaced = await client.patch(
            f"/api/v1/automations/{TARGET_ID}",
            json={
                "expected_config_version": 1,
                "conversation_thread_id": other_thread,
            },
        )
        assert misplaced.status_code == 409, misplaced.text

        moved = await client.patch(
            f"/api/v1/automations/{TARGET_ID}",
            json={"expected_config_version": 1, "workspace_id": other_ws},
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["workspace_id"] == other_ws
        assert moved.json()["user_id"] == COMPANY_SYSTEM_USER_ID
        assert moved.json()["config_version"] == 2
        _assert_public(moved.json())

        pinned = await client.patch(
            f"/api/v1/automations/{TARGET_ID}",
            json={
                "expected_config_version": 2,
                "conversation_thread_id": other_thread,
            },
        )
        assert pinned.status_code == 200, pinned.text
        assert pinned.json()["conversation_thread_id"] == other_thread
        assert pinned.json()["workspace_id"] == other_ws
        assert pinned.json()["user_id"] == COMPANY_SYSTEM_USER_ID
        assert pinned.json()["scope"] == "company"
        assert pinned.json()["config_version"] == 3

    meta_row = await _read_row(au1_live_pool, META_ID)
    target_row = await _read_row(au1_live_pool, TARGET_ID)
    assert meta_row["metadata"] == {
        "label": "replaced",
        "pre_company_migration": {"user_id": LEGACY_OWNER},
        "company_migration": {"stage": "running", "batch_id": "batch-9"},
        "company_migration_batches": [{"batch_id": "batch-9"}],
    }
    assert "note" not in meta_row["metadata"]
    assert meta_row["config_version"] == 2
    _assert_stored_fixed(meta_row, description="meta-start")
    assert target_row["workspace_id"] == other_ws
    assert target_row["conversation_thread_id"] == other_thread
    assert target_row["user_id"] == COMPANY_SYSTEM_USER_ID
    assert target_row["scope"] == "company"
    assert target_row["config_version"] == 3
    assert target_row["description"] == "target-start"
    assert target_row["metadata"]["company_migration"]["stage"] == "running"
    assert target_row["delivery_config"]["webhook_secret"] == SECRET
