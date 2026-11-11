"""Auth, confirmed send, and no extra writes for market_digest_output."""

from __future__ import annotations

import hashlib
import inspect
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec
from uuid import UUID

import httpx
import pytest

from src.dalton.data_jobs.notification_payloads import markdown_to_card_elements
from src.dalton.database.automation_reports import automation_report_content_sha256
from src.dalton.database.market_digest import authorize_market_digest_execution
from src.dalton.tools import market_digest_output_tools as tools
from src.dalton.tools.automation_report_tools import create_automation_report_tools

pytestmark = pytest.mark.asyncio

_DATE = "2026-11-10"
_USER = "12345678-1234-4123-8123-123456789abc"
_RUN = "response-run-1"
_EXEC = "exec-current"
_ORIG = "exec-original"
_TOKEN = "test-admin-token"
_BASE = "http://bridge.test"
_PROFILE = "market-digest-am"
_RUN_ID = f"market-digest:AM:{_DATE}"
_TITLE = f"\U0001f4ca \u5e02\u573a\u6668\u62a5 \u2014 {_DATE}"
_REPORT = create_automation_report_tools()[0].coroutine
_AUTH = {"ok": True, "code": "ok", "profile": _PROFILE, "period": "AM"}


def _markdown(source: str = "wire") -> str:
    return (
        f"# {_TITLE}\n"
        "## \u4eba\u6c11\u5e01 na\u00efve\n"
        "prices\n"
        "## \U0001f4dd \u6765\u6e90\n"
        f"{source}\n"
    )


def _card(markdown: str) -> dict[str, Any]:
    return markdown_to_card_elements(markdown=markdown, title=_TITLE)


def _digest(markdown: str) -> str:
    return automation_report_content_sha256(
        profile=_PROFILE,
        content_kind="cards",
        cards=[_card(markdown)],
        failure_text=None,
    )


def _stored(markdown: str) -> dict[str, Any]:
    raw = markdown.encode()
    return {
        "ok": True,
        "code": "ok",
        "report_run_id": _RUN_ID,
        "period": "AM",
        "date": _DATE,
        "automation_execution_id": _ORIG,
        "prepared_execution_id": "ab" * 16,
        "body_sha256": hashlib.sha256(raw).hexdigest(),
        "input_sha256": "cd" * 32,
        "byte_count": len(raw),
        "section_count": 2,
        "source_line_count": 1,
        "archive_status": "pending",
        "embedding_status": "pending",
        "index_status": "pending",
        "chunk_count": None,
        "markdown": markdown,
    }


def _finalized(record: dict[str, Any]) -> dict[str, Any]:
    body = {
        "ok": True,
        "code": "finalized",
        "archive_status": "done",
        "embedding_status": "ready",
        "index_status": "done",
        "chunk_count": 2,
        "archive": {"status": "written"},
        "embedding": {"status": "embedded", "code": "ok"},
        "index": {"status": "committed", "code": "ok", "chunk_count": 2},
    }
    for key in ("report_run_id", "period", "date", "body_sha256"):
        body[key] = record[key]
    return body


def _status(**fields: Any) -> str:
    body: dict[str, Any] = {
        "ok": True,
        "action": "status",
        "found": False,
        "delivery_confirmed": False,
    }
    body.update(fields)
    return json.dumps(body)


def _submit_accept() -> str:
    # Real submit JSON has no found. Acceptance is not delivery.
    return json.dumps({"ok": True, "action": "submit", "quiet": False, "status": "pending"})


