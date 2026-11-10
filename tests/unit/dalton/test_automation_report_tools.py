"""The automation_report tool forwards server identity and returns the saved batch.

This stage does not send. Storage is autospec'd at its real signature.
"""

from __future__ import annotations

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
from src.dalton.tools import build_dalton_tools
from src.dalton.tools.automation_report_tools import (
    create_automation_report_tools,
)
from src.dalton.tools import automation_report_tools as report_tools

_USER = "user-1"
_WORKSPACE = "ws-1"
_THREAD = "thread-1"
_RUN = "run-1"
_AUTOMATION = "22222222-2222-4222-8222-222222222222"
_EXECUTION = "11111111-1111-4111-8111-111111111111"
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


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    submit = create_autospec(submit_automation_report, return_value=dict(_PENDING))
    status = create_autospec(read_automation_report_status, return_value=dict(_STATUS))
    allowed = create_autospec(is_internal_or_admin, return_value=True)
    monkeypatch.setattr(report_tools, "submit_automation_report", submit)
    monkeypatch.setattr(report_tools, "read_automation_report_status", status)
    monkeypatch.setattr(report_tools, "is_internal_or_admin", allowed)
    return SimpleNamespace(submit=submit, status=status, allowed=allowed)


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
    source = inspect.getsource(report_tools)
    assert "post_data_task" not in source
    assert "claim_target" not in source
    assert "dispatch" not in source


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
    raw = await _tool().ainvoke(
        {
            "action": "submit",
            "report_run_id": _REPORT,
            "material": "yes",
            "cards": _CARDS,
            "failure_text": None,
        },
        config=_config(),
    )
    payload = json.loads(raw)
    assert payload == _PENDING
    assert payload["status"] == "pending"
    assert payload["delivery_confirmed"] is False
    storage.allowed.assert_awaited_once_with(_USER)
    storage.status.assert_not_awaited()
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


@pytest.mark.asyncio
async def test_quiet_material_is_forwarded_and_creates_no_send(
    storage: SimpleNamespace,
) -> None:
    storage.submit.return_value = dict(_QUIET)
    payload = json.loads(
        await _tool().ainvoke(
            {
                "action": "submit",
                "report_run_id": _REPORT,
                "material": "no-change",
            },
            config=_config(),
        )
    )
    assert payload == _QUIET
    assert payload["delivery_confirmed"] is False
    assert storage.submit.await_args.kwargs["material"] == "no-change"
    assert storage.submit.await_args.kwargs["cards"] is None
    assert storage.submit.await_args.kwargs["failure_text"] is None
    storage.status.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_returns_durable_confirmation_without_submitting(
    storage: SimpleNamespace,
) -> None:
    payload = json.loads(
        await _tool().ainvoke(
            {"action": "status", "report_run_id": _REPORT},
            config=_config(),
        )
    )
    assert payload == _STATUS
    assert payload["delivery_confirmed"] is True
    storage.submit.assert_not_awaited()
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
    payload = json.loads(
        await _tool().ainvoke(
            {"action": "status", "report_run_id": _REPORT},
            config=_config(),
        )
    )
    assert payload["delivery_confirmed"] is False
    assert payload["status"] == "pending"
    storage.submit.assert_not_awaited()


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
    payload = json.loads(
        await _tool().ainvoke(
            {"action": "submit", "report_run_id": _REPORT, "material": "yes"},
            config=_config(**{missing: "  "}),
        )
    )
    assert payload["ok"] is False
    assert payload["code"] == "not_automation_run"
    assert payload["delivery_confirmed"] is False
    storage.allowed.assert_not_awaited()
    storage.submit.assert_not_awaited()
    storage.status.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_explicit_run_id_wins_over_response_id(storage: SimpleNamespace) -> None:
    config = _config()
    config["configurable"]["response_id"] = "other-run"
    await _tool().ainvoke(
        {"action": "status", "report_run_id": _REPORT},
        config=config,
    )
    assert storage.status.await_args.kwargs["run_id"] == _RUN


@pytest.mark.asyncio
async def test_external_member_is_refused_at_runtime(storage: SimpleNamespace) -> None:
    storage.allowed.return_value = False
    payload = json.loads(
        await _tool().ainvoke(
            {
                "action": "submit",
                "report_run_id": _REPORT,
                "material": "yes",
                "cards": _CARDS,
            },
            config=_config(),
        )
    )
    assert payload["ok"] is False
    assert payload["code"] == "not_allowed"
    assert payload["delivery_confirmed"] is False
    storage.allowed.assert_awaited_once_with(_USER)
    storage.submit.assert_not_awaited()
    storage.status.assert_not_awaited()


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
    else:
        assert payload["delivery_confirmed"] is False
        kwargs = storage.submit.await_args.kwargs
        assert kwargs["user_id"] == _USER
        assert kwargs["workspace_id"] == _WORKSPACE
        assert kwargs["thread_id"] == _THREAD
        assert kwargs["run_id"] == _RUN
        assert kwargs["automation_id"] == _AUTOMATION
        assert kwargs["execution_id"] == _EXECUTION
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


@pytest.mark.asyncio
async def test_storage_failure_is_not_reported_as_sent(storage: SimpleNamespace) -> None:
    storage.submit.side_effect = RuntimeError("db down")
    payload = json.loads(
        await _tool().ainvoke(
            {"action": "submit", "report_run_id": _REPORT, "material": "yes"},
            config=_config(),
        )
    )
    assert payload["ok"] is False
    assert payload["code"] == "report_unavailable"
    assert payload["delivery_confirmed"] is False
    assert "db down" not in payload["error"]
