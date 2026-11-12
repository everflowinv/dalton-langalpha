"""The automation_report tool submits or reads one saved report batch.

A material submit calls the sender with the saved execution id only.
Confirmation is the saved status read. Storage and the sender are
autospec'd at their real signatures.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.access_tiers import is_internal_or_admin
from src.dalton.database.automation_reports import (
    read_automation_report_status,
    submit_automation_report,
)
from src.dalton.notify.dispatch import dispatch_automation_report
from src.dalton.tools import automation_report_tools as report_tools
from src.dalton.tools import build_dalton_tools
from src.dalton.tools.automation_report_tools import (
    create_automation_report_tools,
)

_USER = "user-1"
_WORKSPACE = "ws-1"
_THREAD = "thread-1"
_RUN = "run-1"
_AUTOMATION = "22222222-2222-4222-8222-222222222222"
_EXECUTION = "11111111-1111-4111-8111-111111111111"
_EXECUTION_B = "33333333-3333-4333-8333-333333333333"
_REPORT = "msft-2026-11-10"
_CALLER_ARGS = {"action", "report_run_id", "material", "cards", "failure_text"}
_ROLES = ("analyst", "chief_of_staff")

_CARDS = [
    {
        "schema": "2.0",
        "body": {"elements": [{"tag": "markdown", "content": "MSFT"}]},
    }
]

_PENDING = {
    "ok": True,
    "action": "submit",
    "quiet": False,
    "report_run_id": _REPORT,
    "profile": "msft-tracker",
    "content_kind": "cards",
    "content_sha256": "abc",
    "execution_id": _EXECUTION,
    "delivery_confirmed": False,
    "status": "pending",
    "targets": ["feishu:chat:oc_saved"],
    "payload": {"source": "automation_report", "cards": _CARDS},
    "created": True,
}

_QUIET = {
    "ok": True,
    "action": "submit",
    "quiet": True,
    "report_run_id": _REPORT,
    "delivery_confirmed": False,
    "created": False,
}

_STATUS = {
    "ok": True,
    "action": "status",
    "quiet": False,
    "found": True,
    "report_run_id": _REPORT,
    "profile": "msft-tracker",
    "content_kind": "cards",
    "content_sha256": "abc",
    "execution_id": _EXECUTION,
    "delivery_confirmed": True,
    "status": "sent",
    "targets": ["feishu:chat:oc_saved"],
    "payload": {"source": "automation_report", "cards": _CARDS},
}

_REFRESH_PENDING = {
    "ok": True,
    "action": "status",
    "quiet": False,
    "found": True,
    "report_run_id": "other-report",
    "profile": "other-profile",
    "content_kind": "failure_text",
    "content_sha256": "fff",
    "execution_id": _EXECUTION_B,
    "delivery_confirmed": False,
    "status": "pending",
    "targets": ["feishu:chat:oc_saved"],
    "payload": {"source": "other"},
    "created": False,
}

_TRANSPORT_ACCEPTANCE = {
    "ok": True,
    "delivery_confirmed": True,
    "status": "sent",
    "targets": ["feishu:chat:oc_transport"],
    "cards": _CARDS,
    "failure_text": "feed down",
    "user_id": _USER,
}


def _config(**overrides: Any) -> dict[str, Any]:
    configurable = {
        "user_id": _USER,
        "workspace_id": _WORKSPACE,
        "thread_id": _THREAD,
        "run_id": _RUN,
        "turn_origin_automation_id": _AUTOMATION,
        "automation_execution_id": _EXECUTION,
    }
    configurable.update(overrides)
    return {"configurable": configurable}


def _tool():
    return create_automation_report_tools()[0]


def _submit_args(**overrides: Any) -> dict[str, Any]:
    args = {
        "action": "submit",
        "report_run_id": _REPORT,
        "material": "yes",
        "cards": _CARDS,
        "failure_text": None,
    }
    args.update(overrides)
    return args


async def _invoke(args: dict[str, Any], **config_overrides: Any) -> dict[str, Any]:
    raw = await _tool().ainvoke(args, config=_config(**config_overrides))
    return json.loads(raw)


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    submit = create_autospec(submit_automation_report, return_value=dict(_PENDING))
    status = create_autospec(
        read_automation_report_status, return_value=dict(_REFRESH_PENDING)
    )
    sender = create_autospec(
        dispatch_automation_report, return_value=dict(_TRANSPORT_ACCEPTANCE)
    )
    allowed = create_autospec(is_internal_or_admin, return_value=True)
    monkeypatch.setattr(report_tools, "submit_automation_report", submit)
    monkeypatch.setattr(report_tools, "read_automation_report_status", status)
    monkeypatch.setattr(report_tools, "dispatch_automation_report", sender)
    monkeypatch.setattr(report_tools, "is_internal_or_admin", allowed)
    return SimpleNamespace(
        submit=submit, status=status, sender=sender, allowed=allowed
    )


def _assert_sender_received_only(storage: SimpleNamespace, execution_id: str) -> None:
    inspect.signature(dispatch_automation_report).bind(execution_id)
    storage.sender.assert_awaited_once_with(execution_id)
    assert storage.sender.await_args.args == (execution_id,)
    assert storage.sender.await_args.kwargs == {}
    rendered = repr(storage.sender.await_args)
    assert "MSFT" not in rendered
    assert "feed down" not in rendered
    assert _USER not in rendered
    assert _WORKSPACE not in rendered
    assert _THREAD not in rendered
    assert _RUN not in rendered
    assert _AUTOMATION not in rendered


def test_schema_takes_only_the_report_arguments() -> None:
    tool = _tool()
    assert set(tool.args) == _CALLER_ARGS
    assert list(inspect.signature(tool.coroutine).parameters) == [
        "action",
        "report_run_id",
        "material",
        "cards",
        "failure_text",
        "config",
    ]
    for name, field in tool.args_schema.model_fields.items():
        assert name in _CALLER_ARGS
        assert not field.description


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("tier", ["internal", "admin", None])
def test_both_roles_mount_the_tool_for_internal_admin_and_unresolved_tier(
    role: str, tier: str | None
) -> None:
    names = {item.name for item in build_dalton_tools(role, user_tier=tier)}
    assert "automation_report" in names


@pytest.mark.parametrize("role", _ROLES)
def test_external_tier_does_not_mount_the_tool(role: str) -> None:
    names = {item.name for item in build_dalton_tools(role, user_tier="external")}
    assert "automation_report" not in names


@pytest.mark.asyncio
async def test_submit_forwards_config_identity_and_returns_pending_saved_batch(
    storage: SimpleNamespace,
) -> None:
    payload = await _invoke(_submit_args())
    assert payload == _PENDING
    assert payload["status"] == "pending"
    assert payload["delivery_confirmed"] is False
    assert "found" not in payload
    assert "dispatch_code" not in payload
    storage.allowed.assert_awaited_once_with(_USER)
    storage.submit.assert_awaited_once()
    kwargs = storage.submit.await_args.kwargs
    inspect.signature(submit_automation_report).bind(**kwargs)
    assert kwargs == {
        "user_id": _USER,
        "workspace_id": _WORKSPACE,
        "thread_id": _THREAD,
        "run_id": _RUN,
        "automation_id": _AUTOMATION,
        "execution_id": _EXECUTION,
        "report_run_id": _REPORT,
        "material": "yes",
        "cards": _CARDS,
        "failure_text": None,
    }
    _assert_sender_received_only(storage, _EXECUTION)
    status_kwargs = storage.status.await_args.kwargs
    inspect.signature(read_automation_report_status).bind(**status_kwargs)
    assert status_kwargs == {
        "user_id": _USER,
        "workspace_id": _WORKSPACE,
        "thread_id": _THREAD,
        "run_id": _RUN,
        "automation_id": _AUTOMATION,
        "execution_id": _EXECUTION,
        "report_run_id": _REPORT,
    }
    assert storage.status.await_count == 1


@pytest.mark.asyncio
async def test_resubmit_sends_the_original_execution_and_reads_the_runtime_one(
    storage: SimpleNamespace,
) -> None:
    saved = dict(_PENDING)
    saved["created"] = False
    saved["execution_id"] = _EXECUTION
    storage.submit.return_value = saved
    payload = await _invoke(
        _submit_args(), automation_execution_id=_EXECUTION_B
    )
    assert storage.submit.await_args.kwargs["execution_id"] == _EXECUTION_B
    _assert_sender_received_only(storage, _EXECUTION)
    assert storage.status.await_args.kwargs["execution_id"] == _EXECUTION_B
    assert payload["action"] == "submit"
    assert payload["created"] is False
    assert payload["execution_id"] == _EXECUTION
    assert payload["report_run_id"] == _REPORT
    assert payload["profile"] == "msft-tracker"
    assert payload["content_kind"] == "cards"
    assert payload["content_sha256"] == "abc"
    assert payload["payload"] == _PENDING["payload"]
    assert payload["delivery_confirmed"] is False
    assert payload["status"] == "pending"
    assert "found" not in payload


@pytest.mark.asyncio
async def test_failure_text_submit_sends_only_the_saved_execution(
    storage: SimpleNamespace,
) -> None:
    saved = dict(_PENDING)
    saved["content_kind"] = "failure_text"
    saved["payload"] = {"source": "automation_report", "failure_text": "feed down"}
    storage.submit.return_value = saved
    payload = await _invoke(
        _submit_args(cards=None, failure_text="feed down")
    )
    assert storage.submit.await_args.kwargs["cards"] is None
    assert storage.submit.await_args.kwargs["failure_text"] == "feed down"
    _assert_sender_received_only(storage, _EXECUTION)
    assert payload["content_kind"] == "failure_text"
    assert payload["payload"]["failure_text"] == "feed down"
    assert payload["action"] == "submit"


@pytest.mark.asyncio
async def test_transport_acceptance_does_not_confirm_delivery(
    storage: SimpleNamespace,
) -> None:
    payload = await _invoke(_submit_args())
    assert payload["delivery_confirmed"] is False
    assert payload["status"] == "pending"
    assert payload["targets"] == ["feishu:chat:oc_saved"]
    assert payload["action"] == "submit"
    assert payload["created"] is True
    assert "dispatch_code" not in payload
    _assert_sender_received_only(storage, _EXECUTION)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_name", ["unknown", "pending", "failed", "partial"])
async def test_unconfirmed_saved_status_stays_false(
    storage: SimpleNamespace, status_name: str
) -> None:
    refreshed = dict(_REFRESH_PENDING)
    refreshed["status"] = status_name
    refreshed["delivery_confirmed"] = True
    storage.status.return_value = refreshed
    payload = await _invoke(_submit_args())
    assert payload["delivery_confirmed"] is False
    assert payload["status"] == status_name
    assert payload["action"] == "submit"
    assert payload["execution_id"] == _EXECUTION
    assert "dispatch_code" not in payload


@pytest.mark.asyncio
async def test_sent_without_the_saved_flag_stays_unconfirmed(
    storage: SimpleNamespace,
) -> None:
    refreshed = dict(_STATUS)
    refreshed["delivery_confirmed"] = False
    refreshed["targets"] = ["feishu:chat:oc_saved", "discord:channel:1"]
    storage.status.return_value = refreshed
    payload = await _invoke(_submit_args())
    assert payload["status"] == "sent"
    assert payload["delivery_confirmed"] is False
    assert payload["targets"] == ["feishu:chat:oc_saved", "discord:channel:1"]
    assert payload["action"] == "submit"
    assert payload["execution_id"] == _EXECUTION
    assert "dispatch_code" not in payload


@pytest.mark.asyncio
async def test_durable_all_sent_read_confirms_the_submit(
    storage: SimpleNamespace,
) -> None:
    refreshed = dict(_STATUS)
    refreshed["execution_id"] = _EXECUTION_B
    refreshed["report_run_id"] = "other-report"
    refreshed["profile"] = "other-profile"
    refreshed["content_sha256"] = "fff"
    refreshed["payload"] = {"source": "other"}
    refreshed["targets"] = ["feishu:chat:oc_saved"]
    storage.status.return_value = refreshed
    payload = await _invoke(_submit_args())
    assert payload["delivery_confirmed"] is True
    assert payload["status"] == "sent"
    assert payload["action"] == "submit"
    assert payload["created"] is True
    assert payload["execution_id"] == _EXECUTION
    assert payload["report_run_id"] == _REPORT
    assert payload["profile"] == "msft-tracker"
    assert payload["content_sha256"] == "abc"
    assert payload["payload"] == _PENDING["payload"]
    assert payload["targets"] == ["feishu:chat:oc_saved"]
    assert payload["quiet"] is False
    assert "found" not in payload
    assert "dispatch_code" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("material", ["no", "no_change", "no-change"])
async def test_quiet_material_is_forwarded_and_creates_no_send(
    storage: SimpleNamespace, material: str
) -> None:
    stored = dict(_QUIET)
    stored["execution_id"] = _EXECUTION
    storage.submit.return_value = stored
    payload = await _invoke(
        {"action": "submit", "report_run_id": _REPORT, "material": material}
    )
    assert payload == stored
    assert payload["delivery_confirmed"] is False
    assert storage.submit.await_args.kwargs["material"] == material
    assert storage.submit.await_args.kwargs["cards"] is None
    assert storage.submit.await_args.kwargs["failure_text"] is None
    storage.sender.assert_not_awaited()
    storage.status.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_returns_durable_confirmation_without_submitting(
    storage: SimpleNamespace,
) -> None:
    storage.status.return_value = dict(_STATUS)
    payload = await _invoke({"action": "status", "report_run_id": _REPORT})
    assert payload == _STATUS
    assert payload["delivery_confirmed"] is True
    storage.submit.assert_not_awaited()
    storage.sender.assert_not_awaited()
    kwargs = storage.status.await_args.kwargs
    inspect.signature(read_automation_report_status).bind(**kwargs)
    assert kwargs == {
        "user_id": _USER,
        "workspace_id": _WORKSPACE,
        "thread_id": _THREAD,
        "run_id": _RUN,
        "automation_id": _AUTOMATION,
        "execution_id": _EXECUTION,
        "report_run_id": _REPORT,
    }


@pytest.mark.asyncio
async def test_unconfirmed_status_stays_unconfirmed(storage: SimpleNamespace) -> None:
    stored = dict(_STATUS)
    stored["delivery_confirmed"] = False
    stored["status"] = "pending"
    storage.status.return_value = stored
    payload = await _invoke({"action": "status", "report_run_id": _REPORT})
    assert payload["delivery_confirmed"] is False
    assert payload["status"] == "pending"
    storage.submit.assert_not_awaited()
    storage.sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_read_failure_is_not_a_send(storage: SimpleNamespace) -> None:
    storage.status.side_effect = RuntimeError("token sk-secret")
    payload = await _invoke({"action": "status", "report_run_id": _REPORT})
    assert payload["ok"] is False
    assert payload["action"] == "status"
    assert payload["code"] == "report_unavailable"
    assert payload["delivery_confirmed"] is False
    assert "sk-secret" not in payload["error"]
    assert "RuntimeError" not in payload["error"]
    storage.submit.assert_not_awaited()
    storage.sender.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing",
    [
        "user_id",
        "workspace_id",
        "thread_id",
        "run_id",
        "turn_origin_automation_id",
        "automation_execution_id",
    ],
)
async def test_missing_server_identity_is_refused(
    storage: SimpleNamespace, missing: str
) -> None:
    payload = await _invoke(
        _submit_args(), **{missing: "  "}
    )
    assert payload["ok"] is False
    assert payload["code"] == "not_automation_run"
    assert payload["delivery_confirmed"] is False
    storage.allowed.assert_not_awaited()
    storage.submit.assert_not_awaited()
    storage.status.assert_not_awaited()
    storage.sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_turn_without_automation_identity_is_refused(
    storage: SimpleNamespace,
) -> None:
    config = _config()
    del config["configurable"]["turn_origin_automation_id"]
    del config["configurable"]["automation_execution_id"]
    payload = json.loads(
        await _tool().ainvoke(
            {"action": "status", "report_run_id": _REPORT},
            config=config,
        )
    )
    assert payload["code"] == "not_automation_run"
    storage.submit.assert_not_awaited()
    storage.status.assert_not_awaited()
    storage.sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_id_fills_a_missing_run_id(storage: SimpleNamespace) -> None:
    config = _config()
    del config["configurable"]["run_id"]
    config["configurable"]["response_id"] = "run-from-response"
    await _tool().ainvoke(
        {"action": "status", "report_run_id": _REPORT},
        config=config,
    )
    assert storage.status.await_args.kwargs["run_id"] == "run-from-response"
    storage.sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_run_id_wins_over_response_id(storage: SimpleNamespace) -> None:
    config = _config()
    config["configurable"]["response_id"] = "other-run"
    await _tool().ainvoke(
        {"action": "status", "report_run_id": _REPORT},
        config=config,
    )
    assert storage.status.await_args.kwargs["run_id"] == _RUN
    storage.sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_member_is_refused_at_runtime(storage: SimpleNamespace) -> None:
    storage.allowed.return_value = False
    payload = await _invoke(_submit_args())
    assert payload["ok"] is False
    assert payload["code"] == "not_allowed"
    assert payload["delivery_confirmed"] is False
    storage.allowed.assert_awaited_once_with(_USER)
    storage.submit.assert_not_awaited()
    storage.status.assert_not_awaited()
    storage.sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_forged_identity_arguments_cannot_replace_the_server_config(
    storage: SimpleNamespace,
) -> None:
    forged = {
        "action": "submit",
        "report_run_id": _REPORT,
        "material": "yes",
        "cards": _CARDS,
        "user_id": "forged-user",
        "workspace_id": "forged-ws",
        "thread_id": "forged-thread",
        "run_id": "forged-run",
        "automation_id": "forged-auto",
        "execution_id": "forged-exec",
        "turn_origin_automation_id": "forged-origin",
        "automation_execution_id": "forged-execution",
    }
    try:
        payload = json.loads(await _tool().ainvoke(forged, config=_config()))
    except Exception:
        storage.submit.assert_not_awaited()
        storage.sender.assert_not_awaited()
    else:
        assert payload["delivery_confirmed"] is False
        assert payload["action"] == "submit"
        assert payload["execution_id"] == _EXECUTION
        kwargs = storage.submit.await_args.kwargs
        assert kwargs["user_id"] == _USER
        assert kwargs["workspace_id"] == _WORKSPACE
        assert kwargs["thread_id"] == _THREAD
        assert kwargs["run_id"] == _RUN
        assert kwargs["automation_id"] == _AUTOMATION
        assert kwargs["execution_id"] == _EXECUTION
        _assert_sender_received_only(storage, _EXECUTION)
    with pytest.raises(TypeError):
        await _tool().coroutine(
            "submit",
            _REPORT,
            "yes",
            config=_config(),
            user_id="forged-user",
            execution_id="forged-exec",
        )


@pytest.mark.asyncio
async def test_invalid_action_and_missing_material_do_not_call_storage(
    storage: SimpleNamespace,
) -> None:
    tool = _tool()
    invalid = json.loads(
        await tool.coroutine("send", _REPORT, "yes", config=_config())
    )
    assert invalid["code"] == "invalid_action"
    missing = json.loads(
        await tool.coroutine("submit", _REPORT, config=_config())
    )
    assert missing["code"] == "invalid_material"
    storage.submit.assert_not_awaited()
    storage.status.assert_not_awaited()
    storage.sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_storage_failure_is_not_reported_as_sent(storage: SimpleNamespace) -> None:
    storage.submit.side_effect = RuntimeError("db down")
    payload = await _invoke(_submit_args())
    assert payload["ok"] is False
    assert payload["code"] == "report_unavailable"
    assert payload["delivery_confirmed"] is False
    assert "db down" not in payload["error"]
    storage.sender.assert_not_awaited()
    storage.status.assert_not_awaited()


@pytest.mark.asyncio
async def test_refused_storage_result_does_not_send(storage: SimpleNamespace) -> None:
    storage.submit.return_value = {
        "ok": False,
        "action": "submit",
        "code": "content_conflict",
        "delivery_confirmed": False,
        "error": "conflict",
        "execution_id": _EXECUTION,
    }
    payload = await _invoke(_submit_args())
    assert payload["ok"] is False
    assert payload["code"] == "content_conflict"
    storage.sender.assert_not_awaited()
    storage.status.assert_not_awaited()


@pytest.mark.asyncio
async def test_blank_saved_execution_does_not_send(storage: SimpleNamespace) -> None:
    saved = dict(_PENDING)
    saved["execution_id"] = "  "
    storage.submit.return_value = saved
    payload = await _invoke(_submit_args())
    assert payload["execution_id"] == "  "
    assert payload["ok"] is True
    assert "dispatch_code" not in payload
    storage.sender.assert_not_awaited()
    storage.status.assert_not_awaited()


@pytest.mark.asyncio
async def test_sender_error_keeps_the_saved_batch_pending(
    storage: SimpleNamespace,
) -> None:
    storage.sender.side_effect = RuntimeError("token sk-secret")
    payload = await _invoke(_submit_args())
    assert payload["ok"] is True
    assert payload["action"] == "submit"
    assert payload["created"] is True
    assert payload["execution_id"] == _EXECUTION
    assert payload["report_run_id"] == _REPORT
    assert payload["profile"] == "msft-tracker"
    assert payload["content_sha256"] == "abc"
    assert payload["payload"] == _PENDING["payload"]
    assert payload["delivery_confirmed"] is False
    assert payload["status"] == "pending"
    assert payload["dispatch_code"] == "dispatch_pending"
    assert "error" not in payload
    rendered = json.dumps(payload)
    assert "sk-secret" not in rendered
    assert "RuntimeError" not in rendered
    storage.status.assert_not_awaited()


@pytest.mark.asyncio
async def test_sender_error_keeps_an_already_confirmed_saved_batch(
    storage: SimpleNamespace,
) -> None:
    saved = dict(_PENDING)
    saved["delivery_confirmed"] = True
    saved["status"] = "sent"
    saved["created"] = False
    storage.submit.return_value = saved
    storage.sender.side_effect = RuntimeError("token sk-secret")
    payload = await _invoke(_submit_args())
    assert payload["ok"] is True
    assert payload["action"] == "submit"
    assert payload["created"] is False
    assert payload["execution_id"] == _EXECUTION
    assert payload["delivery_confirmed"] is True
    assert payload["status"] == "sent"
    assert "dispatch_code" not in payload
    assert "error" not in payload
    assert "sk-secret" not in json.dumps(payload)
    storage.status.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_refresh_failure_does_not_claim_delivery(
    storage: SimpleNamespace,
) -> None:
    storage.status.side_effect = RuntimeError("dsn password=secret")
    payload = await _invoke(_submit_args())
    assert payload["ok"] is True
    assert payload["action"] == "submit"
    assert payload["created"] is True
    assert payload["execution_id"] == _EXECUTION
    assert payload["delivery_confirmed"] is False
    assert payload["status"] == "pending"
    assert payload["targets"] == ["feishu:chat:oc_saved"]
    assert payload["dispatch_code"] == "dispatch_pending"
    assert "error" not in payload
    rendered = json.dumps(payload)
    assert "password" not in rendered
    assert "secret" not in rendered
    assert "RuntimeError" not in rendered
    _assert_sender_received_only(storage, _EXECUTION)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refreshed",
    [
        {
            "ok": True,
            "found": False,
            "delivery_confirmed": True,
            "status": "sent",
            "targets": ["feishu:chat:oc_transport"],
            "error": "password=secret",
        },
        {
            "ok": False,
            "found": True,
            "delivery_confirmed": True,
            "status": "sent",
            "error": "password=secret",
        },
        {
            "ok": True,
            "delivery_confirmed": True,
            "status": "sent",
            "targets": ["feishu:chat:oc_transport"],
        },
        "not-a-mapping",
    ],
)
async def test_unusable_status_refresh_keeps_the_saved_batch(
    storage: SimpleNamespace, refreshed: Any
) -> None:
    storage.status.return_value = refreshed
    payload = await _invoke(_submit_args())
    assert payload["ok"] is True
    assert payload["action"] == "submit"
    assert payload["created"] is True
    assert payload["execution_id"] == _EXECUTION
    assert payload["report_run_id"] == _REPORT
    assert payload["payload"] == _PENDING["payload"]
    assert payload["delivery_confirmed"] is False
    assert payload["status"] == "pending"
    assert payload["targets"] == ["feishu:chat:oc_saved"]
    assert payload["dispatch_code"] == "dispatch_pending"
    rendered = json.dumps(payload)
    assert "password" not in rendered
    assert "secret" not in rendered
    assert "oc_transport" not in rendered


@pytest.mark.asyncio
async def test_submit_cancellation_propagates(storage: SimpleNamespace) -> None:
    storage.submit.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await _invoke(_submit_args())
    storage.sender.assert_not_awaited()
    storage.status.assert_not_awaited()


@pytest.mark.asyncio
async def test_sender_cancellation_propagates(storage: SimpleNamespace) -> None:
    storage.sender.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await _invoke(_submit_args())
    storage.status.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_cancellation_propagates(storage: SimpleNamespace) -> None:
    storage.status.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await _invoke(_submit_args())
    _assert_sender_received_only(storage, _EXECUTION)


@pytest.mark.asyncio
async def test_status_cancellation_propagates(storage: SimpleNamespace) -> None:
    storage.status.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await _invoke({"action": "status", "report_run_id": _REPORT})
    storage.submit.assert_not_awaited()
    storage.sender.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_id", ["meta-tracker", "amzn-tracker", "googl-tracker"])
async def test_saved_company_tracker_profile_is_echoed(
    storage: SimpleNamespace, profile_id: str
) -> None:
    saved = dict(_PENDING)
    saved["profile"] = profile_id
    storage.submit.return_value = saved
    payload = await _invoke(_submit_args())
    assert payload["profile"] == profile_id
    assert payload["content_kind"] == "cards"
    assert payload["status"] == "pending"
    assert payload["delivery_confirmed"] is False
    assert payload["report_run_id"] == _REPORT