def _confirmed(markdown: str) -> str:
    return _status(
        found=True,
        status="sent",
        delivery_confirmed=True,
        content_kind="cards",
        content_sha256=_digest(markdown),
        report_run_id=_RUN_ID,
        profile=_PROFILE,
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    owner: str = _USER,
    allowed: bool = True,
    auth: dict[str, Any] | None = None,
) -> SimpleNamespace:
    world = SimpleNamespace(
        requests=[], calls=[], trace=[], steps=[], reports=[], sent=[]
    )
    mocked = {
        "get_market_digest_owner_user_id": owner,
        "is_internal_or_admin": allowed,
        "authorize_market_digest_execution": auth or _AUTH,
        "get_dalton_bridge_base_url": _BASE,
        "get_dalton_bridge_admin_token": _TOKEN,
    }
    for name, value in mocked.items():
        spec = create_autospec(getattr(tools, name), return_value=value)
        monkeypatch.setattr(tools, name, spec)
        setattr(world, name, spec)
    world.auth = world.authorize_market_digest_execution
    world.allowed = world.is_internal_or_admin
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        world.requests.append(request)
        route = request.url.path.rsplit("/", 1)[-1]
        world.trace.append(("http", route))
        step = world.steps.pop(0)
        status = 404 if step.get("code") == "output_missing" else 200
        return httpx.Response(status, json=step)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return real(
            transport=httpx.MockTransport(handler),
            timeout=kwargs.get("timeout"),
            trust_env=False,
            follow_redirects=False,
        )

    monkeypatch.setattr(
        tools.httpx, "AsyncClient", create_autospec(real, side_effect=factory)
    )

    def report(**kwargs: Any) -> str:
        raw = world.reports.pop(0)
        world.sent.append(raw)
        world.calls.append(kwargs)
        world.trace.append(("report", kwargs["action"]))
        return raw

    coroutine = create_autospec(_REPORT, side_effect=report)
    world.factory = create_autospec(
        tools.create_automation_report_tools,
        return_value=[SimpleNamespace(coroutine=coroutine)],
    )
    monkeypatch.setattr(tools, "create_automation_report_tools", world.factory)
    return world


def _config() -> dict[str, Any]:
    return {
        "configurable": {
            "user_id": _USER.upper(),
            "workspace_id": "ws",
            "thread_id": "th",
            "response_id": _RUN,
            "turn_origin_automation_id": "auto",
            "automation_execution_id": _EXEC,
        }
    }


async def _run(action: str, markdown: str | None = None) -> tuple[str, dict[str, Any]]:
    tool = tools.create_market_digest_output_tools()[0]
    raw = await tool.ainvoke(
        {"action": action, "period": "AM", "markdown": markdown},
        config=_config(),
    )
    return raw, json.loads(raw)


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("owner", "owner_mismatch"),
        ("external", "not_allowed"),
        ("profile", "profile_mismatch"),
    ],
)
async def test_refusal_makes_no_http(
    monkeypatch: pytest.MonkeyPatch, case: str, code: str
) -> None:
    owner = _USER if case != "owner" else "ffffffff-ffff-4fff-8fff-ffffffffffff"
    auth = _AUTH
    if case == "profile":
        auth = {**_AUTH, "profile": "market-digest-pm", "error": "/tmp/secret-auth.md"}
    world = _install(monkeypatch, owner=owner, allowed=case != "external", auth=auth)
    raw, payload = await _run("submit", _markdown())
    assert world.requests == []
    assert world.factory.call_count == 0
    assert payload["ok"] is False
    assert payload["code"] == code
    assert "/tmp/" not in raw
    if case == "owner":
        world.allowed.assert_not_called()
        world.auth.assert_not_called()
    elif case == "external":
        world.auth.assert_not_called()
    else:
        world.auth.assert_awaited()


async def test_submit_finalizes_only_after_confirming_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert str(UUID(_USER.upper())) == _USER
    markdown = _markdown()
    record = _stored(markdown)
    digest = _digest(markdown)
    world = _install(monkeypatch)
    world.steps.extend(
        [
            {"ok": False, "code": "output_missing"},
            {"ok": True, "code": "saved"},
            record,
            _finalized(record),
        ]
    )
    world.reports.extend([_status(found=False), _submit_accept(), _confirmed(markdown)])
    raw, payload = await _run("submit", markdown)
    assert world.trace == [
        ("http", "get"),
        ("http", "save"),
        ("http", "get"),
        ("report", "status"),
        ("report", "submit"),
        ("report", "status"),
        ("http", "finalize"),
    ]
    assert json.loads(world.sent[2]) == json.loads(_confirmed(markdown))
    assert "found" not in json.loads(world.sent[1])
    submit = world.calls[1]
    inspect.signature(_REPORT).bind(**submit)
    assert submit["material"] == "yes"
    assert submit["cards"] == [_card(markdown)]
    assert submit["report_run_id"] == _RUN_ID
    assert "failure_text" not in submit
    saved = json.loads(world.requests[1].content)
    assert saved["markdown"] == markdown
    assert saved["automation_execution_id"] == _EXEC
    assert set(saved) == {"period", "automation_execution_id", "markdown"}
    finalized = json.loads(world.requests[3].content)
    assert finalized["body_sha256"] == record["body_sha256"]
    assert finalized["automation_execution_id"] == _EXEC
    assert payload["stage"] == "done"
    assert payload["finalized"] is True
    assert payload["report_delivery_confirmed"] is True
    assert payload["report_status"] == "sent"
    assert payload["content_sha256"] == digest
    assert payload["body_sha256"] == record["body_sha256"]
    assert payload["section_count"] == 2
    assert payload["source_line_count"] == 1
    auth_kwargs = world.auth.await_args.kwargs
    inspect.signature(authorize_market_digest_execution).bind(**auth_kwargs)
    assert auth_kwargs["user_id"] == _USER.upper()
    assert auth_kwargs["run_id"] == _RUN
    assert auth_kwargs["execution_id"] == _EXEC
    assert "conn" not in auth_kwargs
    world.allowed.assert_awaited_once_with(_USER.upper())
    assert _TOKEN not in raw and _EXEC not in raw and _ORIG not in raw


