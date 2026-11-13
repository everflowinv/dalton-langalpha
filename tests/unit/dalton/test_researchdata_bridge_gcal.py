"""Unit tests for the host calendar route ``gcal``."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"

if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402

_REAL_CREATE_SUBPROCESS_EXEC = asyncio.create_subprocess_exec
CALENDAR_ID = "cal-secret-9f3a"
MARKER = "Created by Dalton earnings tracker"


def _make_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    gws_exists: bool = True,
    calendar_id: str | None = CALENDAR_ID,
) -> rb.ResearchDataBridge:
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("DALTON_BRIDGE_TEST_MODE", "0")
    empty_env = tmp_path / "bridge.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(empty_env))
    for proxy_var in (
        "https_proxy",
        "HTTPS_PROXY",
        "http_proxy",
        "HTTP_PROXY",
        "ALL_PROXY",
        "DALTON_BRIDGE_SUBPROCESS_PROXY",
    ):
        monkeypatch.delenv(proxy_var, raising=False)
    dummy_py = tmp_path / "dummy_python"
    dummy_py.write_text("#!/bin/sh\n", encoding="utf-8")
    dummy_xreach = tmp_path / "xreach"
    dummy_xreach.write_text("#!/bin/sh\n", encoding="utf-8")
    tmeet_bin = tmp_path / "nodebin" / "tmeet"
    tmeet_bin.parent.mkdir(parents=True, exist_ok=True)
    tmeet_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    gws_bin = tmp_path / "nodebin" / "gws"
    if gws_exists:
        gws_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("DALTON_OCW_PYTHON", str(dummy_py))
    monkeypatch.setenv("DALTON_XREACH_BIN", str(dummy_xreach))
    monkeypatch.setenv("DALTON_TMEET_BIN", str(tmeet_bin))
    monkeypatch.setenv("DALTON_GWS_BIN", str(gws_bin if gws_exists else tmp_path / "missing-gws"))
    if calendar_id is None:
        monkeypatch.delenv("DALTON_GCAL_CALENDAR_ID", raising=False)
    else:
        monkeypatch.setenv("DALTON_GCAL_CALENDAR_ID", calendar_id)
    return rb.ResearchDataBridge(rb.BridgeConfig())


def _install_exec(
    monkeypatch: pytest.MonkeyPatch,
    payloads: list[tuple[bytes, bytes, int | BaseException]],
) -> list[tuple[list[str], dict[str, Any]]]:
    exec_mock = create_autospec(_REAL_CREATE_SUBPROCESS_EXEC)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    queue = list(payloads)

    async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        calls.append((list(cmd), kwargs))
        proc = create_autospec(asyncio.subprocess.Process, instance=True)
        proc.returncode = 0
        proc.pid = None
        item = queue.pop(0)
        if isinstance(item[2], BaseException):
            proc.returncode = 0

            async def _boom(input: bytes | None = None) -> tuple[bytes, bytes]:
                raise item[2]

            proc.communicate.side_effect = _boom
            return proc
        stdout, stderr, returncode = item
        proc.returncode = returncode

        async def _communicate(input: bytes | None = None) -> tuple[bytes, bytes]:
            return stdout, stderr

        proc.communicate.side_effect = _communicate
        return proc

    exec_mock.side_effect = _fake_exec
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)
    return calls


def _json_out(payload: Any, returncode: int = 0) -> tuple[bytes, bytes, int]:
    return json.dumps(payload).encode("utf-8"), b"", returncode


async def _call(bridge: rb.ResearchDataBridge, args: dict[str, Any]) -> dict[str, Any]:
    resp = await bridge.handle_tools_call(
        1, {"name": "research", "arguments": {"route": "gcal", **args}}
    )
    return json.loads(resp["result"]["content"][0]["text"])


def _tracker_event(*, description: str, event_id: str = "evt-tracked-1") -> dict[str, Any]:
    return {
        "id": event_id,
        "summary": "Affirm (AFRM) Earnings — Q1 2027 [BMO]",
        "description": description,
        "start": {"date": "2026-11-20"},
        "end": {"date": "2026-11-21"},
    }


def test_gcal_stays_out_of_shared_research_guides() -> None:
    assert "gcal" not in rb.RESEARCH_EVIDENCE_AND_ROUTE_GUIDE
    assert "gcal" not in rb.INTERNAL_LIBRARY_ROUTES


def test_health_probe_is_a_short_list_events_call() -> None:
    from src.dalton.data_map.health import build_route_probe_spec
    from tests.unit.dalton.test_bridge_callers_contract import assert_valid_bridge_call

    tool_name, args = build_route_probe_spec("gcal", "research")
    assert tool_name == "research"
    assert args["route"] == "gcal"
    assert args["operation"] == "list_events"
    assert args["limit"] == 1
    assert "T00:00:00" in args["time_min"]
    assert "T00:00:00" in args["time_max"]
    assert_valid_bridge_call(tool_name, args)


@pytest.mark.asyncio
async def test_health_loop_leaves_private_calendar_unprobed(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.dalton.data_map import health as health_mod

    fake_resolve = create_autospec(
        health_mod.resolve_researchdata_endpoint_and_token,
        return_value=("http://127.0.0.1:9/mcp", "token", set()),
    )
    monkeypatch.setattr(health_mod, "resolve_researchdata_endpoint_and_token", fake_resolve)
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _call_bridge(tool: str, arguments: dict[str, Any], timeout_s: float = 0) -> dict[str, Any]:
        calls.append((tool, dict(arguments)))
        return {"routes": [{"id": "gcal", "category": "research", "kind": "bridge_calendar"}]}

    async def _probe(**kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    async def _probe_conv() -> dict[str, Any]:
        return {"ok": True}

    rows, _catalog, _handoff = await health_mod._execute_probes(
        clamped_timeout=5,
        clamped_concurrency=2,
        call_bridge=_call_bridge,
        probe_web=_probe,
        probe_x=_probe,
        probe_market=_probe,
        probe_sec_filings=None,
        probe_conv=_probe_conv,
    )
    gcal_row = next(row for row in rows if row.get("source_id") == "gcal")
    assert gcal_row["status"] == "unprobed"
    assert gcal_row["detail"]["reason"] == "私有日历不探测"
    assert calls == [("catalog", {})]


@pytest.mark.asyncio
async def test_list_skips_preamble_strips_email_and_hides_calendar_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    event = {
        "id": "evt-list-1",
        "summary": "Affirm (AFRM) Earnings — Q1 2027 [BMO]",
        "description": "room 401 and cal-secret-9f3a",
        "start": {"date": "2026-11-20"},
        "end": {"date": "2026-11-21"},
        "organizer": {"email": "person@example.test"},
        "htmlLink": "https://calendar.example.test/event",
    }
    raw = b"Using keyring backend: keyring\n" + json.dumps({"items": [event]}).encode("utf-8")
    calls = _install_exec(monkeypatch, [(raw, b"", 0)])
    body = await _call(
        bridge,
        {
            "operation": "list_events",
            "query": "AFRM",
            "time_min": "2026-11-14T00:00:00Z",
            "time_max": "2026-11-15T00:00:00Z",
            "limit": 20,
        },
    )
    dumped = json.dumps(body, ensure_ascii=False)
    assert body["ok"] is True
    assert body["status"] == "ok"
    listed = body["data"]["events"][0]
    assert set(listed) == {"id", "summary", "description", "start", "end"}
    assert listed["id"] == "evt-list-1"
    assert "[calendar]" in listed["description"]
    assert "room 401" in listed["description"]
    assert CALENDAR_ID not in dumped
    assert "person@example.test" not in dumped
    assert "htmlLink" not in dumped
    source = body["_sources"][0]
    assert source["document_id"] == "gcal:evt-list-1"
    assert source["identity"] == "internal_note"
    assert source["private"] is True
    assert source["publisher"] == "Google Calendar"
    assert source["source_type"] == "calendar_event"
    assert source["as_of"] == "2026-11-20"
    cmd, kwargs = calls[0]
    assert cmd[1:5] == ["calendar", "events", "list", "--params"]
    assert len(cmd) == 6
    params = json.loads(cmd[5])
    assert params["calendarId"] == CALENDAR_ID
    assert params["singleEvents"] is True
    assert params["q"] == "AFRM"
    assert params["maxResults"] == 20
    assert set(params) <= {"calendarId", "singleEvents", "timeMin", "timeMax", "maxResults", "q"}
    assert kwargs["cwd"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is asyncio.subprocess.DEVNULL
    assert "DALTON_GCAL_CALENDAR_ID" not in kwargs["env"]


@pytest.mark.asyncio
async def test_window_over_400_days_does_not_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    calls = _install_exec(monkeypatch, [])
    body = await _call(
        bridge,
        {
            "operation": "list_events",
            "time_min": "2026-01-01T00:00:00Z",
            "time_max": "2027-02-06T00:00:00Z",
        },
    )
    assert body["ok"] is False
    assert body["status"] == "invalid_arguments"
    assert calls == []


@pytest.mark.asyncio
async def test_window_of_400_days_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    calls = _install_exec(monkeypatch, [_json_out({"items": []})])
    body = await _call(
        bridge,
        {
            "operation": "list_events",
            "time_min": "2026-01-01T00:00:00Z",
            "time_max": "2027-02-05T00:00:00Z",
        },
    )
    assert body["ok"] is True
    assert body["data"]["events"] == []
    assert len(calls) == 1
    params = json.loads(calls[0][0][5])
    assert params["timeMin"] == "2026-01-01T00:00:00Z"
    assert params["timeMax"] == "2027-02-05T00:00:00Z"


@pytest.mark.asyncio
async def test_insert_rejects_illegal_time_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    calls = _install_exec(monkeypatch, [])
    body = await _call(
        bridge,
        {
            "operation": "insert_event",
            "summary": "Example (AAPL) Earnings — Q1 2027 [BMO]",
            "description": "note",
            "start": {"dateTime": "2026-11-20T08:00:00"},
            "end": {"dateTime": "2026-11-20T08:30:00", "timeZone": "America/New_York"},
        },
    )
    assert body["ok"] is False
    assert body["status"] == "invalid_arguments"
    assert calls == []


@pytest.mark.asyncio
async def test_unknown_calendar_id_parameter_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    calls = _install_exec(monkeypatch, [])
    body = await _call(
        bridge,
        {
            "operation": "list_events",
            "time_min": "2026-11-14T00:00:00Z",
            "time_max": "2026-11-15T00:00:00Z",
            "calendar_id": "not-allowed",
        },
    )
    dumped = json.dumps(body, ensure_ascii=False)
    assert body["ok"] is False
    assert body["error_code"] == "unknown_parameters"
    assert CALENDAR_ID not in dumped
    assert calls == []


@pytest.mark.asyncio
async def test_insert_object_times_append_marker_and_drop_organizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    created = {
        "id": "evt-new-1",
        "summary": "Example (AAPL) Earnings — Q1 2027 [BMO]",
        "description": "https://example.test/ir\nScanned 2026-11-14\nConfidence: high",
        "start": {"dateTime": "2026-11-20T08:00:00-05:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-11-20T08:30:00-05:00", "timeZone": "America/New_York"},
        "organizer": {"email": "person@example.test"},
    }
    calls = _install_exec(monkeypatch, [_json_out(created)])
    body = await _call(
        bridge,
        {
            "operation": "insert_event",
            "summary": "Example (AAPL) Earnings — Q1 2027 [BMO]",
            "description": "https://example.test/ir\nScanned 2026-11-14\nConfidence: high",
            "start": {"dateTime": "2026-11-20T08:00:00", "timeZone": "America/New_York"},
            "end": {"dateTime": "2026-11-20T08:30:00", "timeZone": "America/New_York"},
        },
    )
    dumped = json.dumps(body, ensure_ascii=False)
    assert body["ok"] is True
    assert body["data"]["id"] == "evt-new-1"
    assert body["data"]["description"].endswith(MARKER)
    assert "person@example.test" not in dumped
    cmd = calls[0][0]
    assert cmd[1:5] == ["calendar", "events", "insert", "--params"]
    assert cmd[6] == "--json"
    sent = json.loads(cmd[7])
    assert sent["description"].endswith("\n" + MARKER)
    assert set(sent) == {"summary", "description", "start", "end"}
    assert sent["start"]["dateTime"] == "2026-11-20T08:00:00"


@pytest.mark.asyncio
async def test_delete_refuses_non_tracker_without_calling_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    foreign = {
        "id": "evt-foreign-1",
        "summary": "Affirm (AFRM) Earnings — Q1 2027 [BMO]",
        "description": "foreign-body-token",
        "start": {"date": "2026-11-20"},
        "end": {"date": "2026-11-21"},
    }
    calls = _install_exec(monkeypatch, [_json_out(foreign)])
    body = await _call(bridge, {"operation": "delete_event", "event_id": "evt-foreign-1"})
    dumped = json.dumps(body, ensure_ascii=False)
    assert body["ok"] is False
    assert body["status"] == "refused_not_tracker_event"
    assert body["error_code"] == "refused_not_tracker_event"
    assert "foreign-body-token" not in dumped
    assert len(calls) == 1
    assert calls[0][0][1:4] == ["calendar", "events", "get"]
    assert "delete" not in calls[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "description",
    [
        "https://example.test/ir\nScanned 2026-11-01\nConfidence: high",
        "来源：https://example.test/ir\n扫描日期：2026-11-01\n置信度：medium",
        "note\nCreated by Dalton earnings tracker",
    ],
)
async def test_delete_allows_old_and_new_tracker_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, description: str
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    event = _tracker_event(description=description, event_id="evt-old-1")
    calls = _install_exec(monkeypatch, [_json_out(event), (b"", b"", 0)])
    body = await _call(bridge, {"operation": "delete_event", "event_id": "evt-old-1"})
    assert body["ok"] is True
    assert body["data"] == {"deleted": True, "id": "evt-old-1"}
    assert [cmd[1:4] for cmd, _kwargs in calls] == [
        ["calendar", "events", "get"],
        ["calendar", "events", "delete"],
    ]
    assert CALENDAR_ID not in json.dumps(body, ensure_ascii=False)


@pytest.mark.asyncio
async def test_timeout_not_logged_in_missing_binary_and_non_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeout_bridge = _make_bridge(tmp_path, monkeypatch)
    _install_exec(monkeypatch, [(b"", b"", asyncio.TimeoutError())])
    timed = await _call(
        timeout_bridge,
        {
            "operation": "list_events",
            "time_min": "2026-11-14T00:00:00Z",
            "time_max": "2026-11-15T00:00:00Z",
        },
    )
    assert timed["ok"] is False
    assert timed["status"] == "timeout"
    assert "超时" in timed["hint"]

    login_bridge = _make_bridge(tmp_path, monkeypatch)
    _install_exec(monkeypatch, [(b"run: gws auth login\n", b"", 1)])
    login = await _call(
        login_bridge,
        {
            "operation": "list_events",
            "time_min": "2026-11-14T00:00:00Z",
            "time_max": "2026-11-15T00:00:00Z",
        },
    )
    login_dump = json.dumps(login, ensure_ascii=False)
    assert login["ok"] is False
    assert login["status"] == "not_logged_in"
    assert "auth login" not in login_dump
    assert CALENDAR_ID not in login_dump

    broken = _make_bridge(tmp_path, monkeypatch)
    _install_exec(monkeypatch, [(b"not json at all\n", b"", 0)])
    invalid = await _call(
        broken,
        {
            "operation": "list_events",
            "time_min": "2026-11-14T00:00:00Z",
            "time_max": "2026-11-15T00:00:00Z",
        },
    )
    assert invalid["ok"] is False
    assert invalid["status"] == "invalid_response"

    missing_bin = _make_bridge(tmp_path, monkeypatch, gws_exists=False)
    missing_calls = _install_exec(monkeypatch, [])
    missing = await _call(
        missing_bin,
        {
            "operation": "list_events",
            "time_min": "2026-11-14T00:00:00Z",
            "time_max": "2026-11-15T00:00:00Z",
        },
    )
    assert missing["status"] == "source_missing"
    assert "DALTON_GWS_BIN" in missing["hint"]
    assert missing_calls == []

    missing_calendar = _make_bridge(tmp_path, monkeypatch, gws_exists=False, calendar_id=None)
    calendar_calls = _install_exec(monkeypatch, [])
    absent = await _call(
        missing_calendar,
        {
            "operation": "list_events",
            "time_min": "2026-11-14T00:00:00Z",
            "time_max": "2026-11-15T00:00:00Z",
        },
    )
    assert absent["status"] == "source_missing"
    assert "DALTON_GCAL_CALENDAR_ID" in absent["hint"]
    assert "DALTON_GWS_BIN" not in absent["hint"]
    assert calendar_calls == []


@pytest.mark.asyncio
async def test_catalog_injects_gcal_without_calling_gws(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    commands: list[list[str]] = []

    def _mcp_response(req_id: Any) -> dict[str, Any]:
        payload = {
            "ok": True,
            "status": "ok",
            "routes": [{"id": "alphaengine", "category": "research", "operations": ["search_library"]}],
        }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False},
        }

    exec_mock = create_autospec(_REAL_CREATE_SUBPROCESS_EXEC)

    async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        commands.append([str(part) for part in cmd])
        proc = create_autospec(asyncio.subprocess.Process, instance=True)
        proc.returncode = 0
        proc.pid = None

        async def _communicate(input: bytes | None = None) -> tuple[bytes, bytes]:
            lines = (input or b"").decode("utf-8").splitlines()
            reqs = [json.loads(line) for line in lines if line.strip()]
            proc.returncode = 0
            return (json.dumps(_mcp_response(reqs[0]["id"])) + "\n").encode("utf-8"), b""

        proc.communicate.side_effect = _communicate
        return proc

    exec_mock.side_effect = _fake_exec
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)
    listed = await bridge.handle_tools_call(2, {"name": "catalog", "arguments": {}})
    listed_body = json.loads(listed["result"]["content"][0]["text"])
    assert any(item.get("id") == "gcal" for item in listed_body["routes"])
    single = await bridge.handle_tools_call(3, {"name": "catalog", "arguments": {"route": "gcal"}})
    single_body = json.loads(single["result"]["content"][0]["text"])
    assert single_body["route_count"] == 1
    entry = single_body["routes"][0]
    assert entry["id"] == "gcal"
    assert entry["required_params"] == rb.ROUTE_CONTRACT_SPECS["gcal"]["required_params"]
    assert entry["accepted_params"] == rb.ROUTE_CONTRACT_SPECS["gcal"]["accepted_params"]
    flat = [part for cmd in commands for part in cmd]
    assert str(bridge.cfg.gws_bin) not in flat
    assert CALENDAR_ID not in flat


@pytest.mark.asyncio
async def test_short_calendar_id_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    short_id = "q7z"
    bridge = _make_bridge(tmp_path, monkeypatch, calendar_id=short_id)
    event = {
        "id": "evt-list-1",
        "summary": "Affirm (AFRM) Earnings — Q1 2027 [BMO]",
        "description": "room q7z",
        "start": {"date": "2026-11-20"},
        "end": {"date": "2026-11-21"},
    }
    calls = _install_exec(monkeypatch, [_json_out({"items": [event]})])
    body = await _call(
        bridge,
        {
            "operation": "list_events",
            "time_min": "2026-11-14T00:00:00Z",
            "time_max": "2026-11-15T00:00:00Z",
        },
    )
    dumped = json.dumps(body, ensure_ascii=False)
    assert body["ok"] is True
    assert short_id not in dumped
    assert body["data"]["events"][0]["description"] == "room [calendar]"
    params = json.loads(calls[0][0][5])
    assert params["calendarId"] == short_id


@pytest.mark.asyncio
async def test_insert_rejects_non_tracker_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_bridge(tmp_path, monkeypatch)
    calls = _install_exec(monkeypatch, [])
    body = await _call(
        bridge,
        {
            "operation": "insert_event",
            "summary": "Weekly sync",
            "description": "note",
            "start": {"date": "2026-11-20"},
            "end": {"date": "2026-11-21"},
        },
    )
    assert body["ok"] is False
    assert body["status"] == "invalid_arguments"
    assert body["error_code"] == "invalid_arguments"
    assert calls == []
