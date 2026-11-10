"""Service and route checks for company script schedule writes.

The database is mocked. Agent UUID behavior stays on the existing handler.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import inspect
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
import pytest

from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.database import company_data_tasks as company_data_db
from src.dalton.services import company_automations as company_auto_svc
from src.dalton.services import company_script_writes as script_writes
from src.server.dependencies.principal import Principal
from src.server.models.automation import (
    CompanyScriptUpdate,
    CompanyScriptVersionRequest,
    ScriptAutomationResponse,
)
from tests.conftest import create_test_app

JOB = "cn-leverage-daily"
ACTOR = "internal-user"
NOW = datetime(2026, 11, 10, 1, 0, tzinfo=UTC)


class _Context:
    def __init__(self, value: Any = None) -> None:
        self.value = self if value is None else value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Conn:
    def transaction(self) -> _Context:
        return _Context()

    def cursor(self, **_kwargs: Any) -> _Context:
        return _Context(object())


@asynccontextmanager
async def _connection(conn: Any = None):
    yield conn if conn is not None else _Conn()


def _principal(user_id: str, *, computer_id: str | None = None) -> Principal:
    if computer_id is not None:
        return Principal(
            user_id=user_id,
            auth="computer",
            computer_id=computer_id,
            scopes=("workspace:write",),
        )
    return Principal(user_id=user_id, auth="session")


def _seed() -> dict[str, Any]:
    return {
        "version": 1,
        "prev_version": None,
        "actor_user_id": "system",
        "changed_at": "2026-11-07T00:00:00Z",
        "action": "seed",
        "changes": {},
        "snapshot": {
            "enabled": True,
            "cron_expr": "45 17 * * 1-5",
            "timezone": "Asia/Shanghai",
        },
    }


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "job_key": JOB,
        "title": "CN leverage",
        "cron_expr": "45 17 * * 1-5",
        "timezone": "Asia/Shanghai",
        "enabled": True,
        "paused_by_user": False,
        "notify_enabled": False,
        "notify_routes": None,
        "notify_storage_mode": "legacy",
        "cos_mode": "skip",
        "config_version": 1,
        "active_version": 1,
        "updated_by": "seed",
        "updated_at": NOW,
        "migration_state": {},
        "deleted_at": None,
        "audit_history": [_seed()],
    }
    row.update(overrides)
    return row


def _detail() -> ScriptAutomationResponse:
    return ScriptAutomationResponse(
        automation_id=f"data:{JOB}",
        name="CN leverage",
        status="active",
        enabled=True,
        paused_by_user=False,
        cron_expression="15 18 * * 1-5",
        timezone="Asia/Shanghai",
        config_version=2,
        active_version=2,
        updated_at=NOW,
        notify_storage_mode="legacy",
    )


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    async def _allowed(user_id: str | None) -> bool:
        return str(user_id or "") in {ACTOR, "admin-user"}

    monkeypatch.setattr(company_auto_svc, "is_internal_or_admin", _allowed)
    monkeypatch.setattr(script_writes, "load_manifest_job_keys", lambda: [JOB])
    monkeypatch.setattr(company_data_db, "get_db_connection", _connection)
    locked = AsyncMock(return_value=_row())
    updated = AsyncMock(return_value=True)
    cancelled = AsyncMock(return_value=1)
    detail = AsyncMock(return_value=_detail())
    monkeypatch.setattr(company_data_db, "lock_script_def_for_update", locked)
    monkeypatch.setattr(company_data_db, "update_locked_script_def", updated)
    monkeypatch.setattr(company_data_db, "cancel_queued_script_runs", cancelled)
    monkeypatch.setattr(script_writes, "get_script_automation", detail)
    return {
        "lock": locked,
        "update": updated,
        "cancel": cancelled,
        "detail": detail,
    }


def test_service_signatures() -> None:
    update = inspect.signature(script_writes.update_company_script)
    pause = inspect.signature(script_writes.pause_company_script)
    resume = inspect.signature(script_writes.resume_company_script)
    assert list(update.parameters) == ["principal", "job_key", "request", "conn"]
    assert update.parameters["conn"].kind is inspect.Parameter.KEYWORD_ONLY
    for sig in (pause, resume):
        assert list(sig.parameters) == [
            "principal",
            "job_key",
            "expected_config_version",
            "conn",
        ]
        assert sig.parameters["expected_config_version"].kind is inspect.Parameter.KEYWORD_ONLY
        assert sig.parameters["conn"].kind is inspect.Parameter.KEYWORD_ONLY


def test_script_request_rejects_unknown_and_loose_versions() -> None:
    with pytest.raises(Exception):
        CompanyScriptUpdate.model_validate(
            {"expected_config_version": 1, "enabled": False}
        )
    for bad in (True, 1.5, "1", None):
        with pytest.raises(Exception):
            CompanyScriptUpdate.model_validate(
                {"cron_expression": "15 18 * * 1-5", "expected_config_version": bad}
            )
    with pytest.raises(Exception):
        CompanyScriptUpdate.model_validate({"expected_config_version": 1})
    with pytest.raises(Exception):
        CompanyScriptUpdate.model_validate(
            {"cron_expression": None, "expected_config_version": 1}
        )
    with pytest.raises(Exception):
        CompanyScriptVersionRequest.model_validate({})
    with pytest.raises(Exception):
        CompanyScriptVersionRequest.model_validate(
            {"expected_config_version": 1, "actor_user_id": ACTOR}
        )
    parsed = CompanyScriptUpdate.model_validate(
        {
            "cron_expression": " 15 18 * * 1-5 ",
            "expected_config_version": 1,
        }
    )
    assert parsed.cron_expression == "15 18 * * 1-5"
    assert parsed.timezone is None


@pytest.mark.asyncio
async def test_refuses_external_computer_system_and_unknown_manifest(writes: dict[str, AsyncMock]) -> None:
    request = CompanyScriptUpdate.model_validate(
        {"cron_expression": "15 18 * * 1-5", "expected_config_version": 1}
    )
    for principal in (
        _principal("external-user"),
        _principal(ACTOR, computer_id=str(uuid4())),
        _principal(COMPANY_SYSTEM_USER_ID),
    ):
        with pytest.raises(HTTPException) as raised:
            await script_writes.update_company_script(principal, JOB, request)
        assert raised.value.status_code == 403
    with pytest.raises(HTTPException) as missing:
        await script_writes.update_company_script(
            _principal(ACTOR), "not-a-manifest-task", request
        )
    assert missing.value.status_code == 404
    writes["lock"].assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_deleted_frozen_and_bad_migration_refuse(
    writes: dict[str, AsyncMock],
) -> None:
    request = CompanyScriptUpdate.model_validate(
        {"timezone": "Asia/Hong_Kong", "expected_config_version": 1}
    )
    principal = _principal(ACTOR)
    writes["lock"].return_value = None
    with pytest.raises(HTTPException) as missing:
        await script_writes.update_company_script(principal, JOB, request)
    assert missing.value.status_code == 503

    writes["lock"].return_value = _row(deleted_at=NOW)
    with pytest.raises(HTTPException) as deleted:
        await script_writes.update_company_script(principal, JOB, request)
    assert deleted.value.status_code == 404

    for state in (
        {"company_migration": {"stage": "frozen"}},
        {"company_migration": {"stage": "rollback_frozen"}},
        {"company_migration": {"stage": "switching"}},
        None,
        {"company_migration": None},
        {"company_migration": {"stage": 1}},
        {"company_migration": {"stage": "  "}},
    ):
        writes["lock"].return_value = _row(migration_state=state)
        with pytest.raises(HTTPException) as refused:
            await script_writes.update_company_script(principal, JOB, request)
        assert refused.value.status_code == 409
    writes["update"].assert_not_awaited()
    writes["cancel"].assert_not_awaited()


@pytest.mark.asyncio
async def test_version_conflict_and_corrupt_history_do_not_write(
    writes: dict[str, AsyncMock],
) -> None:
    request = CompanyScriptUpdate.model_validate(
        {"cron_expression": "15 18 * * 1-5", "expected_config_version": 1}
    )
    principal = _principal(ACTOR)
    current = _seed()
    current["version"] = 2
    writes["lock"].return_value = _row(
        config_version=2,
        active_version=2,
        audit_history=[current],
    )
    with pytest.raises(HTTPException) as conflict:
        await script_writes.update_company_script(principal, JOB, request)
    assert conflict.value.status_code == 409

    writes["lock"].return_value = _row(audit_history={"bad": True})
    with pytest.raises(HTTPException) as corrupt:
        await script_writes.update_company_script(principal, JOB, request)
    assert corrupt.value.status_code == 409
    assert "Corrupt script history" in str(corrupt.value.detail)
    writes["update"].assert_not_awaited()


@pytest.mark.asyncio
async def test_same_schedule_checks_version_without_history_or_cancel(
    writes: dict[str, AsyncMock],
) -> None:
    request = CompanyScriptUpdate.model_validate(
        {"cron_expression": "45 17 * * 1-5", "timezone": "Asia/Shanghai", "expected_config_version": 1}
    )
    result = await script_writes.update_company_script(_principal(ACTOR), JOB, request)
    assert result.automation_id == f"data:{JOB}"
    writes["lock"].assert_awaited()
    writes["update"].assert_not_awaited()
    writes["cancel"].assert_not_awaited()
    writes["detail"].assert_awaited()


@pytest.mark.asyncio
async def test_schedule_change_appends_history_and_cancels_only_queued_cron(
    writes: dict[str, AsyncMock],
) -> None:
    request = CompanyScriptUpdate.model_validate(
        {"cron_expression": "15 18 * * 1-5", "expected_config_version": 1}
    )
    await script_writes.update_company_script(_principal("admin-user"), JOB, request)
    kwargs = writes["update"].await_args.kwargs
    assert kwargs["cron_expr"] == "15 18 * * 1-5"
    assert kwargs["timezone"] == "Asia/Shanghai"
    assert kwargs["enabled"] is True
    assert kwargs["paused_by_user"] is False
    assert kwargs["config_version"] == 2
    assert kwargs["active_version"] == 2
    assert kwargs["updated_by"] == "admin-user"
    assert kwargs["expected_config_version"] == 1
    history = kwargs["audit_history"]
    assert history[0]["action"] == "seed"
    assert history[1]["version"] == 2
    assert history[1]["prev_version"] == 1
    assert history[1]["actor_user_id"] == "admin-user"
    assert history[1]["action"] == "update"
    assert history[1]["changes"] == {
        "cron_expr": {"old": "45 17 * * 1-5", "new": "15 18 * * 1-5"}
    }
    assert history[1]["snapshot"]["notify_routes"] is None
    assert history[1]["snapshot"]["notify_storage_mode"] == "legacy"
    assert history[1]["snapshot"]["cos_mode"] == "skip"
    assert "command_argv" not in history[1]["snapshot"]
    cancel = writes["cancel"].await_args.kwargs
    assert cancel["failure_reason"] == "schedule_changed_before_start"
    assert cancel["trigger_source"] == "cron"


@pytest.mark.asyncio
async def test_pause_and_resume_version_only_real_changes(
    writes: dict[str, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trial = AsyncMock()
    monkeypatch.setattr(
        "src.dalton.autonomy.system_jobs._reject_enable_if_trial_run_active",
        trial,
    )
    principal = _principal(ACTOR)
    await script_writes.pause_company_script(
        principal, JOB, expected_config_version=1
    )
    paused = writes["update"].await_args.kwargs
    assert paused["enabled"] is False
    assert paused["paused_by_user"] is True
    assert paused["audit_history"][1]["action"] == "pause"
    assert writes["cancel"].await_args.kwargs["failure_reason"] == "paused_before_start"
    assert writes["cancel"].await_args.kwargs.get("trigger_source") is None
    trial.assert_not_awaited()

    writes["update"].reset_mock()
    writes["cancel"].reset_mock()
    writes["lock"].return_value = _row(enabled=False, paused_by_user=True)
    await script_writes.pause_company_script(
        principal, JOB, expected_config_version=1
    )
    writes["update"].assert_not_awaited()
    writes["cancel"].assert_not_awaited()

    writes["lock"].return_value = _row(enabled=False, paused_by_user=True, audit_history=[])
    trial.side_effect = ValueError("试跑进行中（run_id=abc），结束后再开启")
    with pytest.raises(HTTPException) as active:
        await script_writes.resume_company_script(
            principal, JOB, expected_config_version=1
        )
    assert active.value.status_code == 409
    assert "abc" in str(active.value.detail)
    writes["update"].assert_not_awaited()

    trial.side_effect = None
    writes["lock"].return_value = _row(enabled=False, paused_by_user=False, audit_history=[])
    await script_writes.resume_company_script(
        principal, JOB, expected_config_version=1
    )
    resumed = writes["update"].await_args.kwargs
    assert resumed["enabled"] is True
    assert resumed["paused_by_user"] is False
    assert len(resumed["audit_history"]) == 1
    assert resumed["audit_history"][0]["version"] == 2
    assert resumed["audit_history"][0]["prev_version"] == 1
    assert resumed["audit_history"][0]["action"] == "resume"
    writes["cancel"].assert_not_awaited()


def _automation(**overrides: Any) -> dict[str, Any]:
    data = {
        "automation_id": "6f1c2a9e-3b4d-4e8f-9a1b-2c3d4e5f6a7b",
        "user_id": "test-user-123",
        "name": "Daily Briefing",
        "description": "Morning market summary",
        "trigger_type": "cron",
        "cron_expression": "0 8 * * *",
        "timezone": "UTC",
        "trigger_config": None,
        "next_run_at": NOW,
        "last_run_at": None,
        "instruction": "Give me a market summary",
        "workspace_id": None,
        "llm_model": None,
        "additional_context": None,
        "thread_strategy": "new",
        "conversation_thread_id": None,
        "status": "active",
        "max_failures": 3,
        "failure_count": 0,
        "delivery_config": None,
        "metadata": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_routes_keep_uuid_on_agent_and_data_on_script() -> None:
    from src.server.app.automations import router

    app = create_test_app(router)
    agent_id = "6f1c2a9e-3b4d-4e8f-9a1b-2c3d4e5f6a7b"
    script_body = {
        "cron_expression": "15 18 * * 1-5",
        "expected_config_version": 1,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with (
            patch(
                "src.server.app.automations.handler.update_automation",
                new_callable=AsyncMock,
                return_value=_automation(),
            ) as agent_update,
            patch(
                "src.dalton.services.company_script_writes.update_company_script",
                new_callable=AsyncMock,
                return_value=_detail(),
            ) as script_update,
            patch(
                "src.server.app.automations.assert_automation_target",
                new_callable=AsyncMock,
            ) as target,
        ):
            agent = await client.patch(
                f"/api/v1/automations/{agent_id.upper()}",
                json={"name": "Updated"},
            )
            script = await client.patch(
                f"/api/v1/automations/data:{JOB}",
                json=script_body,
            )
            bad_script = await client.patch(
                f"/api/v1/automations/data:{JOB}",
                json={"enabled": True, "expected_config_version": 1},
            )
            assert target.await_count == 1
            assert agent_update.await_args.kwargs["automation_id"] == agent_id
            bad_agent_version = await client.patch(
                f"/api/v1/automations/{agent_id}",
                json={"name": "Updated", "expected_config_version": True},
            )
            missing_pause = await client.post(f"/api/v1/automations/data:{JOB}/pause")
            with patch(
                "src.server.app.automations.handler.pause_automation",
                new_callable=AsyncMock,
                return_value=_automation(status="paused"),
            ) as agent_pause:
                paused = await client.post(
                    f"/api/v1/automations/{agent_id.upper()}/pause"
                )
        assert agent.status_code == 200
        assert agent.json()["name"] == "Daily Briefing"
        agent_update.assert_awaited()
        assert script.status_code == 200
        assert script.json()["engine_kind"] == "script"
        assert script.json()["automation_id"] == f"data:{JOB}"
        script_update.assert_awaited()
        assert target.await_count == 2
        assert bad_script.status_code == 422
        assert bad_agent_version.status_code == 400
        assert missing_pause.status_code == 422
        assert script_update.await_count == 1
        assert paused.status_code == 200
        assert agent_pause.await_args.args[0] == agent_id
