"""Unit tests for the unified company read API.

Database failures and permission gates are faked here. The PostgreSQL cases
live in tests/integration/dalton/test_au1_unified_read_api_db.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import create_autospec

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
import pytest

from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.database import company_data_tasks as company_data_db
from src.dalton.data_jobs.container_runner import LOG_TAIL_MAX_BYTES
from src.dalton.services import company_automations as company_auto_svc
from src.dalton.services import company_task_read
from src.server.app import automations as automations_app
from src.server.database import automation as auto_db
from src.server.dependencies.principal import Principal, get_principal
from src.server.models.automation import (
    AutomationRunsListResponse,
    AutomationsListResponse,
)

INTERNAL = "user-internal-001"
EXTERNAL = "user-external-001"
COMPUTER = "11111111-1111-4111-8111-111111111111"
TIE = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
AGENT_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"
SCRIPT_EARLY = "data:ah-premium-am"
SCRIPT_LATE = "data:ah-premium-pm"


def _app(principal: Principal) -> FastAPI:
    app = FastAPI()
    app.include_router(automations_app.router)
    app.dependency_overrides[get_principal] = lambda: principal
    return app


def _session(user_id: str) -> Principal:
    return Principal(user_id=user_id, auth="session")


def _computer(user_id: str) -> Principal:
    return Principal(
        user_id=user_id,
        auth="computer",
        scopes=("agent:read",),
        computer_id=COMPUTER,
    )


def _allow_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        company_auto_svc,
        "is_internal_or_admin",
        create_autospec(
            company_auto_svc.is_internal_or_admin,
            side_effect=lambda uid: uid == INTERNAL,
        ),
    )


def _agent_row(*, updated_at: datetime, status: str = "active") -> dict[str, Any]:
    return {
        "automation_id": AGENT_ID,
        "user_id": COMPANY_SYSTEM_USER_ID,
        "name": "Company Catalog Agent",
        "file_name": "company.json",
        "description": "kept for the owner model",
        "trigger_type": "cron",
        "cron_expression": "0 9 * * *",
        "timezone": "Asia/Shanghai",
        "trigger_config": {},
        "next_run_at": None,
        "last_run_at": None,
        "instruction": "Collect",
        "workspace_id": "55555555-5555-4555-8555-555555555555",
        "llm_model": None,
        "additional_context": None,
        "thread_strategy": "new",
        "conversation_thread_id": None,
        "status": status,
        "max_failures": 3,
        "failure_count": 0,
        "disable_reason": None,
        "delivery_config": {"methods": ["slack"], "webhook_secret": "hook-secret"},
        "metadata": {
            "note": "visible-note",
            "api_key": "sekret-key",
            "pre_company_migration": {"user_id": INTERNAL},
        },
        "scope": "company",
        "notify_enabled": False,
        "notify_routes": ["feishu:chat:oc_company"],
        "notify_storage_mode": "task_row",
        "config_version": 2,
        "active_version": 2,
        "created_by": INTERNAL,
        "updated_by": INTERNAL,
        "deleted_at": None,
        "created_at": TIE,
        "updated_at": updated_at,
        "last_execution": None,
        "command_argv": ["echo"],
        "migration_state": {"legacy_notify_owner_user_id": INTERNAL},
    }


def _def_row(
    job_key: str,
    *,
    updated_at: datetime,
    enabled: bool = True,
    paused_by_user: bool = False,
    title: str = "DB title",
) -> dict[str, Any]:
    return {
        "job_key": job_key,
        "title": title,
        "cron_expr": "15 8 * * 1-5",
        "timezone": "Asia/Hong_Kong",
        "enabled": enabled,
        "paused_by_user": paused_by_user,
        "notify_enabled": True,
        "notify_routes": ["feishu:chat:oc_row"],
        "notify_storage_mode": "task_row",
        "cos_mode": "skip",
        "config_version": 4,
        "active_version": 4,
        "updated_by": INTERNAL,
        "updated_at": updated_at,
        "migration_state": {"legacy_notify_owner_user_id": "should-not-leak"},
        "deleted_at": None,
        "command_argv": ["echo", "secret"],
        "workdir": "/tmp/secret",
        "required_secrets": ["PLANTED_SECRET_VALUE"],
    }


def test_parse_automation_read_id_accepts_uuid_and_canonical_data_id() -> None:
    agent = company_task_read.parse_automation_read_id(
        "FFFFFFFF-FFFF-4FFF-8FFF-FFFFFFFFFFFF"
    )
    script = company_task_read.parse_automation_read_id("data:bbg-data-pull-asia")

    assert agent.kind == "agent"
    assert agent.agent_id == AGENT_ID
    assert script.kind == "script"
    assert script.job_key == "bbg-data-pull-asia"
    for raw in ("not-a-uuid", "data:", "DATA:bbg-data-pull-asia", "data:Bad", "data:a_b"):
        with pytest.raises(company_task_read.InvalidAutomationId):
            company_task_read.parse_automation_read_id(raw)


def test_shared_company_agent_requires_scope_and_company_owner() -> None:
    assert company_task_read.is_shared_company_agent(
        {"scope": "company", "user_id": COMPANY_SYSTEM_USER_ID}
    )
    assert company_task_read.is_shared_company_agent(
        {"scope": " company ", "user_id": " SyStEm:DaLtOn-CoMpAnY "}
    )
    assert (
        company_task_read.is_shared_company_agent(
            {"scope": "company", "user_id": INTERNAL}
        )
        is False
    )
    assert (
        company_task_read.is_shared_company_agent(
            {"scope": "personal", "user_id": COMPANY_SYSTEM_USER_ID}
        )
        is False
    )
    assert company_task_read.is_shared_company_agent(None) is False


def test_script_status_uses_flags_not_run_status() -> None:
    assert (
        company_task_read.script_display_status(enabled=True, paused_by_user=True)
        == "paused"
    )
    assert (
        company_task_read.script_display_status(enabled=True, paused_by_user=False)
        == "active"
    )
    assert (
        company_task_read.script_display_status(enabled=False, paused_by_user=False)
        == "disabled"
    )
    assert (
        company_task_read.script_matches_list_status(
            "succeeded", enabled=True, paused_by_user=False
        )
        is False
    )
    assert (
        company_task_read.script_matches_list_status(
            "active", enabled=True, paused_by_user=False
        )
        is True
    )


def test_adapter_uses_definition_row_and_keeps_script_run_status() -> None:
    run = {
        "run_id": "11111111-1111-4111-8111-111111111111",
        "status": "succeeded",
        "scheduled_slot_utc": TIE,
        "started_at": TIE,
        "completed_at": TIE + timedelta(minutes=5),
        "created_at": TIE,
        "exit_code": 0,
        "failure_reason": None,
        "notify_status": "dispatched",
        "triggered_by_user_id": INTERNAL,
        "log_tail": ("A" * 20_000) + "ENDMARKER",
        "trigger_source": "cron",
        "workspace_id": "55555555-5555-4555-8555-555555555555",
        "command_argv": ["echo"],
    }
    paused = _adapt(
        _def_row("bbg-data-pull-asia", updated_at=TIE, paused_by_user=True),
        latest_run=run,
    )
    active = _adapt(_def_row("bbg-data-pull-asia", updated_at=TIE), latest_run=run)

    assert paused.status == "paused"
    assert paused.next_run_at is None
    assert active.status == "active"
    assert active.next_run_at is not None
    body = active.model_dump(mode="json")
    assert body["automation_id"] == "data:bbg-data-pull-asia"
    assert body["engine_kind"] == "script"
    assert body["scope"] == "company"
    assert body["name"] == "DB title"
    assert body["description"] is None
    assert body["cron_expression"] == "15 8 * * 1-5"
    assert body["enabled"] is True
    assert body["last_execution"]["status"] == "succeeded"
    assert body["last_execution"]["conversation_thread_id"] is None
    assert body["last_execution"]["triggered_by_user_id"] == INTERNAL
    assert body["last_execution"]["log_tail"].endswith("ENDMARKER")
    assert len(body["last_execution"]["log_tail"].encode()) <= LOG_TAIL_MAX_BYTES
    for absent in (
        "user_id",
        "instruction",
        "workspace_id",
        "command_argv",
        "workdir",
        "required_secrets",
        "migration_state",
        "llm_model",
    ):
        assert absent not in body
        assert absent not in body["last_execution"]
    assert "created_at" not in body
    assert active.last_execution.created_at == run["created_at"]
    assert "PLANTED_SECRET_VALUE" not in str(body)

    with pytest.raises(company_task_read.CompanyDataTaskReadError):
        _adapt(
            _def_row("bbg-data-pull-asia", updated_at=TIE),
            latest_run={"status": "succeeded"},
        )


def _adapt(row: dict[str, Any], *, latest_run: dict[str, Any] | None):
    return company_task_read._adapt_data_task_to_automation_response(
        row,
        latest_run=latest_run,
        notify_routes=["feishu:chat:oc_row"],
        notify_source="task_row",
    )


def test_script_history_sanitizer_keeps_script_fields_and_drops_secrets() -> None:
    snapshot = company_task_read.sanitize_script_snapshot(
        {
            "enabled": True,
            "paused_by_user": False,
            "notify_routes": ["feishu:chat:oc_ok"],
            "cron_expr": "15 8 * * 1-5",
            "timezone": "Asia/Hong_Kong",
            "notify_enabled": True,
            "cos_mode": "skip",
            "notify_storage_mode": "task_row",
            "command_argv": ["echo", "PLANTED_SECRET_VALUE"],
            "required_secrets": ["PLANTED_SECRET_VALUE"],
        }
    )
    changes = company_task_read.sanitize_script_changes(
        {
            "enabled": {"old": False, "new": True},
            "cron_expr": {"old": "0 1 * * *", "new": "15 8 * * 1-5"},
            "command_argv": {"old": ["a"], "new": ["b"]},
            "notify_routes": {
                "old": [{"bot_token": "tok", "route": "feishu:chat:oc_ok"}],
                "new": ["feishu:chat:oc_ok"],
            },
        }
    )

    assert snapshot["cron_expr"] == "15 8 * * 1-5"
    assert snapshot["enabled"] is True
    assert "command_argv" not in snapshot
    assert "required_secrets" not in snapshot
    assert set(changes) == {"enabled", "cron_expr", "notify_routes"}
    assert changes["notify_routes"].old == [{"route": "feishu:chat:oc_ok"}]
    assert "bot_token" not in str(changes)
    assert "PLANTED_SECRET_VALUE" not in str(snapshot)


def test_company_agent_scrub_drops_migration_and_credential_fields() -> None:
    cleaned = company_task_read.scrub_company_agent_row(_agent_row(updated_at=TIE))

    assert cleaned["metadata"] == {"note": "visible-note"}
    assert cleaned["delivery_config"]["methods"] == ["slack"]
    assert "webhook_secret" not in cleaned["delivery_config"]
    for dropped in ("command_argv", "migration_state", "file_name"):
        assert dropped not in cleaned
    assert "sekret-key" not in str(cleaned)
    assert "pre_company_migration" not in str(cleaned)


def test_company_catalog_sort_is_updated_at_desc_then_engine_then_id() -> None:
    newer = _item(TIE + timedelta(hours=1), "script", "data:bbg-data-pull-asia")
    agent = _item(TIE, "agent", AGENT_ID)
    early = _item(TIE, "script", SCRIPT_EARLY)
    late = _item(TIE, "script", SCRIPT_LATE)
    naive_agent = _item(datetime(2026, 11, 9, 12, 0), "agent", AGENT_ID)

    ordered = [
        item.automation_id
        for item in sorted(
            [late, early, agent, newer],
            key=company_task_read.company_catalog_sort_key,
        )
    ]
    assert ordered == [
        "data:bbg-data-pull-asia",
        AGENT_ID,
        SCRIPT_EARLY,
        SCRIPT_LATE,
    ]
    assert company_task_read.company_catalog_sort_key(
        naive_agent
    ) == company_task_read.company_catalog_sort_key(agent)


def _item(updated_at: datetime, engine_kind: str, automation_id: str) -> Any:
    return type(
        "Item",
        (),
        {
            "updated_at": updated_at,
            "engine_kind": engine_kind,
            "automation_id": automation_id,
        },
    )()


def test_merge_execution_windows_pages_one_stable_order() -> None:
    created = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
    newest = _run(
        created + timedelta(hours=1),
        "script",
        "11111111-1111-4111-8111-111111111111",
    )
    agent = _run(created, "agent", "ffffffff-ffff-4fff-8fff-ffffffffffff")
    script = _run(created, "script", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    older = _run(
        created - timedelta(hours=1),
        "agent",
        "11111111-1111-4111-8111-111111111112",
    )

    first, more = company_task_read.merge_execution_windows(
        [agent, older],
        [newest, script],
        offset=0,
        limit=1,
    )
    second, _more_second = company_task_read.merge_execution_windows(
        [agent, older],
        [newest, script],
        offset=1,
        limit=2,
    )
    empty, empty_more = company_task_read.merge_execution_windows(
        [agent],
        [script],
        offset=10,
        limit=2,
    )

    assert more is True
    assert first[0]["automation_execution_id"] == (
        "11111111-1111-4111-8111-111111111111"
    )
    assert [row["automation_execution_id"] for row in second] == [
        "ffffffff-ffff-4fff-8fff-ffffffffffff",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    ]
    assert empty == []
    assert empty_more is False


def _run(created_at: datetime, engine_kind: str, execution_id: str) -> dict[str, Any]:
    return {
        "created_at": created_at,
        "engine_kind": engine_kind,
        "automation_execution_id": execution_id,
    }


@pytest.mark.asyncio
async def test_list_company_automations_sorts_the_merged_catalog_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_internal(monkeypatch)
    keys = ["bbg-data-pull-asia", "ah-premium-am"]
    monkeypatch.setattr(company_task_read, "load_manifest_job_keys", lambda: list(keys))
    db = company_task_read.company_data_db
    monkeypatch.setattr(
        db,
        "find_present_job_keys",
        create_autospec(db.find_present_job_keys, return_value=set(keys)),
    )
    def _agents(*, status: str | None = None, conn: Any = None) -> list[dict[str, Any]]:
        del conn
        row = _agent_row(updated_at=TIE, status="active")
        if status and row["status"] != status:
            return []
        return [row]

    monkeypatch.setattr(
        db,
        "list_company_agent_rows",
        create_autospec(db.list_company_agent_rows, side_effect=_agents),
    )
    monkeypatch.setattr(
        db,
        "list_active_manifest_def_rows",
        create_autospec(
            db.list_active_manifest_def_rows,
            return_value=[
                _def_row(
                    "ah-premium-am",
                    updated_at=TIE,
                    title="AH AM",
                ),
                _def_row(
                    "bbg-data-pull-asia",
                    updated_at=TIE + timedelta(hours=1),
                    paused_by_user=True,
                    title="BBG",
                ),
            ],
        ),
    )
    monkeypatch.setattr(
        db,
        "list_latest_script_runs",
        create_autospec(db.list_latest_script_runs, return_value={}),
    )

    page = await company_task_read.list_company_automations(
        _session(INTERNAL),
        limit=2,
        offset=0,
    )
    paused = await company_task_read.list_company_automations(
        _session(INTERNAL),
        status="paused",
        limit=10,
        offset=0,
    )
    run_status = await company_task_read.list_company_automations(
        _session(INTERNAL),
        status="succeeded",
        limit=10,
        offset=0,
    )

    assert page.total == 3
    assert [str(item.automation_id) for item in page.automations] == [
        "data:bbg-data-pull-asia",
        AGENT_ID,
    ]
    assert [str(item.automation_id) for item in paused.automations] == [
        "data:bbg-data-pull-asia"
    ]
    assert paused.total == 1
    assert run_status.total == 0
    assert run_status.automations == []


@pytest.mark.asyncio
async def test_company_reads_fail_closed_on_missing_defs_db_errors_and_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_internal(monkeypatch)
    db = company_task_read.company_data_db
    find_present = db.find_present_job_keys
    list_active = db.list_active_manifest_def_rows
    get_script = db.get_script_def_row
    find_missing = create_autospec(find_present, return_value=set())
    active = create_autospec(list_active, return_value=[])
    script_row = create_autospec(get_script, return_value=None)
    monkeypatch.setattr(db, "find_present_job_keys", find_missing)
    monkeypatch.setattr(db, "list_active_manifest_def_rows", active)
    monkeypatch.setattr(db, "get_script_def_row", script_row)

    internal = _app(_session(INTERNAL))
    external = _app(_session(EXTERNAL))
    computer = _app(_computer(INTERNAL))
    async with AsyncClient(transport=ASGITransport(app=internal), base_url="http://test") as client:
        missing = await client.get("/api/v1/automations?scope=company")
        assert missing.status_code == 503, missing.text
        assert "bbg-data-pull-asia" in missing.json()["detail"]
        assert "incomplete" in missing.json()["detail"]
        assert "automations" not in missing.json()
        active.assert_not_awaited()

        script_row.reset_mock()
        one = await client.get("/api/v1/automations/data:bbg-data-pull-asia")
        assert one.status_code == 503, one.text
        assert one.json()["detail"] == (
            "Company data task definition is missing: bbg-data-pull-asia"
        )
        script_row.assert_awaited()

        bad = await client.get("/api/v1/automations/data:")
        assert bad.status_code == 422
        assert bad.json()["detail"] == "Invalid automation id"

        unknown = await client.get("/api/v1/automations/data:not-a-company-script")
        assert unknown.status_code == 404
        assert unknown.json()["detail"] == "Automation not found"

    find_broken = create_autospec(
        find_present,
        side_effect=RuntimeError("db down"),
    )
    monkeypatch.setattr(db, "find_present_job_keys", find_broken)
    async with AsyncClient(transport=ASGITransport(app=internal), base_url="http://test") as client:
        broken = await client.get("/api/v1/automations?scope=company")
        assert broken.status_code == 500, broken.text
        assert broken.json()["detail"] == "Failed to list automations"
        assert "db down" not in broken.text

    monkeypatch.setattr(
        company_task_read,
        "load_manifest_job_keys",
        lambda: ["bbg-data-pull-asia"],
    )
    monkeypatch.setattr(
        db,
        "find_present_job_keys",
        create_autospec(find_present, return_value={"bbg-data-pull-asia"}),
    )
    monkeypatch.setattr(
        db,
        "list_company_agent_rows",
        create_autospec(db.list_company_agent_rows, return_value=[]),
    )
    monkeypatch.setattr(
        db,
        "list_active_manifest_def_rows",
        create_autospec(
            list_active,
            return_value=[
                {
                    **_def_row("bbg-data-pull-asia", updated_at=TIE),
                    "notify_storage_mode": "legacy",
                    "notify_routes": None,
                    "migration_state": {"legacy_notify_owner_user_id": "pm-user"},
                }
            ],
        ),
    )
    monkeypatch.setattr(
        db,
        "list_latest_script_runs",
        create_autospec(db.list_latest_script_runs, return_value={}),
    )
    monkeypatch.setattr(
        db,
        "read_user_model_preferences",
        create_autospec(
            db.read_user_model_preferences,
            side_effect=RuntimeError("prefs down"),
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=internal), base_url="http://test") as client:
        prefs = await client.get("/api/v1/automations?scope=company")
        assert prefs.status_code == 500, prefs.text
        assert prefs.json()["detail"] == "Failed to list automations"
        assert "prefs down" not in prefs.text
        assert "feishu" not in prefs.text

    denied_lookup = create_autospec(get_script, return_value=None)
    monkeypatch.setattr(db, "get_script_def_row", denied_lookup)
    for app in (external, computer):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            denied_lookup.reset_mock()
            denied = await client.get("/api/v1/automations/data:bbg-data-pull-asia")
            unknown = await client.get("/api/v1/automations/data:not-a-company-script")
            malformed = await client.get("/api/v1/automations/DATA:bbg-data-pull-asia")
            assert denied.status_code == 403, denied.text
            assert denied.json()["detail"] == "Forbidden"
            assert unknown.status_code == 403, unknown.text
            assert malformed.status_code == 422
            assert malformed.json()["detail"] == "Invalid automation id"
            denied_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_feed_accepts_raw_script_status_and_default_list_stays_owner_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listed = create_autospec(
        auto_db.list_automations,
        return_value=([], 0),
    )
    company_list = create_autospec(
        company_task_read.list_company_automations,
        return_value=AutomationsListResponse(automations=[], total=0),
    )
    feed = create_autospec(
        company_task_read.list_company_execution_feed,
        return_value=AutomationRunsListResponse(executions=[], has_more=False),
    )
    monkeypatch.setattr(automations_app.auto_db, "list_automations", listed)
    monkeypatch.setattr(company_task_read, "list_company_automations", company_list)
    monkeypatch.setattr(company_task_read, "list_company_execution_feed", feed)

    async def _noop(_user_id: str, _items: list[Any]) -> None:
        return None

    monkeypatch.setattr(
        "src.dalton.notify.dispatch.populate_automation_notify_state",
        _noop,
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app(_session(INTERNAL))),
        base_url="http://test",
    ) as client:
        own = await client.get("/api/v1/automations")
        assert own.status_code == 200, own.text
        listed.assert_awaited_once_with(
            INTERNAL,
            status=None,
            limit=50,
            offset=0,
            computer_id=None,
        )
        company_list.assert_not_awaited()

        runs = await client.get(
            "/api/v1/automations/executions?scope=company&status=succeeded"
        )
        assert runs.status_code == 200, runs.text
        assert feed.await_args.kwargs["status"] == "succeeded"


@pytest.mark.asyncio
async def test_single_execution_history_passes_computer_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A computer token reaches the same execution predicate. A session does not."""
    listed = create_autospec(
        automations_app.exec_db.list_executions,
        return_value=([], False),
    )
    owner_identity = company_data_db.get_automation_owner_identity
    identity = create_autospec(owner_identity, return_value=None)
    monkeypatch.setattr(automations_app.exec_db, "list_executions", listed)
    monkeypatch.setattr(company_data_db, "get_automation_owner_identity", identity)

    await automations_app.list_executions(AGENT_ID, _session(INTERNAL), limit=5, offset=1)
    listed.assert_awaited_once_with(
        INTERNAL, automation_id=AGENT_ID, limit=5, offset=1
    )

    listed.reset_mock()
    await automations_app.list_executions(
        AGENT_ID, _computer(INTERNAL), limit=5, offset=1
    )
    listed.assert_awaited_once_with(
        INTERNAL,
        automation_id=AGENT_ID,
        limit=5,
        offset=1,
        computer_id=COMPUTER,
    )

    listed.reset_mock()
    monkeypatch.setattr(
        company_data_db,
        "get_automation_owner_identity",
        create_autospec(
            owner_identity,
            return_value={
                "user_id": COMPANY_SYSTEM_USER_ID,
                "scope": "company",
                "deleted_at": None,
            },
        ),
    )
    with pytest.raises(HTTPException) as raised:
        await automations_app.list_executions(
            AGENT_ID, _computer(INTERNAL), limit=5, offset=0
        )
    assert raised.value.status_code == 403
    assert raised.value.detail == "Forbidden"
    listed.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_detail_passes_computer_id_into_last_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detail keeps the owner lookup and adds computer reach only for a computer token."""
    row = {
        "automation_id": AGENT_ID,
        "user_id": INTERNAL,
        "name": "Owned Task",
        "description": "desc",
        "trigger_type": "cron",
        "cron_expression": "0 9 * * *",
        "timezone": "Asia/Shanghai",
        "trigger_config": {},
        "next_run_at": None,
        "last_run_at": None,
        "instruction": "Run",
        "workspace_id": None,
        "llm_model": None,
        "additional_context": None,
        "thread_strategy": "new",
        "conversation_thread_id": None,
        "status": "active",
        "max_failures": 3,
        "failure_count": 0,
        "delivery_config": {"methods": []},
        "metadata": {},
        "created_at": TIE,
        "updated_at": TIE,
        "last_execution": None,
        "scope": "personal",
        "notify_storage_mode": "legacy",
        "config_version": 1,
        "active_version": 1,
        "created_by": INTERNAL,
    }
    fetched = create_autospec(auto_db.get_automation, return_value=row)
    monkeypatch.setattr(automations_app.auto_db, "get_automation", fetched)

    async def _noop(_user_id: str, _items: list[Any]) -> None:
        return None

    monkeypatch.setattr(
        "src.dalton.notify.dispatch.populate_automation_notify_state",
        _noop,
    )

    await automations_app.get_automation(AGENT_ID, _session(INTERNAL))
    fetched.assert_awaited_once_with(AGENT_ID, INTERNAL)

    fetched.reset_mock()
    await automations_app.get_automation(AGENT_ID, _computer(INTERNAL))
    fetched.assert_awaited_once_with(AGENT_ID, INTERNAL, computer_id=COMPUTER)
