"""Prepare and read wiring for the market-digest tool and admin HTTP route.

Runners are signature-correct substitutes. No test fetches mail, starts a
service, or reads a production credential store.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402
from scripts.dalton import market_digest_inputs as inputs  # noqa: E402
from src.dalton.access_tiers import is_internal_or_admin  # noqa: E402
from src.dalton.database import market_digest as market_digest_db  # noqa: E402
from src.dalton.database.automation_reports import lock_report_execution  # noqa: E402
from src.dalton.settings import (  # noqa: E402
    get_dalton_bridge_admin_token,
    get_dalton_bridge_base_url,
    get_market_digest_owner_user_id,
)
from src.dalton.tools import market_digest_tools as tools  # noqa: E402
from tests.unit.dalton import test_market_digest_inputs as prep  # noqa: E402

BRIDGE_TOKEN = "bridge-mcp-token-secret"
ADMIN_TOKEN = "bridge-admin-token-secret"
USER = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
OWNER = USER.lower()
WORKSPACE = "22222222-2222-4222-8222-222222222222"
THREAD = "33333333-3333-4333-8333-333333333333"
RUN = "44444444-4444-4444-8444-444444444444"
AUTOMATION = "55555555-5555-4555-8555-555555555555"
EXECUTION = "BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB"
HOST_EXECUTION = "abcdef66-6666-4666-8666-666666666666"
OTHER_OWNER = "99999999-9999-4999-8999-999999999999"
ATTEMPT_ID = "ab" * 16
INPUT_HASH = "cd" * 32
NEXT = datetime(2026, 11, 11, 9, 0, tzinfo=inputs.HKT)


class _Clock(datetime):
    current = prep.NOW

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        if tz is None:
            return cls.current
        return cls.current.astimezone(tz)


class _NullCM:
    async def __aenter__(self) -> _NullCM:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _Conn:
    def transaction(self) -> _NullCM:
        return _NullCM()

    def cursor(self, row_factory: Any = None) -> _NullCM:
        del row_factory
        return _NullCM()


class _Pool:
    """One-slot stand-in for a max_size=1 database pool."""

    def __init__(self) -> None:
        self.held = 0
        self.max_held = 0

    def __call__(self, conn: Any = None) -> Any:
        del conn
        pool = self

        class _CM:
            async def __aenter__(self) -> _Conn:
                if pool.held != 0:
                    raise RuntimeError("max_size=1 exhausted")
                pool.held += 1
                pool.max_held = max(pool.max_held, pool.held)
                return _Conn()

            async def __aexit__(self, *_args: object) -> bool:
                pool.held -= 1
                return False

        return _CM()


class _Response:
    def __init__(self, status: int, payload: MappingLike, headers: dict[str, str] | None) -> None:
        self.status_code = status
        self.headers = headers or {}
        self._raw = json.dumps(payload).encode("utf-8")

    async def aiter_bytes(self):  # noqa: ANN201
        yield self._raw


class _Stream:
    def __init__(self, response: _Response) -> None:
        self._response = response

    async def __aenter__(self) -> _Response:
        return self._response

    async def __aexit__(self, *_args: object) -> bool:
        return False


MappingLike = dict[str, Any]


class _HTTP:
    def __init__(self, pool: _Pool) -> None:
        self.pool = pool
        self.calls: list[dict[str, Any]] = []
        self.payload: MappingLike = {}
        self.status = 200
        self.error: BaseException | None = None
        self.headers: dict[str, str] | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        del args
        self.calls.append(
            {
                "timeout": kwargs.get("timeout"),
                "trust_env": kwargs.get("trust_env"),
                "held": self.pool.held,
            }
        )
        parent = self

        class _Client:
            async def __aenter__(self) -> _Client:
                return self

            async def __aexit__(self, *_args: object) -> bool:
                return False

            def stream(self, method: str, url: str, json: Any = None, headers: Any = None) -> _Stream:
                parent.calls[-1].update(
                    {"method": method, "url": url, "json": json, "headers": headers}
                )
                if parent.error is not None:
                    raise parent.error
                return _Stream(_Response(parent.status, parent.payload, parent.headers))

        return _Client()


def _freeze(monkeypatch: pytest.MonkeyPatch) -> type[_Clock]:
    _Clock.current = prep.NOW
    monkeypatch.setattr(inputs, "datetime", _Clock)
    return _Clock


def _row(profile: str) -> dict[str, Any]:
    return {
        "automation_execution_id": EXECUTION,
        "automation_id": AUTOMATION,
        "status": "running",
        "conversation_thread_id": THREAD,
        "conversation_response_id": RUN,
        "user_id": USER,
        "workspace_id": WORKSPACE,
        "metadata": {
            "report_profile": {
                "profile": profile,
                "report_targets": ["feishu:chat:oc_marketdigest"],
                "failure_targets": [],
            }
        },
    }


def _config(**overrides: Any) -> dict[str, Any]:
    configurable = {
        "user_id": USER,
        "workspace_id": WORKSPACE,
        "thread_id": THREAD,
        "run_id": RUN,
        "turn_origin_automation_id": AUTOMATION,
        "automation_execution_id": EXECUTION,
    }
    configurable.update(overrides)
    return {"configurable": configurable}


def _install_tool(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    pool = _Pool()
    http = _HTTP(pool)
    seen: list[dict[str, Any]] = []
    owner_calls: list[str | None] = []
    internal_ids: list[str | None] = []
    profile = ["market-digest-am"]
    owner_value: list[str | None] = [OWNER]
    internal_open = [True]
    real_auth = market_digest_db.authorize_market_digest_execution

    async def _lock(cur: Any, execution_id: str, *, for_update: bool) -> dict[str, Any]:
        del cur
        assert for_update is False
        assert execution_id == EXECUTION
        return _row(profile[0])

    async def _auth(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return await real_auth(**kwargs)

    async def _internal(user_id: str | None) -> bool:
        internal_ids.append(user_id)
        return internal_open[0]

    def _owner() -> str | None:
        owner_calls.append(owner_value[0])
        return owner_value[0]

    monkeypatch.setattr(market_digest_db, "get_db_connection", pool)
    monkeypatch.setattr(
        market_digest_db,
        "lock_report_execution",
        create_autospec(lock_report_execution, side_effect=_lock),
    )
    monkeypatch.setattr(
        tools,
        "authorize_market_digest_execution",
        create_autospec(real_auth, side_effect=_auth),
    )
    monkeypatch.setattr(
        tools,
        "is_internal_or_admin",
        create_autospec(is_internal_or_admin, side_effect=_internal),
    )
    monkeypatch.setattr(
        tools,
        "get_market_digest_owner_user_id",
        create_autospec(get_market_digest_owner_user_id, side_effect=_owner),
    )
    monkeypatch.setattr(
        tools,
        "get_dalton_bridge_admin_token",
        create_autospec(get_dalton_bridge_admin_token, return_value=ADMIN_TOKEN),
    )
    monkeypatch.setattr(
        tools,
        "get_dalton_bridge_base_url",
        create_autospec(get_dalton_bridge_base_url, return_value="http://bridge.invalid"),
    )
    monkeypatch.setattr(tools.httpx, "AsyncClient", http)
    return SimpleNamespace(
        pool=pool,
        http=http,
        seen=seen,
        owner_calls=owner_calls,
        internal_ids=internal_ids,
        profile=profile,
        owner_value=owner_value,
        internal_open=internal_open,
        tool=tools.create_market_digest_tools()[0],
    )


def _page(**overrides: Any) -> dict[str, Any]:
    payload = {
        "ok": True,
        "usable": True,
        "truncated": False,
        "period": "AM",
        "date": prep.DATE,
        "authoritative_date": prep.DATE,
        "batch_id": f"AM/{prep.DATE}",
        "preparation_status": "ready",
        "degraded": False,
        "content_sha256": "a" * 64,
        "hashes": {"input_sha256": INPUT_HASH},
        "offset": 0,
        "max_chars": 16000,
        "total_chars": 4,
        "next_offset": None,
        "text": "page",
    }
    payload.update(overrides)
    return payload


def _prepared(**overrides: Any) -> dict[str, Any]:
    payload = {
        "ok": True,
        "usable": True,
        "period": "AM",
        "automation_execution_id": EXECUTION,
        "preparation_status": "ready",
        "authoritative_date": prep.DATE,
        "started_hkt_date": prep.DATE,
        "batch_id": f"AM/{prep.DATE}",
        "execution_id": ATTEMPT_ID,
        "hashes": {"input_sha256": INPUT_HASH, "emails_sha256": "ef" * 32, "tmtb_sha256": None},
        "emails_count": 1,
        "digest_outcome": "/tmp/secret-outcome",
        "reused": False,
    }
    payload.update(overrides)
    return payload


def _bridge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    state_root: Path | None = None,
    prepare_config: Path | None = None,
) -> rb.ResearchDataBridge:
    env_file = tmp_path / "bridge.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(env_file))
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", BRIDGE_TOKEN)
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    if state_root is None:
        monkeypatch.delenv("DALTON_MARKET_DIGEST_STATE_ROOT", raising=False)
    else:
        monkeypatch.setenv("DALTON_MARKET_DIGEST_STATE_ROOT", str(state_root))
    if prepare_config is None:
        monkeypatch.delenv("DALTON_MARKET_DIGEST_PREPARE_CONFIG", raising=False)
    else:
        monkeypatch.setenv("DALTON_MARKET_DIGEST_PREPARE_CONFIG", str(prepare_config))
    return rb.ResearchDataBridge(rb.BridgeConfig())


def _write_config(layout: prep.Layout, path: Path, **overrides: Any) -> None:
    payload: dict[str, Any] = {
        "state_root": str(layout.state),
        "skill_dir": str(layout.skill),
        "tmtb_script": str(layout.tmtb_script),
        "python": str(layout.python),
        "repo_root": str(layout.repo),
        "digest_output_dir": str(layout.output),
        "tmtb_output_dir": str(layout.tmtb_output),
        "timeout_seconds": 900,
        "stdout_limit_bytes": 2 * 1024 * 1024,
        "stderr_limit_bytes": 64 * 1024,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _arm(layout: prep.Layout, runner: prep.RecordingRunner, *, quota_hit: bool) -> None:
    document = prep._digest_document([prep._mail()], date_value=prep.DATE, period="AM")
    tmtb_doc = prep._tmtb_document(
        [{"doc_id": "d1", "title": "Note", "text": "TMTB-FULL-TEXT"}],
        date_value=prep.DATE,
        period="AM",
    )
    digest_path = layout.digest_path(prep.DATE, "AM")
    tmtb_path = layout.tmtb_path(prep.DATE, "AM")

    def digest(_argv: list[str]) -> inputs.ChildResult:
        prep._write_json(digest_path, document)
        return prep._child(
            prep._digest_stdout(
                date_value=prep.DATE,
                period="AM",
                output_file=str(digest_path),
                count=1,
            )
        )

    def tmtb(_argv: list[str]) -> inputs.ChildResult:
        prep._write_json(tmtb_path, tmtb_doc)
        return prep._child(
            prep._tmtb_stdout(
                date_value=prep.DATE,
                period="AM",
                status="new_docs",
                output_file=str(tmtb_path),
                count=1,
                new_count=1,
                quota_hit=quota_hit,
            )
        )

    runner.digest = digest
    runner.tmtb = tmtb


def _install_runner(
    monkeypatch: pytest.MonkeyPatch,
    layout: prep.Layout,
    *,
    quota_hit: bool,
) -> prep.RecordingRunner:
    runner = prep.RecordingRunner()
    _arm(layout, runner, quota_hit=quota_hit)
    monkeypatch.setattr(inputs, "run_json_command", prep._spec_runner(runner))
    monkeypatch.setattr(inputs, "build_nonarchive_subprocess_env", prep._spec_env())
    return runner


def _blob(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _assert_no_host_leak(payload: Any, layout: prep.Layout | None = None) -> None:
    text = payload if isinstance(payload, str) else _blob(payload)
    for needle in (ADMIN_TOKEN, BRIDGE_TOKEN, prep.GEMINI_VALUE, "/tmp/secret-outcome"):
        assert needle not in text
    if layout is not None:
        for needle in (str(layout.state), str(layout.skill), str(layout.python)):
            assert needle not in text


def _prepare_request() -> dict[str, str]:
    return {"period": "AM", "automation_execution_id": HOST_EXECUTION}


async def _drive(
    bridge: rb.ResearchDataBridge,
    *,
    path: str,
    body: dict[str, Any],
    token: str | None,
) -> tuple[int, dict[str, Any]]:
    raw_body = json.dumps(body).encode("utf-8")
    header_lines = ["Host: 127.0.0.1"]
    if token:
        header_lines.append(f"Authorization: Bearer {token}")
    raw = (
        f"POST {path} HTTP/1.1\r\n"
        + "\r\n".join(header_lines)
        + f"\r\nContent-Length: {len(raw_body)}\r\n\r\n"
    ).encode("utf-8") + raw_body
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    writer = create_autospec(asyncio.StreamWriter, instance=True)
    await rb._handle_client(bridge, reader, writer)
    written = b"".join(call.args[0] for call in writer.write.call_args_list)
    status = int(written.split(b"\r\n", 1)[0].decode("utf-8").split(" ")[1])
    parsed = json.loads(written.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
    return status, parsed


def test_prepare_budget_constant() -> None:
    assert tools._PREPARE_HTTP_TIMEOUT_S == 7260.0
    assert tools._PREPARE_HTTP_TIMEOUT_S == 2 * 3600 + 60
    assert tools._PREPARE_HTTP_TIMEOUT_S >= 2 * 900 + 60
    assert tools._HTTP_TIMEOUT_S == 30.0
    assert tools._MAX_HOST_RESPONSE_BYTES == 128 * 1024


def test_tool_signature_defaults_to_read() -> None:
    tool = tools.create_market_digest_tools()[0]
    signature = inspect.signature(tool.coroutine)
    assert [name for name in signature.parameters][:5] == [
        "period",
        "date",
        "offset",
        "max_chars",
        "action",
    ]
    assert signature.parameters["action"].default == "read"
    assert signature.parameters["date"].default is None
    signature.bind("AM", prep.DATE, 0, 100, config={})
    assert "prepare" in (tool.description or "")
    assert "next_offset" in (tool.description or "")


@pytest.mark.asyncio
async def test_tool_uses_real_authorization_and_separate_budgets(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    installed = _install_tool(monkeypatch)
    installed.http.payload = _prepared()
    caplog.set_level(logging.WARNING)
    prepared = json.loads(
        await installed.tool.ainvoke(
            {"action": "prepare", "period": "AM", "date": "1999-01-01"},
            config=_config(),
        )
    )
    assert installed.seen == [
        {
            "user_id": USER,
            "workspace_id": WORKSPACE,
            "thread_id": THREAD,
            "run_id": RUN,
            "automation_id": AUTOMATION,
            "execution_id": EXECUTION,
            "period": "AM",
        }
    ]
    assert installed.internal_ids == [USER]
    assert installed.pool.max_held == 1
    assert installed.pool.held == 0
    call = installed.http.calls[0]
    assert call["held"] == 0
    assert call["timeout"] == 7260.0
    assert call["trust_env"] is False
    assert call["url"] == "http://bridge.invalid/internal/market-digest/prepare"
    assert call["json"] == {"period": "AM", "automation_execution_id": EXECUTION}
    assert call["headers"]["Authorization"] == f"Bearer {ADMIN_TOKEN}"
    assert call["headers"]["X-Dalton-Bridge-Token"] == ADMIN_TOKEN
    assert prepared["ok"] is True
    assert prepared["authoritative_date"] == prep.DATE
    assert prepared["batch_id"] == f"AM/{prep.DATE}"
    assert prepared["hashes"]["input_sha256"] == INPUT_HASH
    assert prepared["automation_execution_id"] == EXECUTION
    assert prepared["execution_id"] == ATTEMPT_ID
    assert prepared["emails_count"] == 1
    assert "digest_outcome" not in prepared
    _assert_no_host_leak(prepared)

    installed.http.payload = _prepared(preparation_status="ready_degraded")
    degraded = json.loads(
        await installed.tool.ainvoke({"action": "prepare", "period": "AM"}, config=_config())
    )
    assert degraded["preparation_status"] == "ready_degraded"
    assert degraded["authoritative_date"] == prep.DATE

    installed.http.payload = _prepared(text=prep.BODY)
    rejected = json.loads(
        await installed.tool.ainvoke({"action": "prepare", "period": "AM"}, config=_config())
    )
    assert rejected["code"] == "invalid_response"
    assert prep.BODY not in _blob(rejected)

    installed.http.payload = {
        "ok": False,
        "preparation_status": "unresolved",
        "reason": "unresolved",
        "period": "AM",
        "text": prep.BODY,
        "error": "/tmp/secret-outcome",
    }
    unresolved = json.loads(
        await installed.tool.ainvoke({"action": "prepare", "period": "AM"}, config=_config())
    )
    assert unresolved["preparation_status"] == "unresolved"
    assert unresolved["code"] == "unresolved"
    _assert_no_host_leak(unresolved)
    assert prep.BODY not in _blob(unresolved)

    installed.http.payload = {
        "ok": False,
        "preparation_status": "snapshot_hash_mismatch",
        "reason": "snapshot_hash_mismatch",
        "period": "AM",
    }
    damaged = json.loads(
        await installed.tool.ainvoke({"action": "prepare", "period": "AM"}, config=_config())
    )
    assert damaged["preparation_status"] == "snapshot_hash_mismatch"

    installed.http.error = httpx.TimeoutException("budget")
    unconfirmed = json.loads(
        await installed.tool.ainvoke({"action": "prepare", "period": "AM"}, config=_config())
    )
    assert unconfirmed["code"] == "prepare_unconfirmed"
    assert "same run" in unconfirmed["error"]
    assert installed.pool.held == 0

    installed.http.error = None
    installed.http.headers = {"content-length": "200000"}
    bounded = json.loads(
        await installed.tool.ainvoke({"action": "prepare", "period": "AM"}, config=_config())
    )
    assert bounded["code"] == "response_exceeds_bound"
    installed.http.headers = None

    installed.http.payload = _page()
    page = json.loads(
        await installed.tool.ainvoke(
            {"period": "AM", "offset": 0, "max_chars": 16000},
            config=_config(),
        )
    )
    read_call = installed.http.calls[-1]
    assert read_call["timeout"] == 30.0
    assert read_call["url"].endswith("/internal/market-digest/read")
    assert read_call["json"]["automation_execution_id"] == EXECUTION
    assert "date" not in read_call["json"]
    assert page["text"] == "page"
    assert page["hashes"]["input_sha256"] == INPUT_HASH
    assert installed.pool.held == 0

    installed.http.error = httpx.TimeoutException("read budget")
    read_timeout = json.loads(
        await installed.tool.ainvoke({"period": "AM", "date": prep.DATE}, config=_config())
    )
    assert read_timeout["code"] == "host_unavailable"
    assert read_timeout["code"] != "prepare_unconfirmed"

    caplog.clear()
    installed.http.error = httpx.ConnectError(f"{prep.GEMINI_VALUE} /tmp/secret-outcome {ADMIN_TOKEN}")
    leaked = json.loads(
        await installed.tool.ainvoke({"action": "prepare", "period": "AM"}, config=_config())
    )
    assert leaked["code"] == "host_unavailable"
    _assert_no_host_leak(leaked)
    assert prep.GEMINI_VALUE not in caplog.text
    assert "/tmp/secret-outcome" not in caplog.text
    assert ADMIN_TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_tool_refuses_chat_owner_external_and_profile_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = _install_tool(monkeypatch)
    raw = await installed.tool.coroutine(
        "AM",
        None,
        0,
        16000,
        "archive",
        config=_config(),
    )
    assert json.loads(raw)["code"] == "invalid_action"
    assert installed.owner_calls == []
    assert installed.http.calls == []

    missing = json.loads(await installed.tool.ainvoke({"action": "prepare", "period": "AM"}, config={}))
    assert missing["code"] == "not_automation_run"
    assert installed.http.calls == []

    installed.owner_value[0] = None
    unconfigured = json.loads(
        await installed.tool.ainvoke({"action": "prepare", "period": "AM"}, config=_config())
    )
    assert unconfigured["code"] == "owner_not_configured"
    assert installed.http.calls == []

    installed.owner_value[0] = OTHER_OWNER
    wrong_owner = json.loads(
        await installed.tool.ainvoke({"action": "read", "period": "AM", "date": prep.DATE}, config=_config())
    )
    assert wrong_owner["code"] == "not_allowed"
    assert installed.seen == []
    assert installed.http.calls == []

    installed.owner_value[0] = OWNER
    installed.internal_open[0] = False
    external = json.loads(
        await installed.tool.ainvoke({"action": "prepare", "period": "AM"}, config=_config())
    )
    assert external["code"] == "not_allowed"
    assert installed.seen == []
    assert installed.http.calls == []

    installed.internal_open[0] = True
    installed.profile[0] = "msft-tracker"
    mismatch = json.loads(
        await installed.tool.ainvoke({"action": "prepare", "period": "AM"}, config=_config())
    )
    assert mismatch["code"] == "profile_mismatch"
    assert installed.http.calls == []
    assert installed.pool.held == 0
    assert installed.pool.max_held == 1


@pytest.mark.asyncio
async def test_config_mismatch_and_admin_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = prep.Layout(tmp_path / "layout")
    prepare_calls: list[Any] = []

    def _no_prepare(*args: Any, **kwargs: Any) -> dict[str, Any]:
        prepare_calls.append((args, kwargs))
        return {"ok": False, "preparation_status": "should_not_run"}

    monkeypatch.setattr(inputs, "prepare_execution_inputs", _no_prepare)

    unknown = tmp_path / "unknown.json"
    unknown.write_text(
        json.dumps({"imap_password": "secret-mailbox", "state_root": "/tmp/secret-outcome"}),
        encoding="utf-8",
    )
    bridge = _bridge(monkeypatch, tmp_path, state_root=layout.state, prepare_config=unknown)
    status, payload = await bridge.handle_market_digest_prepare(_prepare_request())
    assert status == 503
    assert payload == {
        "ok": False,
        "code": "config_invalid",
        "error": "Market-digest prepare config is invalid.",
    }
    assert "secret-mailbox" not in _blob(payload)
    assert "imap_password" not in _blob(payload)
    assert prepare_calls == []

    huge = tmp_path / "huge.json"
    huge.write_bytes(b" " * (70 * 1024))
    bridge = _bridge(monkeypatch, tmp_path, state_root=layout.state, prepare_config=huge)
    status, payload = await bridge.handle_market_digest_prepare(_prepare_request())
    assert status == 503 and payload["code"] == "config_invalid"
    assert prepare_calls == []

    mismatched = tmp_path / "mismatch.json"
    _write_config(layout, mismatched, state_root=str(tmp_path / "missing-root"))
    bridge = _bridge(monkeypatch, tmp_path, state_root=layout.state, prepare_config=mismatched)
    status, payload = await bridge.handle_market_digest_prepare(_prepare_request())
    assert status == 503
    assert payload["error"] == "Market-digest prepare config does not match the read state root."
    assert str(layout.state) not in _blob(payload)
    assert prepare_calls == []

    bridge = _bridge(monkeypatch, tmp_path, state_root=layout.state)
    status, payload = await bridge.handle_market_digest_prepare(_prepare_request())
    assert status == 503 and payload["code"] == "not_configured"
    assert prepare_calls == []

    status, payload = await bridge.handle_market_digest_prepare(
        {"period": "AM", "automation_execution_id": HOST_EXECUTION.upper(), "token": ADMIN_TOKEN}
    )
    assert status == 400 and payload["code"] == "unknown_field"
    assert ADMIN_TOKEN not in _blob(payload)

    status, payload = await bridge.handle_market_digest_prepare(
        {"period": "AM", "automation_execution_id": HOST_EXECUTION.upper()}
    )
    assert status == 400 and payload["code"] == "invalid_automation_execution"

    status, payload = await bridge.handle_market_digest_read(
        {"period": "AM", "date": prep.DATE, "automation_execution_id": ""}
    )
    assert status == 400 and payload["code"] == "invalid_automation_execution"

    bare = _bridge(monkeypatch, tmp_path)
    status, payload = await bare.handle_market_digest_read({"period": "AM", "date": prep.DATE})
    assert status == 503 and payload["code"] == "not_configured"

    gate = _bridge(monkeypatch, tmp_path)
    reached: list[tuple[str, dict[str, Any]]] = []

    async def _prepare(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        reached.append(("prepare", body))
        return 200, {"ok": True, "code": "ok"}

    async def _read(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        reached.append(("read", body))
        return 200, {"ok": True, "code": "ok"}

    gate.handle_market_digest_prepare = _prepare  # type: ignore[method-assign]
    gate.handle_market_digest_read = _read  # type: ignore[method-assign]
    for path in ("/internal/market-digest/prepare", "/internal/market-digest/read"):
        status, payload = await _drive(gate, path=path, body={"period": "AM"}, token=None)
        assert status == 401 and payload["error"] == "Unauthorized"
        status, payload = await _drive(gate, path=path, body={"period": "AM"}, token=BRIDGE_TOKEN)
        assert status == 403 and payload["error"] == "Forbidden"
    status, payload = await _drive(
        gate, path="/internal/market-digest/prepare", body=_prepare_request(), token=ADMIN_TOKEN
    )
    assert status == 200 and payload["code"] == "ok"
    assert reached == [("prepare", _prepare_request())]

    matched = tmp_path / "matched.json"
    _write_config(layout, matched)
    bridge = _bridge(monkeypatch, tmp_path, state_root=layout.state, prepare_config=matched)

    def _huge(config: Any, period: str, automation_execution_id: str, **kwargs: Any) -> dict[str, Any]:
        del config
        assert kwargs == {}
        return {
            "ok": True,
            "usable": True,
            "preparation_status": "ready",
            "period": period,
            "automation_execution_id": automation_execution_id,
            "authoritative_date": prep.DATE,
            "batch_id": f"{period}/{prep.DATE}",
            "execution_id": ATTEMPT_ID,
            "hashes": {"input_sha256": INPUT_HASH},
            "priority_senders": {f"s{i:05d}": True for i in range(12000)},
        }

    monkeypatch.setattr(inputs, "prepare_execution_inputs", _huge)
    status, payload = await bridge.handle_market_digest_prepare(_prepare_request())
    assert status == 409 and payload["code"] == "response_exceeds_bound"
    assert "s00000" not in _blob(payload)


@pytest.mark.asyncio
async def test_ready_degraded_same_execution_and_read_guards(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    layout = prep.Layout(tmp_path / "layout")
    config_path = tmp_path / "prepare.json"
    _write_config(layout, config_path)
    _freeze(monkeypatch)
    runner = _install_runner(monkeypatch, layout, quota_hit=True)
    load_calls: list[Path] = []
    real_load = inputs.load_prepare_config

    def _load(*, config_path: Path | None = None, **kwargs: Any) -> Any:
        assert kwargs == {}
        assert config_path is not None
        load_calls.append(config_path)
        return real_load(config_path=config_path)

    monkeypatch.setattr(inputs, "load_prepare_config", _load)
    recorded: list[dict[str, Any]] = []
    real_read = rb._read_market_digest_page

    def _read(state_root: Path, body: dict[str, Any]) -> dict[str, Any]:
        recorded.append(dict(body))
        return real_read(state_root, body)

    monkeypatch.setattr(rb, "_read_market_digest_page", _read)
    bridge = _bridge(monkeypatch, tmp_path, state_root=layout.state, prepare_config=config_path)
    caplog.set_level(logging.INFO)
    caplog.clear()
    status, prepared = await bridge.handle_market_digest_prepare(_prepare_request())
    assert status == 200 and prepared["ok"] is True
    assert prepared["preparation_status"] == "ready_degraded"
    assert prepared["usable"] is True
    assert prepared["period"] == "AM"
    assert prepared["automation_execution_id"] == HOST_EXECUTION
    assert prepared["authoritative_date"] == prep.DATE
    assert prepared["batch_id"] == f"AM/{prep.DATE}"
    assert prepared["hashes"]["input_sha256"]
    assert len(prepared["execution_id"]) == 32
    assert prepared["execution_id"] != HOST_EXECUTION.replace("-", "")
    assert [path.resolve() for path in load_calls] == [config_path.resolve()]
    assert [item["kind"] for item in runner.calls] == ["digest", "tmtb"]
    assert [item["timeout_seconds"] for item in runner.calls] == [900, 900]
    assert {item["stdout_limit_bytes"] for item in runner.calls} == {2 * 1024 * 1024}
    assert {item["stderr_limit_bytes"] for item in runner.calls} == {64 * 1024}
    _assert_no_host_leak(prepared, layout)
    assert prep.BODY not in _blob(prepared)
    assert "TMTB-FULL-TEXT" not in _blob(prepared)

    read_body = {
        "period": "AM",
        "date": prep.DATE,
        "offset": 0,
        "max_chars": 32000,
        "automation_execution_id": HOST_EXECUTION,
    }
    status, page = await bridge.handle_market_digest_read(read_body)
    assert status == 200 and page["ok"] is True
    assert page["preparation_status"] == "ready_degraded"
    assert page["degraded"] is True
    assert page["hashes"]["input_sha256"] == prepared["hashes"]["input_sha256"]
    assert prep.BODY in page["text"]
    assert recorded[-1]["date"] == prep.DATE
    assert "automation_execution_id" not in recorded[-1]
    assert set(recorded[-1]) <= {"period", "date", "offset", "max_chars"}
    _assert_no_host_leak(page, layout)

    direct = dict(read_body)
    direct.pop("automation_execution_id")
    status, direct_page = await bridge.handle_market_digest_read(direct)
    assert status == 200 and direct_page["ok"] is True
    assert prep.BODY in direct_page["text"]
    assert "automation_execution_id" not in recorded[-1]

    before = len(recorded)
    status, rejected = await bridge.handle_market_digest_read(
        {**read_body, "date": "2026-11-11"}
    )
    assert status == 409 and rejected["code"] == "date_mismatch"
    assert len(recorded) == before
    assert prep.BODY not in _blob(rejected)

    status, rejected = await bridge.handle_market_digest_read({**read_body, "period": "PM"})
    assert status == 409 and rejected["code"] == "period_mismatch"
    assert len(recorded) == before
    assert prep.BODY not in _blob(rejected)

    binding_path = layout.state / "executions" / f"{HOST_EXECUTION}.json"
    stored = json.loads(binding_path.read_text(encoding="utf-8"))
    stored["hashes"]["input_sha256"] = "0" * 64
    binding_path.write_text(json.dumps(stored), encoding="utf-8")
    status, rejected = await bridge.handle_market_digest_read(read_body)
    assert status == 409 and rejected["code"] == "hash_mismatch"
    assert "text" not in rejected
    assert prep.BODY not in _blob(rejected)
    assert len(recorded) == before + 1

    _Clock.current = NEXT
    status, again = await bridge.handle_market_digest_prepare(_prepare_request())
    assert status == 200 and again["ok"] is True
    assert again["authoritative_date"] == prep.DATE
    assert again["batch_id"] == f"AM/{prep.DATE}"
    assert again["execution_id"] == prepared["execution_id"]
    assert again["reused"] is True
    assert [item["kind"] for item in runner.calls].count("digest") == 1

    emails = layout.state / "batches" / "AM" / prep.DATE / "emails.json"
    emails.write_text(emails.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    status, damaged = await bridge.handle_market_digest_prepare(_prepare_request())
    assert status == 200
    assert damaged["ok"] is False
    assert damaged["preparation_status"] == "snapshot_hash_mismatch"
    assert prep.BODY not in _blob(damaged)
    assert [item["kind"] for item in runner.calls].count("digest") == 1
    _assert_no_host_leak(damaged, layout)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    _assert_no_host_leak(logged, layout)
    assert prep.BODY not in logged
    assert "TMTB-FULL-TEXT" not in logged


@pytest.mark.asyncio
async def test_cancel_finishes_the_worker_and_resumes_the_same_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = prep.Layout(tmp_path / "layout")
    config_path = tmp_path / "prepare.json"
    _write_config(layout, config_path)
    _freeze(monkeypatch)
    runner = _install_runner(monkeypatch, layout, quota_hit=False)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    prepare_calls: list[str] = []
    real_prepare = inputs.prepare_execution_inputs

    def _block() -> None:
        started.set()
        release.wait(10)

    def _prepare(config: Any, period: str, automation_execution_id: str, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {}
        prepare_calls.append(automation_execution_id)
        try:
            return real_prepare(config, period, automation_execution_id)
        finally:
            finished.set()

    runner.before_spawn = _block
    monkeypatch.setattr(inputs, "prepare_execution_inputs", _prepare)
    bridge = _bridge(monkeypatch, tmp_path, state_root=layout.state, prepare_config=config_path)
    task = asyncio.create_task(bridge.handle_market_digest_prepare(_prepare_request()))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(finished.wait, 5)
    assert prepare_calls == [HOST_EXECUTION]
    binding = json.loads(
        (layout.state / "executions" / f"{HOST_EXECUTION}.json").read_text(encoding="utf-8")
    )
    assert binding["batch_id"] == f"AM/{prep.DATE}"
    finished.clear()
    status, again = await bridge.handle_market_digest_prepare(_prepare_request())
    assert status == 200 and again["ok"] is True
    assert again["execution_id"] == binding["prepared_execution_id"]
    assert again["authoritative_date"] == prep.DATE
    assert again["batch_id"] == f"AM/{prep.DATE}"
    assert prepare_calls == [HOST_EXECUTION, HOST_EXECUTION]
    assert [item["kind"] for item in runner.calls].count("digest") == 1
    _assert_no_host_leak(again, layout)
    assert prep.BODY not in _blob(again)


@pytest.mark.asyncio
async def test_failed_prepare_keeps_status_and_read_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = prep.Layout(tmp_path / "layout")
    config_path = tmp_path / "prepare.json"
    _write_config(layout, config_path)
    _freeze(monkeypatch)
    runner = prep.RecordingRunner()

    def _fail(_argv: list[str]) -> inputs.ChildResult:
        return prep._child("{}", returncode=1)

    runner.digest = _fail
    monkeypatch.setattr(inputs, "run_json_command", prep._spec_runner(runner))
    monkeypatch.setattr(inputs, "build_nonarchive_subprocess_env", prep._spec_env())
    recorded: list[dict[str, Any]] = []

    def _read(state_root: Path, body: dict[str, Any]) -> dict[str, Any]:
        del state_root
        recorded.append(dict(body))
        raise AssertionError("page read")

    monkeypatch.setattr(rb, "_read_market_digest_page", _read)
    bridge = _bridge(monkeypatch, tmp_path, state_root=layout.state, prepare_config=config_path)
    status, payload = await bridge.handle_market_digest_prepare(_prepare_request())
    assert status == 200
    assert payload["ok"] is False
    assert payload["preparation_status"] == "unresolved"
    _assert_no_host_leak(payload, layout)
    assert prep.BODY not in _blob(payload)
    status, rejected = await bridge.handle_market_digest_read(
        {"period": "AM", "automation_execution_id": HOST_EXECUTION}
    )
    assert status == 409 and rejected["code"] == "not_ready"
    assert recorded == []
    assert prep.BODY not in _blob(rejected)

    missing = "77777777-7777-4777-8777-777777777777"
    status, rejected = await bridge.handle_market_digest_read(
        {"period": "AM", "automation_execution_id": missing}
    )
    assert status == 409 and rejected["code"] == "binding_missing"
    assert recorded == []