@pytest.mark.parametrize("case", ["pending", "wrong_id", "wrong_kind", "wrong_hash"])
async def test_unconfirmed_report_never_finalizes(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    markdown = _markdown()
    base = json.loads(_confirmed(markdown))
    world = _install(monkeypatch)
    world.steps.append(_stored(markdown))
    if case == "pending":
        pending = {**base, "status": "pending", "delivery_confirmed": False}
        world.reports.extend([_status(**pending), _submit_accept(), _status(**pending)])
    else:
        override = {
            "wrong_id": {"report_run_id": "market-digest:AM:2020-01-01"},
            "wrong_kind": {"content_kind": "failure_text"},
            "wrong_hash": {"content_sha256": "ab" * 32},
        }[case]
        world.reports.append(_status(**{**base, **override}))
    _raw, payload = await _run("submit")
    assert ("http", "finalize") not in world.trace
    assert payload["finalized"] is False
    assert json.loads(world.sent[0])["found"] is True
    actions = [call["action"] for call in world.calls]
    if case == "pending":
        assert actions == ["status", "submit", "status"]
        assert "found" not in json.loads(world.sent[1])
        assert payload["report_status"] == "pending"
        assert payload["report_delivery_confirmed"] is False
    else:
        assert actions == ["status"]


@pytest.mark.parametrize("action", ["status", "finalize"])
async def test_sent_body_is_not_submitted_again(
    monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    markdown = _markdown()
    record = _stored(markdown)
    world = _install(monkeypatch)
    world.steps.append(record)
    if action == "finalize":
        world.steps.append(_finalized(record))
    world.reports.append(_confirmed(markdown))
    _raw, payload = await _run(action)
    assert [call["action"] for call in world.calls] == ["status"]
    assert ("http", "save") not in world.trace
    assert ("report", "submit") not in world.trace
    assert payload["report_status"] == "sent"
    assert payload["report_delivery_confirmed"] is True
    if action == "status":
        assert world.trace == [("http", "get"), ("report", "status")]
        assert payload["stage"] == "archive_pending"
        assert payload["finalized"] is False
        return
    assert world.trace[-1] == ("http", "finalize")
    assert payload["stage"] == "done"
    assert payload["finalized"] is True
    sent = json.loads(world.requests[-1].content)
    assert sent["body_sha256"] == record["body_sha256"]
    assert sent["automation_execution_id"] == _EXEC


async def test_different_body_is_not_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    stored_md = _markdown("stored-line")
    record = _stored(stored_md)
    world = _install(monkeypatch)
    world.steps.append(record)
    raw, payload = await _run("submit", _markdown("caller-line"))
    assert world.trace == [("http", "get")]
    assert world.factory.call_count == 0
    assert payload["code"] == "body_conflict"
    assert payload["body_sha256"] == record["body_sha256"]
    assert payload["byte_count"] == len(stored_md.encode())
    assert "stored-line" not in raw
    assert "caller-line" not in raw


@pytest.mark.parametrize("marker", ["## AM", "## PM"])
async def test_period_marker_line_is_not_sent(
    monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    source = "## \U0001f4dd \u6765\u6e90\n"
    markdown = _markdown().replace(source, f"{marker}\r\n{source}", 1)
    record = _stored(markdown)
    record["section_count"] = sum(
        line.startswith("## ") for line in markdown.splitlines()
    )
    world = _install(monkeypatch)
    world.steps.append(record)
    _raw, payload = await _run("submit")
    assert world.trace == [("http", "get")]
    assert world.factory.call_count == 0
    assert world.calls == []
    assert payload["code"] == "body_invalid"
    assert payload["stage"] == "body_invalid"
    assert payload["finalized"] is False
