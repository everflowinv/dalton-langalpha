"""Tool refusal and admin prepare/read wiring for one AI-finance batch.

The token check drives the bridge client handler in process. It does not
open a listening socket. AlphaEngine answers are the documented envelope.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

import httpx
import pytest

REAL_ASYNC_CLIENT = httpx.AsyncClient

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402
from scripts.dalton import ai_finance_access as access  # noqa: E402
from src.dalton.access_tiers import is_internal_or_admin  # noqa: E402
from src.dalton.database import ai_finance as ai_finance_db  # noqa: E402
from src.dalton.database.automation_reports import lock_report_execution  # noqa: E402
from src.dalton.settings import (  # noqa: E402
    get_dalton_bridge_admin_token,
    get_dalton_bridge_base_url,
    get_ai_finance_owner_user_id,
)
from src.dalton.tools import ai_finance_inputs_tools as tools  # noqa: E402

HKT = ZoneInfo("Asia/Hong_Kong")
BRIDGE_TOKEN = "bridge-mcp-token-secret"
ADMIN_TOKEN = "bridge-admin-token-secret"
OWNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "99999999-9999-4999-8999-999999999999"
WORKSPACE = "22222222-2222-4222-8222-222222222222"
THREAD = "33333333-3333-4333-8333-333333333333"
RUN = "44444444-4444-4444-8444-444444444444"
OTHER_RUN = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
AUTOMATION = "55555555-5555-4555-8555-555555555555"
EXECUTION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
EXECUTION_2 = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SCHEDULED = datetime(2026, 11, 11, 20, 0, tzinfo=HKT)
STARTED = datetime(2026, 11, 11, 20, 5, tzinfo=HKT)
LATER = datetime(2026, 11, 11, 22, 30, tzinfo=HKT)
DATE = "2026-11-11"
DOCUMENT = "AI revenue grew"
DOCUMENT_SHA = hashlib.sha256(DOCUMENT.encode("utf-8")).hexdigest()


class _NullCM:
    async def __aenter__(self) -> _NullCM:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _Cursor:
    def __init__(self, clock: dict[str, Any], calls: list[tuple[Any, Any]]) -> None:
        self.clock = clock
        self.calls = calls

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))

    async def fetchone(self) -> dict[str, Any]:
        return dict(self.clock)


class _Conn:
    def __init__(self, clock: dict[str, Any], calls: list[tuple[Any, Any]]) -> None:
        self.clock = clock
        self.calls = calls

    def transaction(self) -> _NullCM:
        return _NullCM()

    def cursor(self, row_factory: Any = None) -> _Cursor:
        del row_factory
        return _Cursor(self.clock, self.calls)


class _Pool:
    def __init__(self, clock: dict[str, Any], calls: list[tuple[Any, Any]]) -> None:
        self.clock = clock
        self.calls = calls
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
                return _Conn(pool.clock, pool.calls)

            async def __aexit__(self, *_args: object) -> bool:
                pool.held -= 1
                return False

        return _CM()


def _config(**overrides: Any) -> dict[str, Any]:
    configurable = {
        "user_id": OWNER.upper(),
        "workspace_id": WORKSPACE.upper(),
        "thread_id": THREAD.upper(),
        "run_id": RUN.upper(),
        "turn_origin_automation_id": AUTOMATION.upper(),
        "automation_execution_id": EXECUTION.upper(),
    }
    configurable.update(overrides)
    return {"configurable": configurable}


def _row(profile: str, *, run_id: str = RUN) -> dict[str, Any]:
    return {
        "automation_execution_id": EXECUTION,
        "automation_id": AUTOMATION,
        "status": "running",
        "conversation_thread_id": THREAD,
        "conversation_response_id": run_id,
        "user_id": OWNER,
        "workspace_id": WORKSPACE,
        "metadata": {
            "report_profile": {
                "profile": profile,
                "report_targets": ["feishu:chat:oc_marketdigest"],
                "failure_targets": [],
            }
        },
    }


def _install(monkeypatch: pytest.MonkeyPatch, handler: Any) -> SimpleNamespace:
    clock = {"scheduled_at": SCHEDULED, "started_at": STARTED}
    sql_calls: list[tuple[Any, Any]] = []
    pool = _Pool(clock, sql_calls)
    http_calls: list[dict[str, Any]] = []
    seen: list[dict[str, Any]] = []
    profile = ["ai-finance-daily"]
    run_ids = [RUN]
    owner_value: list[str | None] = [OWNER]
    internal_open = [True]
    real_auth = ai_finance_db.authorize_ai_finance_execution

    async def _lock(cur: Any, execution_id: str, *, for_update: bool) -> dict[str, Any]:
        del cur
        assert for_update is False
        assert execution_id == EXECUTION
        return _row(profile[0], run_id=run_ids[0])

    async def _auth(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return await real_auth(**kwargs)

    async def _internal(user_id: str | None) -> bool:
        return internal_open[0] and bool(user_id)

    def _owner() -> str | None:
        return owner_value[0]

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        http_calls.append({
            "timeout": kwargs.get("timeout"),
            "trust_env": kwargs.get("trust_env"),
            "held": pool.held,
        })
        kwargs["transport"] = httpx.MockTransport(handler)
        return REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(ai_finance_db, "get_db_connection", pool)
    monkeypatch.setattr(
        ai_finance_db,
        "lock_report_execution",
        create_autospec(lock_report_execution, side_effect=_lock),
    )
    monkeypatch.setattr(
        tools,
        "authorize_ai_finance_execution",
        create_autospec(real_auth, side_effect=_auth),
    )
    monkeypatch.setattr(
        tools,
        "is_internal_or_admin",
        create_autospec(is_internal_or_admin, side_effect=_internal),
    )
    monkeypatch.setattr(
        tools,
        "get_ai_finance_owner_user_id",
        create_autospec(get_ai_finance_owner_user_id, side_effect=_owner),
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
    monkeypatch.setattr(tools.httpx, "AsyncClient", factory)
    prepare_tool, read_tool = tools.create_ai_finance_inputs_tools()
    return SimpleNamespace(
        pool=pool,
        http_calls=http_calls,
        seen=seen,
        sql_calls=sql_calls,
        profile=profile,
        run_ids=run_ids,
        owner_value=owner_value,
        internal_open=internal_open,
        prepare=prepare_tool,
        read=read_tool,
    )


def _host_prepare() -> dict[str, Any]:
    return {
        "ok": True,
        "code": "ok",
        "profile": "ai-finance-daily",
        "batch_id": DATE,
        "started_at": STARTED.isoformat(),
        "window": {
            "timezone": "Asia/Hong_Kong",
            "start": datetime(2026, 11, 10, 20, 5, tzinfo=HKT).isoformat(),
            "end": STARTED.isoformat(),
            "hours": 24,
        },
        "stages": {"github": "complete", "earnings": "complete"},
        "reused": [],
        "errors": [],
        "inconsistent": False,
        "recovered": False,
        "manifest_ready": True,
        "candidate_doc_ids": ["doc1"],
        "resources": ["manifest", "github", "earnings", "candidate"],
    }


def _bridge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: Path | None) -> rb.ResearchDataBridge:
    env_file = tmp_path / "bridge.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(env_file))
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", BRIDGE_TOKEN)
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("DALTON_AI_FINANCE_OWNER_USER_ID", OWNER)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    if config is None:
        monkeypatch.delenv("DALTON_AI_FINANCE_CONFIG", raising=False)
    else:
        monkeypatch.setenv("DALTON_AI_FINANCE_CONFIG", str(config))
    return rb.ResearchDataBridge(rb.BridgeConfig())


def _prepare_body(execution_id: str = EXECUTION, started_at: datetime = STARTED) -> dict[str, str]:
    return {
        "automation_execution_id": execution_id,
        "scheduled_at": started_at.isoformat(),
        "started_at": started_at.isoformat(),
        "owner_user_id": OWNER,
        "user_id": OWNER,
        "workspace_id": WORKSPACE,
        "thread_id": THREAD,
        "profile": "ai-finance-daily",
    }


async def _drive(
    bridge: rb.ResearchDataBridge,
    *,
    path: str,
    body: dict[str, Any],
    token: str | None,
    method: str = "POST",
) -> tuple[int, dict[str, Any]]:
    raw_body = json.dumps(body).encode("utf-8")
    header_lines = ["Host: 127.0.0.1"]
    if token:
        header_lines.append(f"Authorization: Bearer {token}")
    raw = (
        f"{method} {path} HTTP/1.1\r\n"
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


def test_tool_signatures_have_no_path_or_clock() -> None:
    prepare_tool, read_tool = tools.create_ai_finance_inputs_tools()
    prepare_signature = inspect.signature(prepare_tool.coroutine)
    read_signature = inspect.signature(read_tool.coroutine)
    assert list(prepare_signature.parameters) == ["config"]
    assert prepare_signature.parameters["config"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(read_signature.parameters) == [
        "batch_id",
        "resource",
        "doc_id",
        "offset",
        "max_chars",
        "config",
    ]
    assert "state_root" not in prepare_signature.parameters
    assert "period" not in inspect.signature(ai_finance_db.authorize_ai_finance_execution).parameters
    assert "state_root" not in (prepare_tool.description or "")
    assert "ai_finance_read" in (prepare_tool.description or "")
    assert tools._PREPARE_HTTP_TIMEOUT_S == 7260.0
    assert tools._HTTP_TIMEOUT_S == 30.0
    assert tools._MAX_HOST_RESPONSE_BYTES == 128 * 1024


@pytest.mark.asyncio
async def test_gate_refusals_make_no_http_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("http")

    installed = _install(monkeypatch, handler)
    chat = json.loads(await installed.prepare.ainvoke({}, config={"configurable": {}}))
    assert chat["code"] == "not_automation_run"
    assert installed.seen == []
    installed.owner_value[0] = OTHER
    wrong_owner = json.loads(await installed.prepare.ainvoke({}, config=_config()))
    assert wrong_owner["code"] == "not_allowed"
    installed.owner_value[0] = OWNER
    installed.internal_open[0] = False
    external = json.loads(await installed.read.ainvoke(
        {"batch_id": DATE, "resource": "manifest"},
        config=_config(),
    ))
    assert external["code"] == "not_allowed"
    installed.internal_open[0] = True
    installed.profile[0] = "market-digest-am"
    wrong_profile = json.loads(await installed.prepare.ainvoke({}, config=_config()))
    assert wrong_profile["code"] == "profile_mismatch"
    installed.profile[0] = "ai-finance-daily"
    installed.run_ids[0] = OTHER_RUN
    wrong_run = json.loads(await installed.prepare.ainvoke({}, config=_config()))
    assert wrong_run["code"] == "run_mismatch"
    bad_batch = json.loads(await installed.read.ainvoke(
        {"batch_id": "/tmp/secret", "resource": "manifest"},
        config=_config(),
    ))
    assert bad_batch["code"] == "invalid_batch"
    bad_doc = json.loads(await installed.read.ainvoke(
        {"batch_id": DATE, "resource": "candidate", "doc_id": "a/../../x"},
        config=_config(),
    ))
    assert bad_doc["code"] == "invalid_doc_id"
    assert installed.http_calls == []
    assert installed.pool.held == 0


@pytest.mark.asyncio
async def test_prepare_uses_real_authorization_before_http(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_host_prepare())

    installed = _install(monkeypatch, handler)
    result = json.loads(await installed.prepare.ainvoke({}, config=_config()))
    assert result["ok"] is True
    assert result["batch_id"] == DATE
    assert result["candidate_doc_ids"] == ["doc1"]
    assert "text" not in result
    assert installed.pool.held == 0
    assert installed.pool.max_held == 1
    assert installed.http_calls[0]["held"] == 0
    assert installed.http_calls[0]["trust_env"] is False
    assert installed.http_calls[0]["timeout"] == 7260.0
    assert installed.seen == [{
        "user_id": OWNER,
        "workspace_id": WORKSPACE,
        "thread_id": THREAD,
        "run_id": RUN,
        "automation_id": AUTOMATION,
        "execution_id": EXECUTION,
    }]
    sql, params = installed.sql_calls[0]
    assert "scheduled_at" in sql and "started_at" in sql
    assert params == (EXECUTION, AUTOMATION)
    request = captured[0]
    body = json.loads(request.content.decode("utf-8"))
    assert set(body) == {
        "automation_execution_id",
        "scheduled_at",
        "started_at",
        "owner_user_id",
        "user_id",
        "workspace_id",
        "thread_id",
        "profile",
    }
    assert datetime.fromisoformat(body["scheduled_at"]) == SCHEDULED
    assert datetime.fromisoformat(body["started_at"]) == STARTED
    assert body["user_id"] == OWNER
    assert "period" not in body
    assert "state_root" not in body
    headers = {key.lower(): value for key, value in request.headers.items()}
    assert headers["authorization"] == f"Bearer {ADMIN_TOKEN}"
    assert headers["x-dalton-bridge-token"] == ADMIN_TOKEN
    assert str(request.url).endswith("/internal/ai-finance/prepare")


@pytest.mark.asyncio
async def test_prepare_timeout_and_host_text_are_not_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("late")

    installed = _install(monkeypatch, timeout)
    timed = json.loads(await installed.prepare.ainvoke({}, config=_config()))
    assert timed["ok"] is False
    assert timed["code"] == "prepare_unconfirmed"
    assert "late" not in json.dumps(timed)

    def leaked(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={
            "ok": False,
            "code": "hash_mismatch",
            "error": "The prepared batch failed the hash check.",
            "text": "SECRET-RESEARCH",
            "path": "/tmp/state",
            "batch_id": DATE,
        })

    installed = _install(monkeypatch, leaked)
    failure = json.loads(await installed.read.ainvoke(
        {"batch_id": DATE, "resource": "github"},
        config=_config(),
    ))
    blob = json.dumps(failure)
    assert failure["ok"] is False
    assert failure["code"] == "hash_mismatch"
    assert failure["batch_id"] == DATE
    assert "SECRET-RESEARCH" not in blob
    assert "/tmp/state" not in blob


@pytest.mark.asyncio
async def test_admin_token_gate_rejects_before_the_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    called: list[dict[str, Any]] = []

    async def _record(self: rb.ResearchDataBridge, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        del self
        called.append(body)
        return 200, {"ok": True}

    monkeypatch.setattr(rb.ResearchDataBridge, "handle_ai_finance_prepare", _record)
    monkeypatch.setattr(rb.ResearchDataBridge, "handle_ai_finance_read", _record)
    bridge = _bridge(monkeypatch, tmp_path, None)
    missing, missing_body = await _drive(
        bridge,
        path="/internal/ai-finance/prepare",
        body={"token": ADMIN_TOKEN},
        token=None,
    )
    assert missing == 401
    assert missing_body == {"ok": False, "error": "Unauthorized"}
    forbidden, forbidden_body = await _drive(
        bridge,
        path="/internal/ai-finance/read",
        body={"token": BRIDGE_TOKEN},
        token=BRIDGE_TOKEN,
    )
    assert forbidden == 403
    assert forbidden_body == {"ok": False, "error": "Forbidden"}
    assert called == []
    assert ADMIN_TOKEN not in json.dumps(missing_body)
    assert BRIDGE_TOKEN not in json.dumps(forbidden_body)


@pytest.mark.asyncio
async def test_callback_envelope_reuses_completed_stage_without_another_document(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "operator.json"
    config.write_text(json.dumps({"state_root": str(state)}), encoding="utf-8")
    bridge = _bridge(monkeypatch, tmp_path, config)
    counts: dict[str, int] = {}
    real_call = bridge.handle_tools_call
    real_collect = access.collect_trending

    async def _call(req_id: Any, params: dict[str, Any], *, caller_identity: dict[str, str] | None = None) -> dict[str, Any]:
        assert caller_identity == {"user": OWNER, "workspace": WORKSPACE, "thread": THREAD}
        assert params["name"] == "research"
        arguments = params["arguments"]
        assert arguments["route"] == "alphaengine"
        assert "url" not in arguments
        operation = arguments["operation"]
        counts[operation] = counts.get(operation, 0) + 1
        if operation == "auth_status":
            assert arguments["params"] == {}
            structured: dict[str, Any] = {"authenticated": True, "status": "ready"}
        elif operation == "search_library":
            structured = {
                "results": [{
                    "doc_id": "doc1",
                    "title": "AI update",
                    "rank_date": "2026-11-11T18:00:00+08:00",
                    "publish_time": "2026-11-11T18:00:00+08:00",
                    "snippet": "AI",
                }],
                "cursor": None,
                "has_more": False,
                "total_upstream": 1,
                "filters": {},
            }
        elif operation == "get_document":
            structured = {
                "metadata": {},
                "content_chars": len(DOCUMENT),
                "content_sha256": DOCUMENT_SHA,
                "offset": 0,
                "returned_chars": len(DOCUMENT),
                "text": DOCUMENT,
                "next_offset": None,
                "complete": True,
            }
        else:
            raise AssertionError(operation)
        business = {
            "ok": True,
            "status": "ok",
            "executed": True,
            "data": {"isError": False, "structuredContent": structured},
        }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(business)}],
                "isError": False,
            },
        }

    def _collect(**_kwargs: Any) -> dict[str, Any]:
        counts["github"] = counts.get("github", 0) + 1
        return {"version": 3, "candidates": []}

    monkeypatch.setattr(bridge, "handle_tools_call", create_autospec(real_call, side_effect=_call))
    monkeypatch.setattr(access, "collect_trending", create_autospec(real_collect, side_effect=_collect))
    status, payload = await _drive(
        bridge,
        path="/internal/ai-finance/prepare",
        body=_prepare_body(),
        token=ADMIN_TOKEN,
    )
    assert status == 200
    assert payload["ok"] is True
    assert payload["batch_id"] == DATE
    assert DOCUMENT not in json.dumps(payload)
    assert str(state) not in json.dumps(payload)
    assert counts["auth_status"] == 1
    assert counts["get_document"] == 1
    assert counts["github"] == 1
    frozen = payload["started_at"]
    again, recovered = await _drive(
        bridge,
        path="/internal/ai-finance/prepare",
        body=_prepare_body(EXECUTION_2, LATER),
        token=ADMIN_TOKEN,
    )
    assert again == 200
    assert recovered["ok"] is True
    assert recovered["recovered"] is True
    assert "earnings" in recovered["reused"]
    assert recovered["started_at"] == frozen
    assert counts["get_document"] == 1
    assert counts["auth_status"] == 1
    assert counts["github"] == 1
    page_status, page = await _drive(
        bridge,
        path="/internal/ai-finance/read",
        body={
            "automation_execution_id": EXECUTION_2,
            "owner_user_id": OWNER,
            "profile": "ai-finance-daily",
            "batch_id": DATE,
            "resource": "github",
            "offset": 0,
            "max_chars": 2,
        },
        token=ADMIN_TOKEN,
    )
    assert page_status == 200
    assert page["complete"] is False
    assert page["next_offset"] == 2
    full = (state / "batches" / DATE / "github_candidates.json").read_bytes()
    assert page["content_sha256"] == hashlib.sha256(full).hexdigest()
    assert page["text"] == full.decode("utf-8")[:2]
    assert str(state) not in json.dumps(page)


@pytest.mark.asyncio
async def test_bridge_rejects_bad_config_clock_and_failed_prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "operator.json"
    config.write_text(json.dumps({"state_root": str(state), "token": "secret-token"}), encoding="utf-8")
    bridge = _bridge(monkeypatch, tmp_path, config)
    status, payload = await _drive(
        bridge,
        path="/internal/ai-finance/prepare",
        body=_prepare_body(),
        token=ADMIN_TOKEN,
    )
    assert status == 503
    assert payload["code"] == "config_invalid"
    assert "secret-token" not in json.dumps(payload)
    assert str(state) not in json.dumps(payload)
    config.write_text(json.dumps({"state_root": str(state)}), encoding="utf-8")
    bridge = _bridge(monkeypatch, tmp_path, config)
    real_collect = access.collect_trending
    real_call = bridge.handle_tools_call

    def _explode(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("SECRET-PATH")

    async def _quiet(req_id: Any, params: dict[str, Any], *, caller_identity: dict[str, str] | None = None) -> dict[str, Any]:
        del caller_identity
        operation = params["arguments"]["operation"]
        structured = {"authenticated": True, "status": "ready"} if operation == "auth_status" else {
            "results": [],
            "cursor": None,
            "has_more": False,
            "total_upstream": 0,
            "filters": {},
        }
        business = {
            "ok": True,
            "status": "ok",
            "executed": True,
            "data": {"isError": False, "structuredContent": structured},
        }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": json.dumps(business)}], "isError": False},
        }

    monkeypatch.setattr(access, "collect_trending", create_autospec(real_collect, side_effect=_explode))
    monkeypatch.setattr(bridge, "handle_tools_call", create_autospec(real_call, side_effect=_quiet))
    crossed = _prepare_body()
    crossed["scheduled_at"] = datetime(2026, 11, 11, 23, 30, tzinfo=HKT).isoformat()
    crossed["started_at"] = datetime(2026, 11, 12, 0, 30, tzinfo=HKT).isoformat()
    status, payload = await _drive(
        bridge,
        path="/internal/ai-finance/prepare",
        body=crossed,
        token=ADMIN_TOKEN,
    )
    assert status == 400
    assert payload["code"] == "clock_disagreement"
    assert not (state / "batches").exists()
    failed_status, failed = await _drive(
        bridge,
        path="/internal/ai-finance/prepare",
        body=_prepare_body(),
        token=ADMIN_TOKEN,
    )
    blob = json.dumps(failed)
    assert failed["ok"] is False
    assert failed_status == 409
    assert "github_failed:RuntimeError" in failed["errors"]
    assert "SECRET-PATH" not in blob
    method, method_body = await _drive(
        bridge,
        path="/internal/ai-finance/read",
        body=_prepare_body(),
        token=ADMIN_TOKEN,
        method="GET",
    )
    assert method == 405
    assert method_body == {"ok": False, "error": "Method Not Allowed"}
    huge_status, huge = rb._ai_finance_response(
        {"ok": True, "text": "密" * 80000},
        failed_code="read_failed",
    )
    assert huge_status == 409
    assert huge["code"] == "response_exceeds_bound"
    assert "text" not in huge
