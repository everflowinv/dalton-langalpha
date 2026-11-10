"""Unit tests for company agent update, pause, and resume.

Narrow stand-ins cover the service. They do not open a database connection.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from unittest.mock import create_autospec

import pytest
from fastapi import HTTPException

from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.services import company_agent_writes as writes
from src.dalton.services import company_automations
from src.dalton.services.company_automations import get_current_history_context
from src.server.database import automation as auto_db
from src.server.dependencies.principal import Principal
from src.server.models.automation import AutomationUpdate, DeliveryConfig
from src.server.services.automations import lifecycle
from src.server.services.automations.lifecycle import PreparedAutomationUpdate

AUTOMATION_ID = "11111111-1111-4111-8111-111111111111"
INTERNAL_ID = "user-internal-a"
ADMIN_ID = "user-admin-a"

_NOTIFY_COLUMNS = (
    "notify_enabled",
    "notify_routes",
    "notify_storage_mode",
    "delivery_config",
)


def _stored_metadata() -> dict:
    return {
        "label": "old",
        "pre_company_migration": {"user_id": "legacy-user"},
        "company_migration": {"stage": "running", "batch_id": "batch-1"},
        "company_migration_batches": [{"batch_id": "batch-1"}],
    }


def _principal(
    user_id: str,
    *,
    computer_id: str | None = None,
    auth: str = "session",
) -> Principal:
    return Principal(user_id=user_id, auth=auth, computer_id=computer_id)


def _identity(**overrides) -> dict:
    row = {
        "automation_id": AUTOMATION_ID,
        "user_id": COMPANY_SYSTEM_USER_ID,
        "scope": "company",
        "workspace_id": None,
        "deleted_at": None,
    }
    row.update(overrides)
    return row


def _company_row(**overrides) -> dict:
    row = {
        "automation_id": AUTOMATION_ID,
        "user_id": COMPANY_SYSTEM_USER_ID,
        "scope": "company",
        "deleted_at": None,
        "notify_storage_mode": "task_row",
        "notify_enabled": True,
        "notify_routes": ["feishu:chat:oc_internal"],
        "status": "active",
        "trigger_type": "price",
        "thread_strategy": "new",
        "llm_model": None,
        "workspace_id": None,
        "conversation_thread_id": None,
        "cron_expression": None,
        "timezone": "UTC",
        "metadata": _stored_metadata(),
    }
    row.update(overrides)
    return row


class _Harness:
    def __init__(self) -> None:
        self.committed: list[dict] = []
        self.prepare_fields = None
        self.prepare_owner = None
        self.delivery_checked = None
        self.model_pref = None
        self.context = None
        self.locked = _company_row()
        self.prepared_kwargs = {"description": "new"}


def _install(
    monkeypatch,
    harness: _Harness,
    *,
    tier: bool = True,
    identity: dict | None = None,
    current: dict | None = None,
):
    identity_mock = create_autospec(
        writes.company_data_db.get_automation_owner_identity,
        return_value=_identity() if identity is None else identity,
    )
    monkeypatch.setattr(
        writes.company_data_db,
        "get_automation_owner_identity",
        identity_mock,
    )
    tier_mock = create_autospec(
        company_automations.is_internal_or_admin,
        return_value=tier,
    )
    monkeypatch.setattr(company_automations, "is_internal_or_admin", tier_mock)
    monkeypatch.setattr(
        "src.server.services.claude_code.hosted.checks_automations",
        lambda: False,
    )

    prepare_spec = create_autospec(lifecycle.prepare_owned_automation_update)

    async def _prepare(
        automation_id,
        user_id,
        fields,
        *,
        conn=None,
        model_pref=None,
        current=None,
        delivery_checked=False,
    ):
        await prepare_spec(
            automation_id,
            user_id,
            fields,
            conn=conn,
            model_pref=model_pref,
            current=current,
            delivery_checked=delivery_checked,
        )
        harness.prepare_fields = fields
        harness.prepare_owner = user_id
        harness.delivery_checked = delivery_checked
        harness.model_pref = model_pref
        update = AutomationUpdate.model_validate(
            fields,
            context={"trigger_type": "price"},
        )
        return PreparedAutomationUpdate(
            current=_company_row(),
            update=update,
            update_kwargs=dict(harness.prepared_kwargs),
            stored_in=None,
            stored_pin=None,
            new_cron=None,
            new_tz=None,
        )

    monkeypatch.setattr(lifecycle, "prepare_owned_automation_update", _prepare)

    update_spec = create_autospec(auto_db.update_automation)

    async def _update(automation_id, user_id, *, conn=None, **kwargs):
        await update_spec(automation_id, user_id, conn=conn, **kwargs)
        ctx = get_current_history_context()
        harness.context = ctx
        effective = dict(kwargs)
        if ctx is not None and ctx.locked_row_reconciler is not None:
            await ctx.locked_row_reconciler(harness.locked, effective, conn)
        harness.committed.append(
            {
                "automation_id": automation_id,
                "user_id": user_id,
                "conn": conn,
                "kwargs": effective,
            }
        )
        return {"automation_id": automation_id, "user_id": user_id, **effective}

    monkeypatch.setattr(writes.auto_db, "update_automation", _update)

    get_mock = create_autospec(
        auto_db.get_automation,
        return_value=current if current is not None else _company_row(),
    )
    monkeypatch.setattr(writes.auto_db, "get_automation", get_mock)
    return identity_mock, prepare_spec, update_spec, get_mock


def _request(body: dict) -> tuple[AutomationUpdate, dict]:
    request = AutomationUpdate.model_validate(body)
    return request, request.model_dump(exclude_none=True)


# --- delivery capture -------------------------------------------------------


def test_raw_null_captures_actual_delivery_config():
    parsed = AutomationUpdate.model_validate(
        {
            "expected_config_version": 1,
            "description": "new",
            "delivery_config": {
                "methods": ["webhook"],
                "webhook_url": "https://evil.example/hook",
            },
            "delivery_config_raw": None,
        }
    )
    assert parsed.delivery_config_raw == {
        "methods": ["webhook"],
        "webhook_url": "https://evil.example/hook",
    }
    assert parsed.delivery_config is not None
    assert parsed.delivery_config.methods == ["webhook"]
    dumped = parsed.model_dump(exclude_none=True)
    assert "delivery_config_raw" not in dumped
    assert dumped["delivery_config"]["methods"] == ["webhook"]
    assert "webhook_url" not in dumped["delivery_config"]


def test_raw_empty_object_captures_actual_delivery_config():
    parsed = AutomationUpdate.model_validate(
        {
            "description": "new",
            "delivery_config": {"methods": ["webhook"]},
            "delivery_config_raw": {},
        }
    )
    assert parsed.delivery_config_raw == {"methods": ["webhook"]}
    assert parsed.delivery_config is not None
    assert parsed.delivery_config.methods == ["webhook"]


def test_omitted_delivery_discards_caller_supplied_raw():
    parsed = AutomationUpdate.model_validate(
        {
            "description": "keep",
            "delivery_config_raw": {"methods": ["webhook"]},
        }
    )
    assert "delivery_config_raw" not in parsed.model_fields_set
    assert "delivery_config" not in parsed.model_fields_set
    dumped = parsed.model_dump(
        exclude_unset=True,
        exclude={"trigger_type", "expected_config_version"},
    )
    assert dumped == {"description": "keep"}


def test_personal_request_keeps_parsed_delivery_and_metadata():
    forged = AutomationUpdate.model_validate(
        {
            "description": "keep",
            "delivery_config": {"methods": ["slack"]},
            "delivery_config_raw": None,
            "metadata": {
                "company_migration": {"stage": "done"},
                "label": "x",
            },
        }
    )
    dumped = forged.model_dump(
        exclude_unset=True,
        exclude={"trigger_type", "expected_config_version"},
    )
    assert "delivery_config_raw" not in dumped
    assert dumped["delivery_config"] == {"methods": ["slack"]}
    assert dumped["metadata"]["company_migration"]["stage"] == "done"
    assert dumped["metadata"]["label"] == "x"

    plain = AutomationUpdate.model_validate(
        {"description": "plain", "delivery_config": {"methods": ["slack"]}}
    )
    plain_dump = plain.model_dump(
        exclude_unset=True,
        exclude={"trigger_type", "expected_config_version"},
    )
    assert plain.delivery_config_raw == {"methods": ["slack"]}
    assert plain_dump == {
        "description": "plain",
        "delivery_config": {"methods": ["slack"]},
    }
    assert "delivery_config_raw" not in plain_dump


@pytest.mark.asyncio
async def test_update_refuses_delivery_hidden_by_raw_null_without_write(monkeypatch):
    harness = _Harness()
    _, prepare_spec, update_spec, _ = _install(monkeypatch, harness)
    request, fields = _request(
        {
            "expected_config_version": 1,
            "description": "new",
            "delivery_config": {
                "methods": ["webhook"],
                "webhook_url": "https://evil.example/hook",
            },
            "delivery_config_raw": None,
        }
    )
    with pytest.raises(HTTPException) as exc:
        await writes.update_company_agent(
            _principal(INTERNAL_ID),
            AUTOMATION_ID,
            fields,
            request=request,
            expected_config_version=1,
        )
    assert exc.value.status_code == 400
    assert "delivery_config" in exc.value.detail
    prepare_spec.assert_not_awaited()
    update_spec.assert_not_awaited()
    assert harness.committed == []


@pytest.mark.asyncio
async def test_update_refuses_delivery_hidden_by_raw_empty_object_without_write(
    monkeypatch,
):
    harness = _Harness()
    _, prepare_spec, update_spec, _ = _install(monkeypatch, harness)
    request, fields = _request(
        {
            "expected_config_version": 1,
            "description": "new",
            "delivery_config": {"methods": ["webhook"]},
            "delivery_config_raw": {},
        }
    )
    with pytest.raises(HTTPException) as exc:
        await writes.update_company_agent(
            _principal(INTERNAL_ID),
            AUTOMATION_ID,
            fields,
            request=request,
            expected_config_version=1,
        )
    assert exc.value.status_code == 400
    prepare_spec.assert_not_awaited()
    update_spec.assert_not_awaited()
    assert harness.committed == []


@pytest.mark.asyncio
async def test_update_refuses_fields_delivery_when_raw_is_null_without_write(
    monkeypatch,
):
    harness = _Harness()
    _, prepare_spec, update_spec, _ = _install(monkeypatch, harness)
    with pytest.raises(HTTPException) as exc:
        await writes.update_company_agent(
            _principal(INTERNAL_ID),
            AUTOMATION_ID,
            {
                "description": "new",
                "delivery_config": {"methods": ["webhook"]},
                "delivery_config_raw": None,
            },
            request=None,
            expected_config_version=1,
        )
    assert exc.value.status_code == 400
    prepare_spec.assert_not_awaited()
    update_spec.assert_not_awaited()
    assert harness.committed == []


@pytest.mark.asyncio
async def test_update_refuses_parsed_delivery_when_constructed_raw_is_null(
    monkeypatch,
):
    harness = _Harness()
    _, prepare_spec, update_spec, _ = _install(monkeypatch, harness)
    request = AutomationUpdate.model_construct(
        description="new",
        delivery_config=DeliveryConfig(methods=["webhook"]),
        delivery_config_raw=None,
    )
    request.__pydantic_fields_set__.update(
        {"description", "delivery_config", "delivery_config_raw"}
    )
    with pytest.raises(HTTPException) as exc:
        await writes.update_company_agent(
            _principal(INTERNAL_ID),
            AUTOMATION_ID,
            {"description": "new", "delivery_config_raw": None},
            request=request,
            expected_config_version=1,
        )
    assert exc.value.status_code == 400
    prepare_spec.assert_not_awaited()
    update_spec.assert_not_awaited()
    assert harness.committed == []


@pytest.mark.asyncio
async def test_update_discards_raw_when_delivery_omitted_and_writes_description(
    monkeypatch,
):
    harness = _Harness()
    _, prepare_spec, update_spec, _ = _install(monkeypatch, harness)
    request, fields = _request(
        {
            "expected_config_version": 1,
            "description": "new",
            "delivery_config_raw": {"methods": ["webhook"]},
        }
    )
    fields["delivery_config_raw"] = {"methods": ["webhook"]}
    result = await writes.update_company_agent(
        _principal(INTERNAL_ID),
        AUTOMATION_ID,
        fields,
        request=request,
        expected_config_version=1,
    )
    assert result is not None
    prepare_spec.assert_awaited()
    update_spec.assert_awaited()
    assert harness.prepare_fields == {"description": "new"}
    assert harness.committed[0]["kwargs"] == {"description": "new"}
    assert harness.committed[0]["user_id"] == COMPANY_SYSTEM_USER_ID


# --- migration metadata -----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"label": "x", "pre_company_migration": {"user_id": "forged"}},
        {"label": "x", "company_migration": {"stage": "done"}},
        {"label": "x", "company_migration_batches": [{"batch_id": "forged"}]},
        {"label": "x", "Company_Migration": {"stage": "done"}},
        {"notes": [{"company_migration": {"stage": "frozen"}}]},
    ],
)
async def test_metadata_rejects_protected_migration_subfields_without_write(
    monkeypatch, metadata
):
    harness = _Harness()
    _, prepare_spec, update_spec, _ = _install(monkeypatch, harness)
    request, fields = _request(
        {"expected_config_version": 2, "metadata": metadata}
    )
    with pytest.raises(HTTPException) as exc:
        await writes.update_company_agent(
            _principal(INTERNAL_ID),
            AUTOMATION_ID,
            fields,
            request=request,
            expected_config_version=2,
        )
    assert exc.value.status_code == 400
    assert "migration" in exc.value.detail
    prepare_spec.assert_not_awaited()
    update_spec.assert_not_awaited()
    assert harness.committed == []


@pytest.mark.asyncio
async def test_metadata_replace_retains_migration_and_backup_subfields(monkeypatch):
    harness = _Harness()
    incoming = {"label": "new", "note": "desk"}
    harness.prepared_kwargs = {"metadata": dict(incoming)}
    _, prepare_spec, update_spec, _ = _install(monkeypatch, harness)
    request, fields = _request(
        {"expected_config_version": 3, "metadata": incoming}
    )
    await writes.update_company_agent(
        _principal(INTERNAL_ID),
        AUTOMATION_ID,
        fields,
        request=request,
        expected_config_version=3,
    )
    prepare_spec.assert_awaited()
    update_spec.assert_awaited()
    assert harness.committed[0]["kwargs"]["metadata"] == {
        "label": "new",
        "note": "desk",
        "pre_company_migration": {"user_id": "legacy-user"},
        "company_migration": {"stage": "running", "batch_id": "batch-1"},
        "company_migration_batches": [{"batch_id": "batch-1"}],
    }
    assert harness.context is not None
    assert harness.context.actor_user_id == INTERNAL_ID
    assert harness.context.action == "update"
    assert harness.context.expected_config_version == 3


# --- actor, version, lock, notify ------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [INTERNAL_ID, ADMIN_ID])
async def test_internal_and_admin_update_use_caller_actor_company_owner_and_version(
    monkeypatch, actor
):
    harness = _Harness()
    harness.prepared_kwargs = {"description": f"by {actor}"}
    _, prepare_spec, update_spec, _ = _install(monkeypatch, harness)
    request, fields = _request(
        {"description": f"by {actor}", "expected_config_version": 4}
    )
    await writes.update_company_agent(
        _principal(actor),
        AUTOMATION_ID,
        fields,
        request=request,
        expected_config_version=4,
    )
    assert prepare_spec.await_args.args[1] == COMPANY_SYSTEM_USER_ID
    assert prepare_spec.await_args.kwargs["model_pref"] is None
    assert prepare_spec.await_args.kwargs["delivery_checked"] is False
    assert harness.committed[0]["user_id"] == COMPANY_SYSTEM_USER_ID
    assert harness.committed[0]["kwargs"]["description"] == f"by {actor}"
    assert harness.context is not None
    assert harness.context.actor_user_id == actor
    assert harness.context.action == "update"
    assert harness.context.expected_config_version == 4
    assert harness.context.record_history is True
    update_spec.assert_awaited()


@pytest.mark.asyncio
async def test_external_caller_is_forbidden(monkeypatch):
    harness = _Harness()
    _, prepare_spec, update_spec, _ = _install(monkeypatch, harness, tier=False)
    with pytest.raises(HTTPException) as exc:
        await writes.update_company_agent(
            _principal("user-external"),
            AUTOMATION_ID,
            {"description": "new"},
            expected_config_version=1,
        )
    assert exc.value.status_code == 403
    prepare_spec.assert_not_awaited()
    update_spec.assert_not_awaited()
    assert harness.committed == []


@pytest.mark.asyncio
async def test_computer_caller_is_forbidden(monkeypatch):
    harness = _Harness()
    _, prepare_spec, update_spec, _ = _install(monkeypatch, harness)
    with pytest.raises(HTTPException) as exc:
        await writes.update_company_agent(
            _principal(INTERNAL_ID, auth="computer", computer_id="computer-1"),
            AUTOMATION_ID,
            {"description": "new"},
            expected_config_version=1,
        )
    assert exc.value.status_code == 403
    prepare_spec.assert_not_awaited()
    update_spec.assert_not_awaited()
    assert harness.committed == []


@pytest.mark.asyncio
async def test_company_system_caller_is_forbidden(monkeypatch):
    harness = _Harness()
    _, prepare_spec, update_spec, _ = _install(monkeypatch, harness)
    with pytest.raises(HTTPException) as exc:
        await writes.update_company_agent(
            _principal(COMPANY_SYSTEM_USER_ID),
            AUTOMATION_ID,
            {"description": "new"},
            expected_config_version=1,
        )
    assert exc.value.status_code == 403
    prepare_spec.assert_not_awaited()
    update_spec.assert_not_awaited()
    assert harness.committed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_version",
    [None, 0, -1, True, False, "1", 1.5],
)
async def test_missing_and_invalid_expected_config_version_is_400_without_write(
    monkeypatch, raw_version
):
    harness = _Harness()
    _, prepare_spec, update_spec, _ = _install(monkeypatch, harness)
    with pytest.raises(HTTPException) as exc:
        await writes.update_company_agent(
            _principal(INTERNAL_ID),
            AUTOMATION_ID,
            {"description": "new"},
            expected_config_version=raw_version,
        )
    assert exc.value.status_code == 400
    assert "expected_config_version" in exc.value.detail
    prepare_spec.assert_not_awaited()
    update_spec.assert_not_awaited()
    assert harness.committed == []


def test_locked_identity_deletion_freeze_and_notify_storage():
    with pytest.raises(HTTPException) as identity_user:
        writes.assert_locked_company_agent(_company_row(user_id=INTERNAL_ID))
    assert identity_user.value.status_code == 409

    with pytest.raises(HTTPException) as identity_scope:
        writes.assert_locked_company_agent(_company_row(scope="personal"))
    assert identity_scope.value.status_code == 409

    with pytest.raises(HTTPException) as deleted:
        writes.assert_locked_company_agent(
            _company_row(deleted_at=datetime.now(UTC))
        )
    assert deleted.value.status_code == 404

    with pytest.raises(HTTPException) as notify_mode:
        writes.assert_locked_company_agent(
            _company_row(notify_storage_mode="legacy")
        )
    assert notify_mode.value.status_code == 409

    for stage in ("frozen", "rollback_frozen", "switching"):
        with pytest.raises(HTTPException) as frozen:
            writes.assert_locked_company_agent(
                _company_row(metadata={"company_migration": {"stage": stage}})
            )
        assert frozen.value.status_code == 409

    for stage in ("running", "done", "rolled_back"):
        writes.assert_locked_company_agent(
            _company_row(metadata={"company_migration": {"stage": stage}})
        )
    writes.assert_locked_company_agent(_company_row(metadata={}))
    writes.assert_locked_company_agent(_company_row(metadata=None))


@pytest.mark.asyncio
async def test_update_freeze_raises_409_before_commit(monkeypatch):
    harness = _Harness()
    harness.locked = _company_row(
        metadata={"company_migration": {"stage": "frozen"}}
    )
    _, prepare_spec, update_spec, _ = _install(monkeypatch, harness)
    request, fields = _request(
        {"description": "new", "expected_config_version": 1}
    )
    with pytest.raises(HTTPException) as exc:
        await writes.update_company_agent(
            _principal(INTERNAL_ID),
            AUTOMATION_ID,
            fields,
            request=request,
            expected_config_version=1,
        )
    assert exc.value.status_code == 409
    prepare_spec.assert_awaited()
    update_spec.assert_awaited()
    assert harness.committed == []


@pytest.mark.asyncio
async def test_update_does_not_write_notification_columns(monkeypatch):
    harness = _Harness()
    harness.prepared_kwargs = {
        "description": "new",
        "notify_enabled": False,
        "notify_routes": ["webhook"],
        "notify_storage_mode": "legacy",
        "delivery_config": {"methods": []},
        "scope": "personal",
        "audit_history": [],
        "config_version": 9,
    }
    _install(monkeypatch, harness)
    request, fields = _request(
        {
            "description": "new",
            "expected_config_version": 2,
            "delivery_config": {},
        }
    )
    fields["user_id"] = INTERNAL_ID
    fields["notify_enabled"] = False
    fields["notify_routes"] = ["webhook"]
    fields["notify_storage_mode"] = "legacy"
    await writes.update_company_agent(
        _principal(INTERNAL_ID),
        AUTOMATION_ID,
        fields,
        request=request,
        expected_config_version=2,
    )
    assert harness.prepare_fields is not None
    for column in _NOTIFY_COLUMNS:
        assert column not in harness.prepare_fields
    assert "delivery_config_raw" not in harness.prepare_fields
    assert "user_id" not in harness.prepare_fields
    written = harness.committed[0]["kwargs"]
    for column in (
        *_NOTIFY_COLUMNS,
        "scope",
        "user_id",
        "audit_history",
        "config_version",
    ):
        assert column not in written
    assert written["description"] == "new"
    assert harness.committed[0]["user_id"] == COMPANY_SYSTEM_USER_ID


@pytest.mark.asyncio
async def test_pause_and_resume_record_caller_actor_and_company_owner(monkeypatch):
    with monkeypatch.context() as pause_patch:
        pause_harness = _Harness()
        _, pause_prepare, _, pause_get = _install(
            pause_patch,
            pause_harness,
            current=_company_row(status="active", trigger_type="price"),
        )
        paused = await writes.pause_company_agent(
            _principal(INTERNAL_ID),
            AUTOMATION_ID,
            expected_config_version=5,
        )
        assert paused is not None
        pause_prepare.assert_not_awaited()
        assert pause_get.await_args.args[1] == COMPANY_SYSTEM_USER_ID
        assert pause_harness.committed[0]["user_id"] == COMPANY_SYSTEM_USER_ID
        assert pause_harness.committed[0]["kwargs"]["status"] == "paused"
        assert pause_harness.committed[0]["kwargs"]["next_run_at"] is None
        for column in _NOTIFY_COLUMNS:
            assert column not in pause_harness.committed[0]["kwargs"]
        assert pause_harness.context is not None
        assert pause_harness.context.actor_user_id == INTERNAL_ID
        assert pause_harness.context.action == "pause"
        assert pause_harness.context.expected_config_version == 5

    resume_harness = _Harness()
    paused_row = _company_row(status="paused", trigger_type="price")
    resume_harness.locked = paused_row
    _, resume_prepare, _, resume_get = _install(
        monkeypatch,
        resume_harness,
        current=paused_row,
    )
    resumed = await writes.resume_company_agent(
        _principal(ADMIN_ID),
        AUTOMATION_ID,
        expected_config_version=6,
    )
    assert resumed is not None
    resume_prepare.assert_not_awaited()
    assert resume_get.await_args.args[1] == COMPANY_SYSTEM_USER_ID
    assert resume_harness.committed[0]["user_id"] == COMPANY_SYSTEM_USER_ID
    assert resume_harness.committed[0]["kwargs"]["status"] == "active"
    assert resume_harness.committed[0]["kwargs"]["failure_count"] == 0
    assert resume_harness.committed[0]["kwargs"]["disable_reason"] is None
    for column in _NOTIFY_COLUMNS:
        assert column not in resume_harness.committed[0]["kwargs"]
    assert resume_harness.context is not None
    assert resume_harness.context.actor_user_id == ADMIN_ID
    assert resume_harness.context.action == "resume"
    assert resume_harness.context.expected_config_version == 6


def test_lifecycle_signatures_accept_company_write_calls():
    inspect.signature(lifecycle.prepare_owned_automation_update).bind(
        AUTOMATION_ID,
        COMPANY_SYSTEM_USER_ID,
        {"description": "new"},
        conn=None,
        model_pref=None,
        delivery_checked=False,
    )
    update = AutomationUpdate.model_validate(
        {"description": "new"},
        context={"trigger_type": "price"},
    )
    inspect.signature(lifecycle.recheck_locked_automation_update).bind(
        user_id=COMPANY_SYSTEM_USER_ID,
        update=update,
        current={"trigger_type": "price"},
        stored_in=None,
        stored_pin=None,
        new_cron=None,
        new_tz=None,
        pref_for_lock=None,
        locked_row={"trigger_type": "price", "status": "active"},
        effective_kwargs={"description": "new"},
        locked_conn=None,
    )
    active = {"status": "active", "trigger_type": "price"}
    inspect.signature(lifecycle.pause_update_fields).bind(active)
    pause_fields = lifecycle.pause_update_fields(active)
    inspect.signature(lifecycle.apply_locked_pause_schedule).bind(
        active, dict(pause_fields)
    )
    paused = {"status": "paused", "trigger_type": "price"}
    inspect.signature(lifecycle.resume_update_fields).bind(paused)
    resume_fields = lifecycle.resume_update_fields(paused)
    inspect.signature(lifecycle.apply_locked_resume_schedule).bind(
        paused, dict(resume_fields)
    )
    inspect.signature(auto_db.update_automation).bind(
        AUTOMATION_ID,
        COMPANY_SYSTEM_USER_ID,
        conn=None,
        description="new",
    )
    inspect.signature(auto_db.get_automation).bind(
        AUTOMATION_ID,
        COMPANY_SYSTEM_USER_ID,
        conn=None,
    )
    assert pause_fields["status"] == "paused"
    assert resume_fields["status"] == "active"
