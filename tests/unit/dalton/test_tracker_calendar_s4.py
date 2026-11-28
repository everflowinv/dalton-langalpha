"""Step 4 unit tests: tracker calendar gate, caller token signing, bridge forwarding, and ops probe.

All test doubles use ``create_autospec``.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import fcntl
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

from fastapi import FastAPI
import httpx
from psycopg_pool import AsyncConnectionPool
import pytest

from ptc_agent.agent.tools.code_execution import _bound_caller_identity
from src.dalton.app import tracker_calendar_router as tc_router
from src.dalton.database import user_integrations as integrations_db
from src.dalton.database.user_integrations import (
    IntegrationRow,
    IntegrationStatus,
)
from src.dalton.integrations import tracker_calendar as tc
from src.dalton.integrations.constants import (
    GOOGLE_SCOPE_CALENDAR_EVENTS,
    GOOGLE_SCOPE_CALENDAR_EVENTS_RO,
    GOOGLE_SCOPE_CALENDAR_LIST_RO,
)
from src.dalton.integrations.google_calendar import GoogleCalendarMCPClient
from src.dalton.sandbox_caller_identity import (
    caller_env,
    sign_caller_token,
    verify_caller_token,
)
from src.dalton.services import integration_background as _integration_bg  # noqa: F401
from src.dalton.services.integration_data_purge import get_tracker_purge_handler
from src.dalton.services.user_integrations_oauth import FreshAccessToken

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402
import source_identity  # noqa: E402

OWNER_ID = "991a99ad-8ff2-4bd8-bf08-e5963a248798"
AUTO_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
EXEC_ID = "12345678-1234-1234-1234-1234567890ab"
RUN_ID = "87654321-4321-4321-4321-ba0987654321"
WS_ID = "ws-tracker-1"
THREAD_ID = "thread-tracker-1"
BRIDGE_TOKEN = "bridge-admin-secret-s4"
CALENDAR_ID = "cal-tracker-secret-s4@group.calendar.google.com"


def _load_probe_module() -> Any:
    mod_name = "gws_migration_probe_under_test"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    probe_path = REPO_ROOT / "scripts" / "dalton" / "ops" / "gws_migration_probe.py"
    spec = importlib.util.spec_from_file_location(mod_name, probe_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_richard_earnings_module() -> Any:
    mod_name = "richard_earnings_s4_under_test"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    path = (
        REPO_ROOT
        / "plugins"
        / "dalton"
        / "skills"
        / "dalton-richard-earnings"
        / "scripts"
        / "richard_earnings.py"
    )
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_integration_row(
    *,
    user_id: str = OWNER_ID,
    scopes: str = f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS}",
    calendar_id: str = CALENDAR_ID,
    access_token: str | None = "ya29.test-access-token",
    refresh_token: str | None = "1//test-refresh-token",
) -> IntegrationRow:
    now = datetime.now(UTC)
    return IntegrationRow(
        user_id=user_id,
        provider="google",
        service="calendar",
        oauth_client_id="123456-test.apps.googleusercontent.com",
        provider_account_id="sub-owner-1",
        provider_union_id=None,
        provider_email="owner@example.test",
        granted_scopes=scopes,
        auth_epoch=1,
        authorized_at=now,
        token_generation=1,
        status=IntegrationStatus.CONNECTED,
        refresh_inflight_gen=None,
        refresh_started_at=None,
        expires_at=now + timedelta(hours=1),
        last_refresh_at=now,
        needs_reauth_at=None,
        last_error_code=None,
        reauth_notified_at=None,
        preferences={"tracker_calendar_id": calendar_id},
        has_access_token=bool(access_token),
        has_refresh_token=bool(refresh_token),
        created_at=now,
        updated_at=now,
        access_token=access_token,
        refresh_token=refresh_token,
    )


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(tc_router.router)
    return app


def _signed_headers(
    *,
    user: str = OWNER_ID,
    workspace: str = WS_ID,
    thread: str = THREAD_ID,
    run_origin: str = "automation",
    run_id: str = RUN_ID,
    automation_id: str = AUTO_ID,
    execution_id: str = EXEC_ID,
    exp: int | None = None,
    bridge_token: str = BRIDGE_TOKEN,
    admin_token_for_sig: str = BRIDGE_TOKEN,
) -> dict[str, str]:
    tok = sign_caller_token(
        {
            "user": user,
            "workspace": workspace,
            "thread": thread,
            "run_origin": run_origin,
            "run_id": run_id,
            "turn_origin_automation_id": automation_id,
            "automation_execution_id": execution_id,
        },
        exp=exp,
        admin_token=admin_token_for_sig,
    )
    assert tok is not None
    return {
        "X-Dalton-Bridge-Token": bridge_token,
        "X-Dalton-Caller-Scope": "per-request",
        "X-Dalton-Caller-Token": tok,
    }


def _configure_gate_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    backend_mode: str = "official_mcp",
) -> None:
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", BRIDGE_TOKEN)
    monkeypatch.setenv("DALTON_TRACKER_CALENDAR_OWNER_ID", OWNER_ID)
    monkeypatch.setenv("DALTON_TRACKER_CALENDAR_AUTOMATION_ID", AUTO_ID)
    monkeypatch.setenv("DALTON_GCAL_BACKEND", backend_mode)
    monkeypatch.setenv("DALTON_GCAL_CALENDAR_ID", CALENDAR_ID)
    monkeypatch.setenv("DALTON_CAP_FUND_AUTOMATIONS", "true")
    monkeypatch.setattr(tc_router, "is_dalton_enabled", lambda: True)
    monkeypatch.setattr(tc_router, "require_capability", lambda *a, **k: None)


def _build_local_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> rb.ResearchDataBridge:
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", BRIDGE_TOKEN)
    monkeypatch.setenv("DALTON_GCAL_CALENDAR_ID", CALENDAR_ID)
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("DALTON_BRIDGE_TEST_MODE", "0")
    empty_env = tmp_path / "bridge.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(empty_env))
    gws_bin = tmp_path / "gws"
    gws_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("DALTON_GWS_BIN", str(gws_bin))
    return rb.ResearchDataBridge(rb.BridgeConfig())


# ---------------------------------------------------------------------------
# 1. Caller identity signing & _bound_caller_identity hook
# ---------------------------------------------------------------------------


def test_caller_token_signs_and_verifies_execution_triplet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", BRIDGE_TOKEN)
    config = {
        "configurable": {
            "user_id": OWNER_ID,
            "workspace_id": WS_ID,
            "thread_id": THREAD_ID,
            "run_id": RUN_ID,
            "turn_origin_automation_id": AUTO_ID,
            "automation_execution_id": EXEC_ID,
            "turn_time_limit": 300,
        }
    }
    with _bound_caller_identity(config, "fallback-thread"):
        env = caller_env()
    token = env["DALTON_CALLER_TOKEN"]
    verified = verify_caller_token(token, admin_token=BRIDGE_TOKEN)
    assert verified == {
        "user": OWNER_ID,
        "workspace": WS_ID,
        "thread": THREAD_ID,
        "run_origin": "automation",
        "run_id": RUN_ID,
        "turn_origin_automation_id": AUTO_ID,
        "automation_execution_id": EXEC_ID,
    }


def test_caller_token_rejects_forged_expired_and_tampered_run_id() -> None:
    now = 1_800_000_000
    valid = sign_caller_token(
        {
            "user": OWNER_ID,
            "workspace": WS_ID,
            "thread": THREAD_ID,
            "run_origin": "automation",
            "run_id": RUN_ID,
            "turn_origin_automation_id": AUTO_ID,
            "automation_execution_id": EXEC_ID,
        },
        exp=now + 60,
        admin_token=BRIDGE_TOKEN,
    )
    assert valid is not None

    assert verify_caller_token(valid, admin_token=BRIDGE_TOKEN, now_ts=now + 61) is None
    assert verify_caller_token(valid, admin_token="wrong-secret", now_ts=now) is None

    payload_b64, sig_b64 = valid.split(".")
    raw_claims = json.loads(
        base64.urlsafe_b64decode(payload_b64 + "=" * ((4 - len(payload_b64) % 4) % 4))
    )
    raw_claims["run_id"] = "00000000-0000-0000-0000-000000000000"
    tampered_b64 = (
        base64.urlsafe_b64encode(
            json.dumps(raw_claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    assert verify_caller_token(f"{tampered_b64}.{sig_b64}", admin_token=BRIDGE_TOKEN, now_ts=now) is None


# ---------------------------------------------------------------------------
# 2. Backend gate: authz, 400 unknown op, 4 operations mapping, read-only refusal
# ---------------------------------------------------------------------------


class _InMemoryExecCursor:
    """Minimal async cursor backing ``lock_report_execution`` against in-memory rows."""

    def __init__(
        self,
        automations: dict[str, dict[str, Any]],
        executions: dict[str, dict[str, Any]],
    ) -> None:
        self._automations = automations
        self._executions = executions
        self._next_row: dict[str, Any] | None = None

    async def __aenter__(self) -> _InMemoryExecCursor:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        normalized = " ".join(sql.split()).upper()
        if "FROM AUTOMATION_EXECUTIONS" in normalized and "AND AUTOMATION_ID" not in normalized:
            exec_id = str(params[0]).lower()
            row = self._executions.get(exec_id)
            self._next_row = {"automation_id": row["automation_id"]} if row else None
        elif "FROM AUTOMATIONS" in normalized:
            auto_id = str(params[0]).lower()
            row = self._automations.get(auto_id)
            self._next_row = dict(row) if row else None
        elif "FROM AUTOMATION_EXECUTIONS" in normalized and "AND AUTOMATION_ID" in normalized:
            exec_id = str(params[0]).lower()
            auto_id = str(params[1]).lower()
            row = self._executions.get(exec_id)
            if row and str(row["automation_id"]).lower() == auto_id:
                self._next_row = dict(row)
            else:
                self._next_row = None
        else:
            self._next_row = None

    async def fetchone(self) -> dict[str, Any] | None:
        return self._next_row


class _InMemoryExecConn:
    def __init__(
        self,
        automations: dict[str, dict[str, Any]],
        executions: dict[str, dict[str, Any]],
    ) -> None:
        self._automations = automations
        self._executions = executions

    @asynccontextmanager
    async def transaction(self) -> Any:
        yield self

    def cursor(self, **_kwargs: Any) -> _InMemoryExecCursor:
        return _InMemoryExecCursor(self._automations, self._executions)


def _patch_inmemory_exec_db(
    monkeypatch: pytest.MonkeyPatch,
    *,
    automations: dict[str, dict[str, Any]],
    executions: dict[str, dict[str, Any]],
) -> None:
    conn = _InMemoryExecConn(automations, executions)

    @asynccontextmanager
    async def _fake_get_db_connection(_conn: Any = None) -> Any:
        yield conn

    monkeypatch.setattr(tc, "get_db_connection", _fake_get_db_connection)


@pytest.mark.asyncio
async def test_tracker_calendar_gate_rejects_forged_expired_tampered_and_unknown_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_gate_env(monkeypatch, backend_mode="official_mcp")
    other_auto_id = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    finished_exec_id = "22345678-1234-1234-1234-1234567890ab"
    other_exec_id = "32345678-1234-1234-1234-1234567890ab"
    _patch_inmemory_exec_db(
        monkeypatch,
        automations={
            AUTO_ID.lower(): {
                "automation_id": AUTO_ID,
                "user_id": OWNER_ID,
                "workspace_id": WS_ID,
                "name": "richard-earnings",
                "metadata": {},
                "delivery_config": {},
            },
            other_auto_id.lower(): {
                "automation_id": other_auto_id,
                "user_id": OWNER_ID,
                "workspace_id": WS_ID,
                "name": "other-automation",
                "metadata": {},
                "delivery_config": {},
            },
        },
        executions={
            EXEC_ID.lower(): {
                "automation_execution_id": EXEC_ID,
                "automation_id": AUTO_ID,
                "status": "running",
                "conversation_thread_id": THREAD_ID,
                "conversation_response_id": RUN_ID,
            },
            finished_exec_id.lower(): {
                "automation_execution_id": finished_exec_id,
                "automation_id": AUTO_ID,
                "status": "completed",
                "conversation_thread_id": THREAD_ID,
                "conversation_response_id": RUN_ID,
            },
            other_exec_id.lower(): {
                "automation_execution_id": other_exec_id,
                "automation_id": other_auto_id,
                "status": "running",
                "conversation_thread_id": THREAD_ID,
                "conversation_response_id": RUN_ID,
            },
        },
    )

    assert tc_router.authorize_tracker_calendar_execution is tc.authorize_tracker_calendar_execution

    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    list_body = {
        "operation": "list_events",
        "arguments": {
            "time_min": "2026-11-14T00:00:00Z",
            "time_max": "2026-11-15T00:00:00Z",
        },
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r_forged = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={"operation": "health_check", "arguments": {}},
            headers=_signed_headers(admin_token_for_sig="forged-secret"),
        )
        assert r_forged.status_code == 401

        r_expired = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={"operation": "health_check", "arguments": {}},
            headers=_signed_headers(exp=100),
        )
        assert r_expired.status_code == 401

        valid_headers = _signed_headers()
        payload_b64, sig_b64 = valid_headers["X-Dalton-Caller-Token"].split(".")
        raw_claims = json.loads(
            base64.urlsafe_b64decode(payload_b64 + "=" * ((4 - len(payload_b64) % 4) % 4))
        )
        raw_claims["run_id"] = "99999999-9999-9999-9999-999999999999"
        mutated_b64 = (
            base64.urlsafe_b64encode(
                json.dumps(raw_claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        r_tampered_sig = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json=list_body,
            headers={**valid_headers, "X-Dalton-Caller-Token": f"{mutated_b64}.{sig_b64}"},
        )
        assert r_tampered_sig.status_code == 401
        assert r_tampered_sig.json()["detail"]["code"] == "invalid_caller_token"

        r_run_mismatch = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json=list_body,
            headers=_signed_headers(run_id="99999999-9999-9999-9999-999999999999"),
        )
        assert r_run_mismatch.status_code == 403
        assert r_run_mismatch.json()["detail"]["code"] == "run_mismatch"

        r_other_auto = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json=list_body,
            headers=_signed_headers(automation_id=other_auto_id, execution_id=other_exec_id),
        )
        assert r_other_auto.status_code == 403
        assert r_other_auto.json()["detail"]["code"] == "automation_mismatch"

        r_finished = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json=list_body,
            headers=_signed_headers(execution_id=finished_exec_id),
        )
        assert r_finished.status_code == 403
        assert r_finished.json()["detail"]["code"] == "execution_not_running"

        r_unknown = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={"operation": "update_event", "arguments": {}},
            headers=_signed_headers(),
        )
        assert r_unknown.status_code == 400
        assert r_unknown.json()["detail"]["code"] == "invalid_operation"


@pytest.mark.asyncio
async def test_tracker_calendar_gate_official_mcp_four_operations_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_gate_env(monkeypatch, backend_mode="official_mcp")
    authz_mock = create_autospec(
        tc.authorize_tracker_calendar_execution,
        return_value={"ok": True, "code": "ok"},
    )
    monkeypatch.setattr(tc_router, "authorize_tracker_calendar_execution", authz_mock)

    write_row = _make_integration_row(
        scopes=f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS}"
    )
    monkeypatch.setattr(
        integrations_db,
        "get_connection",
        create_autospec(integrations_db.get_connection, return_value=write_row),
    )

    fresh_mock = create_autospec(
        tc.ensure_fresh_access_token,
        return_value=FreshAccessToken(
            access_token="ya29.fresh-token",
            provider="google",
            service="calendar",
            oauth_client_id=write_row.oauth_client_id,
            provider_account_id="sub-owner-1",
            auth_epoch=1,
            token_generation=1,
            granted_scopes=f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS}",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    monkeypatch.setattr(tc, "ensure_fresh_access_token", fresh_mock)

    mcp_client = create_autospec(GoogleCalendarMCPClient, instance=True)
    recorded_calls: list[tuple[str, dict[str, Any]]] = []

    async def _fake_call_mcp(
        *,
        access_token: str,
        tool_name: str,
        arguments: dict[str, Any],
        request_state: str | None = None,
        input_responses: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert access_token == "ya29.fresh-token"
        recorded_calls.append((tool_name, dict(arguments)))
        if tool_name == "list_events":
            return {
                "events": [
                    {
                        "id": "evt-est-1",
                        "summary": "Affirm (AFRM) Earnings — Q1 2027 [BMO]",
                        "description": "Scanned 2026-11-14\nCreated by Dalton earnings tracker",
                        "start": {"dateTime": "2026-11-20T08:00:00-05:00", "timeZone": "America/New_York"},
                        "end": {"dateTime": "2026-11-20T08:30:00-05:00", "timeZone": "America/New_York"},
                        "organizer": {"email": "secret@example.test"},
                    }
                ]
            }
        if tool_name == "create_event":
            return {
                "id": "evt-created-1",
                "summary": arguments["summary"],
                "description": arguments["description"],
                "start": {"dateTime": "2026-11-20T08:00:00-05:00", "timeZone": "America/New_York"},
                "end": {"dateTime": "2026-11-20T08:30:00-05:00", "timeZone": "America/New_York"},
            }
        if tool_name == "get_event":
            if arguments["eventId"] == "evt-old-marker":
                return {
                    "id": "evt-old-marker",
                    "summary": "Affirm (AFRM) Earnings — Q4 2026 [BMO]",
                    "description": "来源：IR\n扫描日期：2026-08-10",
                    "start": {"date": "2026-08-20"},
                    "end": {"date": "2026-08-21"},
                }
            return {
                "id": "evt-non-tracker",
                "summary": "1:1 with Bob",
                "description": "personal meeting",
                "start": {"date": "2026-11-20"},
                "end": {"date": "2026-11-21"},
            }
        if tool_name == "delete_event":
            return {"deleted": True}
        raise AssertionError(f"unexpected tool {tool_name}")

    mcp_client.call_mcp_tool.side_effect = _fake_call_mcp
    monkeypatch.setattr(tc_router, "get_router_tracker_google_client", lambda: mcp_client)

    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    headers = _signed_headers()

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r_list = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={
                "operation": "list_events",
                "arguments": {
                    "time_min": "2026-11-14T13:00:00Z",  # EST -> 08:00:00
                    "time_max": "2027-06-15T12:00:00Z",  # EDT -> 08:00:00
                    "query": "AFRM",
                    "limit": 25,
                },
            },
            headers=headers,
        )
        assert r_list.status_code == 200
        list_body = r_list.json()["payload"]
        assert list_body["ok"] is True
        assert list_body["data"]["event_count"] == 1
        assert set(list_body["data"]["events"][0]) == {"id", "summary", "description", "start", "end"}
        tool_name, args = recorded_calls[-1]
        assert tool_name == "list_events"
        assert args == {
            "calendarId": CALENDAR_ID,
            "startTime": "2026-11-14T08:00:00",
            "endTime": "2027-06-15T08:00:00",
            "timeZone": "America/New_York",
            "pageSize": 25,
            "orderBy": "startTime",
            "fullText": "AFRM",
        }

        r_ins = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={
                "operation": "insert_event",
                "arguments": {
                    "summary": "Affirm (AFRM) Earnings — Q1 2027 [BMO]",
                    "description": "https://example.test/ir\nScanned 2026-11-14",
                    "start": {"dateTime": "2026-11-20T08:00:00", "timeZone": "America/New_York"},
                    "end": {"dateTime": "2026-11-20T08:30:00", "timeZone": "America/New_York"},
                },
            },
            headers=headers,
        )
        assert r_ins.status_code == 200
        ins_body = r_ins.json()["payload"]
        assert ins_body["ok"] is True
        assert ins_body["data"]["id"] == "evt-created-1"
        assert ins_body["data"]["description"].endswith(tc.TRACKER_GCAL_MARKER)
        tool_name, args = recorded_calls[-1]
        assert tool_name == "create_event"
        assert args["allDay"] is False
        assert args["timeZone"] == "America/New_York"
        assert args["startTime"] == "2026-11-20T08:00:00"
        assert args["endTime"] == "2026-11-20T08:30:00"

        r_del_ok = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={"operation": "delete_event", "arguments": {"event_id": "evt-old-marker"}},
            headers=headers,
        )
        assert r_del_ok.status_code == 200
        assert r_del_ok.json()["payload"] == {
            "ok": True,
            "status": "ok",
            "data": {"deleted": True, "id": "evt-old-marker"},
        }
        assert recorded_calls[-2][0] == "get_event"
        assert recorded_calls[-1] == (
            "delete_event",
            {
                "calendarId": CALENDAR_ID,
                "eventId": "evt-old-marker",
                "notificationLevel": "NONE",
            },
        )

        calls_before = len(recorded_calls)
        r_del_ref = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={"operation": "delete_event", "arguments": {"event_id": "evt-non-tracker"}},
            headers=headers,
        )
        assert r_del_ref.status_code == 200
        assert r_del_ref.json()["payload"]["status"] == "refused_not_tracker_event"
        assert len(recorded_calls) == calls_before + 1
        assert recorded_calls[-1][0] == "get_event"

        r_hc = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={"operation": "health_check", "arguments": {}},
            headers=headers,
        )
        assert r_hc.status_code == 200
        assert r_hc.json()["payload"] == {
            "ok": True,
            "status": "ok",
            "data": {"calendar_ok": True, "backend": "official_mcp"},
        }
        assert recorded_calls[-1][0] == "list_events"
        assert recorded_calls[-1][1]["pageSize"] == 1
        assert recorded_calls[-1][1]["timeZone"] == "America/New_York"


@pytest.mark.asyncio
async def test_official_mcp_readonly_scope_refuses_writes_and_allows_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_gate_env(monkeypatch, backend_mode="official_mcp")
    authz_mock = create_autospec(
        tc.authorize_tracker_calendar_execution,
        return_value={"ok": True, "code": "ok"},
    )
    monkeypatch.setattr(tc_router, "authorize_tracker_calendar_execution", authz_mock)

    ro_row = _make_integration_row(
        scopes=f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS_RO}"
    )
    monkeypatch.setattr(
        integrations_db,
        "get_connection",
        create_autospec(integrations_db.get_connection, return_value=ro_row),
    )

    fresh_mock = create_autospec(
        tc.ensure_fresh_access_token,
        return_value=FreshAccessToken(
            access_token="ya29.ro-token",
            provider="google",
            service="calendar",
            oauth_client_id=ro_row.oauth_client_id,
            provider_account_id="sub-owner-1",
            auth_epoch=1,
            token_generation=1,
            granted_scopes=f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS_RO}",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    monkeypatch.setattr(tc, "ensure_fresh_access_token", fresh_mock)

    mcp_client = create_autospec(GoogleCalendarMCPClient, instance=True)
    mcp_client.call_mcp_tool.return_value = {"events": []}
    monkeypatch.setattr(tc_router, "get_router_tracker_google_client", lambda: mcp_client)

    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    headers = _signed_headers()

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r_ins = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={
                "operation": "insert_event",
                "arguments": {
                    "summary": "Affirm (AFRM) Earnings — Q1 2027 [BMO]",
                    "description": "Scanned 2026-11-14",
                    "start": {"date": "2026-11-20"},
                    "end": {"date": "2026-11-21"},
                },
            },
            headers=headers,
        )
        assert r_ins.status_code == 200
        ins_payload = r_ins.json()["payload"]
        assert ins_payload["ok"] is False
        assert ins_payload["error_code"] == tc.TRACKER_WRITE_SCOPE_MISSING_CODE
        assert "legacy_gws" in ins_payload["hint"]
        assert mcp_client.call_mcp_tool.call_count == 0

        r_del = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={"operation": "delete_event", "arguments": {"event_id": "evt-old-1"}},
            headers=headers,
        )
        assert r_del.status_code == 200
        del_payload = r_del.json()["payload"]
        assert del_payload["ok"] is False
        assert del_payload["error_code"] == tc.TRACKER_WRITE_SCOPE_MISSING_CODE
        assert mcp_client.call_mcp_tool.call_count == 0

        r_hc = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={"operation": "health_check", "arguments": {}},
            headers=headers,
        )
        assert r_hc.status_code == 200
        assert r_hc.json()["payload"]["data"] == {"calendar_ok": True, "backend": "official_mcp"}

        r_list = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={
                "operation": "list_events",
                "arguments": {
                    "time_min": "2026-11-14T00:00:00Z",
                    "time_max": "2026-11-15T00:00:00Z",
                },
            },
            headers=headers,
        )
        assert r_list.status_code == 200
        assert r_list.json()["payload"]["ok"] is True
        assert mcp_client.call_mcp_tool.call_count == 2


# ---------------------------------------------------------------------------
# 3. legacy_gws branch + bridge health_check conversion + raw token forwarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_gws_branch_calls_bridge_and_converts_health_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_gate_env(monkeypatch, backend_mode="legacy_gws")
    monkeypatch.setenv("DALTON_BRIDGE_URL", "http://127.0.0.1:8960")
    monkeypatch.setattr(tc, "has_capability", lambda cap: True)

    bridge = _build_local_bridge(tmp_path, monkeypatch)

    gws_calls: list[list[str]] = []

    async def _fake_exec_gws(argv: list[str], timeout_s: float) -> tuple[int, bytes, bytes]:
        gws_calls.append(list(argv))
        return 0, json.dumps({"items": []}).encode("utf-8"), b""

    exec_mock = create_autospec(bridge._exec_gws, side_effect=_fake_exec_gws)
    monkeypatch.setattr(bridge, "_exec_gws", exec_mock)

    http_client = create_autospec(httpx.AsyncClient, instance=True)
    captured_posts: list[dict[str, Any]] = []

    async def _fake_post(
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: Any = None,
    ) -> httpx.Response:
        captured_posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        code, resp_body = await bridge.handle_tracker_calendar_legacy_gws(json or {})
        return httpx.Response(code, json=resp_body)

    http_client.post.side_effect = _fake_post
    monkeypatch.setattr(tc_router, "get_router_legacy_gws_http_client", lambda: http_client)

    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={"operation": "health_check", "arguments": {}, "timeout_s": 60.0},
            headers=_signed_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["payload"] == {
        "ok": True,
        "status": "ok",
        "data": {"calendar_ok": True, "backend": "legacy_gws"},
    }
    assert body["calendar_id"] == CALENDAR_ID
    assert len(captured_posts) == 1
    assert captured_posts[0]["url"] == "http://127.0.0.1:8960/internal/tracker-calendar/legacy-gws"
    assert captured_posts[0]["json"]["timeout_s"] == 25.0
    assert len(gws_calls) == 1
    assert gws_calls[0][1:4] == ["calendar", "events", "list"]
    params = json.loads(gws_calls[0][5])
    assert params["maxResults"] == 1
    assert params["calendarId"] == CALENDAR_ID

    assert (
        source_identity.declare("research", {"route": "gcal", "operation": "health_check"}, body["payload"])
        == []
    )


@pytest.mark.asyncio
async def test_bridge_forwards_raw_caller_token_and_runs_gcal_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", BRIDGE_TOKEN)
    monkeypatch.setenv("DALTON_BACKEND_INTERNAL_URL", "http://backend:8000")
    monkeypatch.setenv("DALTON_GCAL_BACKEND", "official_mcp")
    bridge = _build_local_bridge(tmp_path, monkeypatch)

    raw_tok = sign_caller_token(
        {
            "user": OWNER_ID,
            "workspace": WS_ID,
            "thread": THREAD_ID,
            "run_origin": "automation",
            "run_id": RUN_ID,
            "turn_origin_automation_id": AUTO_ID,
            "automation_execution_id": EXEC_ID,
        },
        admin_token=BRIDGE_TOKEN,
    )
    assert raw_tok is not None

    forwarded: list[dict[str, Any]] = []
    http_client = create_autospec(httpx.AsyncClient, instance=True)

    async def _fake_post(
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: Any = None,
    ) -> httpx.Response:
        forwarded.append({"url": url, "json": json, "headers": headers})
        return httpx.Response(
            200,
            json={
                "payload": {
                    "ok": True,
                    "status": "ok",
                    "data": {
                        "events": [
                            {
                                "id": "evt-fwd-1",
                                "summary": "Affirm (AFRM) Earnings — Q1 2027 [BMO]",
                                "description": f"contains {CALENDAR_ID}",
                                "start": {"date": "2026-11-20"},
                                "end": {"date": "2026-11-21"},
                            }
                        ],
                        "event_count": 1,
                    },
                },
                "calendar_id": CALENDAR_ID,
            },
        )

    http_client.post.side_effect = _fake_post
    bridge._gcal_http = http_client

    rpc_resp = await bridge.handle_tools_call(
        7,
        {
            "name": "research",
            "arguments": {
                "route": "gcal",
                "operation": "list_events",
                "time_min": "2026-11-14T00:00:00Z",
                "time_max": "2026-11-15T00:00:00Z",
            },
        },
        raw_caller_token=raw_tok,
    )
    assert len(forwarded) == 1
    assert forwarded[0]["url"] == "http://backend:8000/api/v1/dalton/mcp/tracker-calendar"
    assert forwarded[0]["headers"]["X-Dalton-Caller-Token"] == raw_tok
    assert forwarded[0]["headers"]["X-Dalton-Caller-Scope"] == "per-request"
    assert forwarded[0]["headers"]["X-Dalton-Bridge-Token"] == BRIDGE_TOKEN

    finished = json.loads(rpc_resp["result"]["content"][0]["text"])
    assert finished["ok"] is True
    assert CALENDAR_ID not in json.dumps(finished)
    assert "[calendar]" in finished["data"]["events"][0]["description"]
    assert finished["_sources"][0]["document_id"] == "gcal:evt-fwd-1"


@pytest.mark.asyncio
async def test_legacy_gws_empty_token_runs_gcal_run_without_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_GCAL_BACKEND", "legacy_gws")
    monkeypatch.delenv("DALTON_TRACKER_CALENDAR_OWNER_ID", raising=False)
    bridge = _build_local_bridge(tmp_path, monkeypatch)

    http_client = create_autospec(httpx.AsyncClient, instance=True)
    bridge._gcal_http = http_client

    gws_calls: list[list[str]] = []

    async def _fake_exec_gws(argv: list[str], timeout_s: float) -> tuple[int, bytes, bytes]:
        gws_calls.append(list(argv))
        return 0, json.dumps({"items": []}).encode("utf-8"), b""

    monkeypatch.setattr(bridge, "_exec_gws", create_autospec(bridge._exec_gws, side_effect=_fake_exec_gws))

    rpc_resp = await bridge.handle_tools_call(
        11,
        {
            "name": "research",
            "arguments": {
                "route": "gcal",
                "operation": "list_events",
                "time_min": "2026-11-14T00:00:00Z",
                "time_max": "2026-11-15T00:00:00Z",
            },
        },
        raw_caller_token="",
    )
    finished = json.loads(rpc_resp["result"]["content"][0]["text"])
    assert finished["ok"] is True
    assert http_client.post.call_count == 0
    assert len(gws_calls) == 1


@pytest.mark.asyncio
async def test_legacy_gws_owner_unconfigured_insert_and_health_check_succeed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DALTON_GCAL_BACKEND", raising=False)
    monkeypatch.delenv("DALTON_TRACKER_CALENDAR_OWNER_ID", raising=False)
    bridge = _build_local_bridge(tmp_path, monkeypatch)

    http_client = create_autospec(httpx.AsyncClient, instance=True)
    bridge._gcal_http = http_client

    async def _fake_exec_gws(argv: list[str], timeout_s: float) -> tuple[int, bytes, bytes]:
        if "insert" in argv:
            return (
                0,
                json.dumps(
                    {
                        "id": "evt-legacy-insert-1",
                        "summary": "Affirm (AFRM) Earnings — Q1 2027 [BMO]",
                        "description": "Scanned 2026-11-14\nCreated by Dalton earnings tracker",
                        "start": {"date": "2026-11-20"},
                        "end": {"date": "2026-11-21"},
                    }
                ).encode("utf-8"),
                b"",
            )
        return 0, json.dumps({"items": []}).encode("utf-8"), b""

    monkeypatch.setattr(bridge, "_exec_gws", create_autospec(bridge._exec_gws, side_effect=_fake_exec_gws))

    ins_resp = await bridge.handle_tools_call(
        12,
        {
            "name": "research",
            "arguments": {
                "route": "gcal",
                "operation": "insert_event",
                "summary": "Affirm (AFRM) Earnings — Q1 2027 [BMO]",
                "description": "Scanned 2026-11-14",
                "start": {"date": "2026-11-20"},
                "end": {"date": "2026-11-21"},
            },
        },
        raw_caller_token="",
    )
    ins_body = json.loads(ins_resp["result"]["content"][0]["text"])
    assert ins_body["ok"] is True
    assert ins_body["data"]["id"] == "evt-legacy-insert-1"

    hc_resp = await bridge.handle_tools_call(
        13,
        {"name": "research", "arguments": {"route": "gcal", "operation": "health_check"}},
        raw_caller_token=None,
    )
    hc_body = json.loads(hc_resp["result"]["content"][0]["text"])
    assert hc_body["ok"] is True
    assert hc_body["data"] == {"calendar_ok": True, "backend": "legacy_gws"}
    assert http_client.post.call_count == 0


@pytest.mark.asyncio
async def test_official_mcp_owner_unconfigured_returns_503_and_empty_token_never_forwards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_gate_env(monkeypatch, backend_mode="official_mcp")
    monkeypatch.delenv("DALTON_TRACKER_CALENDAR_OWNER_ID", raising=False)

    app = _make_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={"operation": "health_check", "arguments": {}},
            headers=_signed_headers(),
        )
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "tracker_owner_not_configured"

    bridge = _build_local_bridge(tmp_path, monkeypatch)
    http_client = create_autospec(httpx.AsyncClient, instance=True)
    bridge._gcal_http = http_client
    rpc_resp = await bridge.handle_tools_call(
        14,
        {"name": "research", "arguments": {"route": "gcal", "operation": "health_check"}},
        raw_caller_token="",
    )
    body = json.loads(rpc_resp["result"]["content"][0]["text"])
    assert body["ok"] is False
    assert body["error_code"] == "invalid_caller_token"
    assert http_client.post.call_count == 0


@pytest.mark.asyncio
async def test_official_mcp_rejects_missing_or_invalid_automation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_gate_env(monkeypatch, backend_mode="official_mcp")
    app = _make_app()
    transport = httpx.ASGITransport(app=app)

    for bad_auto in ("", "not-a-valid-uuid"):
        if bad_auto:
            monkeypatch.setenv("DALTON_TRACKER_CALENDAR_AUTOMATION_ID", bad_auto)
        else:
            monkeypatch.delenv("DALTON_TRACKER_CALENDAR_AUTOMATION_ID", raising=False)

        direct = await tc.authorize_tracker_calendar_execution(
            user_id=OWNER_ID,
            workspace_id=WS_ID,
            thread_id=THREAD_ID,
            run_id=RUN_ID,
            automation_id=AUTO_ID,
            execution_id=EXEC_ID,
        )
        assert direct == {"ok": False, "code": "tracker_automation_not_configured"}

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r_list = await client.post(
                "/api/v1/dalton/mcp/tracker-calendar",
                json={
                    "operation": "list_events",
                    "arguments": {
                        "time_min": "2026-11-14T00:00:00Z",
                        "time_max": "2026-11-15T00:00:00Z",
                    },
                },
                headers=_signed_headers(),
            )
            assert r_list.status_code == 503
            assert r_list.json()["detail"]["code"] == "tracker_automation_not_configured"


# ---------------------------------------------------------------------------
# 4. Batch 1 narrowed invariants: self-test health_check & Step 1 disconnect flag
# ---------------------------------------------------------------------------


def test_richard_earnings_self_test_calls_health_check_and_preserves_universe_ok(
    tmp_path: Path,
) -> None:
    mod = _load_richard_earnings_module()
    ws = tmp_path / "ws"
    ws.mkdir()

    seen_calls: list[dict[str, Any]] = []

    def _fake_call_research(**kwargs: Any) -> dict[str, Any]:
        seen_calls.append(dict(kwargs))
        return {
            "ok": True,
            "status": "ok",
            "data": {"calendar_ok": True, "backend": "legacy_gws"},
        }

    orig_call = mod.call_research
    orig_now = mod.now_et
    mod.call_research = create_autospec(mod.call_research, side_effect=_fake_call_research)
    mod.now_et = lambda: datetime(2026, 11, 14, 7, 0, tzinfo=ZoneInfo("America/New_York"))
    try:
        ns = argparse.Namespace(workspace_dir=str(ws))
        out = mod.cmd_self_test(ns)
    finally:
        mod.call_research = orig_call
        mod.now_et = orig_now

    assert seen_calls == [{"route": "gcal", "operation": "health_check"}]
    assert out["ok"] is True
    assert out["calendar_ok"] is True
    assert out["universe_ok"] is True
    assert out["universe_count"] == 105
    assert out["backend"] == "legacy_gws"


def test_batch1_registers_no_tracker_purge_handler_and_no_reconcile_routes() -> None:
    """Narrowed Batch 1 keeps Step 1's ledger flag convention (no registered purge handler)
    and removes the extra reconcile endpoints.
    """
    assert get_tracker_purge_handler() is None
    route_paths = {route.path for route in tc_router.router.routes}
    assert route_paths == {"/api/v1/dalton/mcp/tracker-calendar"}


# ---------------------------------------------------------------------------
# 5. Ops probe: prod-readonly-check, bridge runtime-info, DB name & mail/calendar drain
# ---------------------------------------------------------------------------


def test_probe_parser_exposes_only_prod_readonly_check_and_drain() -> None:
    probe = _load_probe_module()
    parser = probe.build_parser()
    subparsers_actions = [
        action for action in parser._actions if hasattr(action, "choices") and isinstance(action.choices, dict)
    ]
    assert len(subparsers_actions) == 1
    assert set(subparsers_actions[0].choices.keys()) == {
        "prod-readonly-check",
        "calendar",
        "drain",
    }


def test_probe_extract_database_name_parses_libpq_and_query_override() -> None:
    probe = _load_probe_module()
    assert (
        probe.extract_database_name("postgresql://u:p@localhost:5432/langalpha_test?dbname=langalpha")
        == "langalpha"
    )
    assert probe.extract_database_name("host=localhost dbname=langalpha_test") == "langalpha_test"


@pytest.mark.asyncio
async def test_probe_open_db_pool_opens_and_closes_when_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.database import pool as db_pool

    probe = _load_probe_module()
    fake_pool = create_autospec(AsyncConnectionPool, instance=True)
    fake_pool.closed = True
    monkeypatch.setattr(
        db_pool,
        "get_or_create_pool",
        create_autospec(db_pool.get_or_create_pool, return_value=fake_pool),
    )

    async with probe.open_probe_db_pool() as yielded:
        assert yielded is fake_pool
        assert fake_pool.open.await_count == 1
        assert fake_pool.close.await_count == 0
    assert fake_pool.close.await_count == 1


@pytest.mark.asyncio
async def test_probe_prod_readonly_check_skips_write_roundtrip_when_readonly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _load_probe_module()
    ro_row = _make_integration_row(
        scopes=f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS_RO}"
    )

    @asynccontextmanager
    async def _noop_pool() -> Any:
        yield None

    monkeypatch.setattr(probe, "open_probe_db_pool", _noop_pool)
    monkeypatch.setattr(
        integrations_db,
        "get_connection",
        create_autospec(integrations_db.get_connection, return_value=ro_row),
    )
    monkeypatch.setattr(
        tc,
        "ensure_fresh_access_token",
        create_autospec(
            tc.ensure_fresh_access_token,
            return_value=FreshAccessToken(
                access_token="ya29.ro-probe",
                provider="google",
                service="calendar",
                oauth_client_id=ro_row.oauth_client_id,
                provider_account_id="sub-owner-1",
                auth_epoch=1,
                token_generation=1,
                granted_scopes=f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS_RO}",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ),
        ),
    )
    mcp_client = create_autospec(GoogleCalendarMCPClient, instance=True)
    mcp_client.call_mcp_tool.return_value = {"events": []}

    async def _bridge_info() -> dict[str, Any]:
        return {
            "ok": True,
            "gcal_backend": "legacy_gws",
            "calendar_id": CALENDAR_ID,
            "host_gws_account": "owner@example.test",
        }

    code, payload = await probe.run_prod_readonly_check(
        owner_id=OWNER_ID,
        expected_calendar_id=CALENDAR_ID,
        bridge_runtime_resolver=create_autospec(_bridge_info, side_effect=_bridge_info),
        google_client=mcp_client,
        now_et=datetime(2026, 11, 14, 9, 0, tzinfo=ZoneInfo("America/New_York")),
    )
    assert code == 0
    assert payload["ok"] is True
    assert payload["bridge_reachable"] is True
    assert payload["account_matched"] is True
    assert payload["calendar_id_matched"] is True
    assert payload["health_check"]["ok"] is True
    assert payload["health_check"]["backend"] == "official_mcp"
    assert payload["list_events_400d"]["ok"] is True
    assert payload["write_roundtrip"] == {
        "status": "skipped",
        "reason": "跳过：需要写权限",
    }
    assert mcp_client.call_mcp_tool.call_count == 2


@pytest.mark.asyncio
async def test_probe_prod_readonly_check_aborts_before_mcp_on_account_or_calendar_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _load_probe_module()
    ro_row = _make_integration_row(
        scopes=f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS_RO}",
        calendar_id=CALENDAR_ID,
    )

    @asynccontextmanager
    async def _noop_pool() -> Any:
        yield None

    monkeypatch.setattr(probe, "open_probe_db_pool", _noop_pool)
    monkeypatch.setattr(
        integrations_db,
        "get_connection",
        create_autospec(integrations_db.get_connection, return_value=ro_row),
    )

    mcp_client = create_autospec(GoogleCalendarMCPClient, instance=True)

    async def _wrong_acct() -> dict[str, Any]:
        return {
            "ok": True,
            "gcal_backend": "legacy_gws",
            "calendar_id": CALENDAR_ID,
            "host_gws_account": "someone-else@example.test",
        }

    code_acct, body_acct = await probe.run_prod_readonly_check(
        owner_id=OWNER_ID,
        expected_calendar_id=CALENDAR_ID,
        bridge_runtime_resolver=create_autospec(_wrong_acct, side_effect=_wrong_acct),
        google_client=mcp_client,
    )
    assert code_acct != 0
    assert body_acct["ok"] is False
    assert body_acct["account_matched"] is False
    assert mcp_client.call_mcp_tool.call_count == 0

    async def _wrong_cal() -> dict[str, Any]:
        return {
            "ok": True,
            "gcal_backend": "legacy_gws",
            "calendar_id": "different-calendar@group.calendar.google.com",
            "host_gws_account": "owner@example.test",
        }

    code_cal, body_cal = await probe.run_prod_readonly_check(
        owner_id=OWNER_ID,
        expected_calendar_id=CALENDAR_ID,
        bridge_runtime_resolver=create_autospec(_wrong_cal, side_effect=_wrong_cal),
        google_client=mcp_client,
    )
    assert code_cal != 0
    assert body_cal["ok"] is False
    assert body_cal["account_matched"] is True
    assert body_cal["calendar_id_matched"] is False
    assert mcp_client.call_mcp_tool.call_count == 0


@pytest.mark.asyncio
async def test_probe_prod_readonly_check_queries_running_bridge_runtime_info_before_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _load_probe_module()
    bridge = _build_local_bridge(tmp_path, monkeypatch)
    monkeypatch.setenv("DALTON_GCAL_CALENDAR_ID", "bridge-actual-cal@group.calendar.google.com")
    monkeypatch.setenv("DALTON_BRIDGE_URL", "http://127.0.0.1:8960")
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", BRIDGE_TOKEN)

    async def _fake_exec_gws(argv: list[str], timeout_s: float) -> tuple[int, bytes, bytes]:
        if argv[1:3] == ["auth", "status"]:
            return 0, json.dumps({"email": "owner@example.test"}).encode("utf-8"), b""
        return 1, b"", b""

    monkeypatch.setattr(bridge, "_exec_gws", create_autospec(bridge._exec_gws, side_effect=_fake_exec_gws))

    code_rt, rt_payload = await bridge.handle_tracker_calendar_runtime_info()
    assert code_rt == 200
    assert rt_payload["ok"] is True
    assert rt_payload["host_gws_account"] == "owner@example.test"
    assert rt_payload["calendar_id"] == "bridge-actual-cal@group.calendar.google.com"
    assert rt_payload["gcal_backend"] == "legacy_gws"

    http_client = create_autospec(httpx.AsyncClient, instance=True)

    async def _fake_get(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: Any = None,
    ) -> httpx.Response:
        assert url == "http://127.0.0.1:8960/internal/tracker-calendar/runtime-info"
        c, p = await bridge.handle_tracker_calendar_runtime_info()
        return httpx.Response(c, json=p)

    http_client.get.side_effect = _fake_get

    @asynccontextmanager
    async def _noop_pool() -> Any:
        yield None

    monkeypatch.setattr(probe, "open_probe_db_pool", _noop_pool)
    ro_row = _make_integration_row(
        scopes=f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS_RO}",
        calendar_id=CALENDAR_ID,
    )
    monkeypatch.setattr(
        integrations_db,
        "get_connection",
        create_autospec(integrations_db.get_connection, return_value=ro_row),
    )
    mcp_client = create_autospec(GoogleCalendarMCPClient, instance=True)

    code_mismatch, body_mismatch = await probe.run_prod_readonly_check(
        owner_id=OWNER_ID,
        expected_calendar_id=CALENDAR_ID,
        http_client=http_client,
        google_client=mcp_client,
    )
    assert code_mismatch != 0
    assert body_mismatch["calendar_id_matched"] is False
    assert mcp_client.call_mcp_tool.call_count == 0

    async def _unreachable() -> None:
        return None

    code_down, body_down = await probe.run_prod_readonly_check(
        owner_id=OWNER_ID,
        bridge_runtime_resolver=create_autospec(_unreachable, side_effect=_unreachable),
        google_client=mcp_client,
    )
    assert code_down != 0
    assert body_down["bridge_reachable"] is False
    assert mcp_client.call_mcp_tool.call_count == 0


@pytest.mark.asyncio
async def test_probe_drain_returns_zero_when_idle_and_two_on_timeout() -> None:
    probe = _load_probe_module()

    async def _zero(_ids: list[str]) -> int:
        return 0

    async def _one(_ids: list[str]) -> int:
        return 1

    zero_mock = create_autospec(_zero, side_effect=_zero)
    code_ok, body_ok = await probe.run_drain(
        target="calendar",
        automation_ids=[AUTO_ID],
        wait_seconds=1,
        poll_interval=0.05,
        count_running_fn=zero_mock,
    )
    assert code_ok == 0
    assert body_ok["drained"] is True

    one_mock = create_autospec(_one, side_effect=_one)
    code_to, body_to = await probe.run_drain(
        target="calendar",
        automation_ids=[AUTO_ID],
        wait_seconds=0,
        poll_interval=0.05,
        count_running_fn=one_mock,
    )
    assert code_to == 2
    assert body_to["drained"] is False
    assert body_to["running_count"] == 1


@pytest.mark.asyncio
async def test_probe_drain_mail_checks_prepare_and_execution_locks_and_fails_on_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _load_probe_module()
    state_root = tmp_path / "md-state"
    locks_dir = state_root / "locks"
    locks_dir.mkdir(parents=True)

    research_home = tmp_path / "research-home"
    (research_home / "state" / "research-images").mkdir(parents=True)
    bridge_env = tmp_path / "researchdata-bridge.env"
    bridge_env.write_text(f"DALTON_RESEARCH_HOME={research_home}\n", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(bridge_env))
    monkeypatch.delenv("DALTON_RESEARCH_HOME", raising=False)
    monkeypatch.delenv("DALTON_MARKET_DIGEST_PREPARE_CONFIG", raising=False)
    monkeypatch.delenv("DALTON_MARKET_DIGEST_STATE_ROOT", raising=False)

    prepare_cfg = tmp_path / "market-digest-prepare.json"
    prepare_cfg.write_text(json.dumps({"state_root": str(state_root)}), encoding="utf-8")

    async def _zero(_ids: list[str]) -> int:
        return 0

    zero_mock = create_autospec(_zero, side_effect=_zero)

    am_lock = locks_dir / "prepare_AM.lock"
    am_lock.write_text("", encoding="utf-8")
    with open(am_lock, "r+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            code_am, body_am = await probe.run_drain(
                target="mail",
                automation_ids=[AUTO_ID],
                wait_seconds=0,
                poll_interval=0.05,
                prepare_config_path=prepare_cfg,
                count_running_fn=zero_mock,
            )
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    assert code_am == 2
    assert "locks/prepare_AM.lock" in body_am["active_locks"]

    exec_lock = locks_dir / f"execution_{EXEC_ID}.lock"
    exec_lock.write_text("", encoding="utf-8")
    with open(exec_lock, "r+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            code_ex, body_ex = await probe.run_drain(
                target="mail",
                automation_ids=[AUTO_ID],
                wait_seconds=0,
                poll_interval=0.05,
                prepare_config_path=prepare_cfg,
                count_running_fn=zero_mock,
            )
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    assert code_ex == 2
    assert f"locks/execution_{EXEC_ID}.lock" in body_ex["active_locks"]

    code_ok, body_ok = await probe.run_drain(
        target="mail",
        automation_ids=[AUTO_ID],
        wait_seconds=0,
        poll_interval=0.05,
        prepare_config_path=prepare_cfg,
        count_running_fn=zero_mock,
    )
    assert code_ok == 0
    assert body_ok["drained"] is True

    bad_link = locks_dir / "prepare_PM.lock"
    bad_link.symlink_to(tmp_path / "nonexistent")
    code_bad, body_bad = await probe.run_drain(
        target="mail",
        automation_ids=[AUTO_ID],
        wait_seconds=0,
        poll_interval=0.05,
        prepare_config_path=prepare_cfg,
        count_running_fn=zero_mock,
    )
    assert code_bad == 1
    assert body_bad["ok"] is False
    bad_link.unlink()

    am_lock.chmod(0o000)
    try:
        code_perm, body_perm = await probe.run_drain(
            target="mail",
            automation_ids=[AUTO_ID],
            wait_seconds=0,
            poll_interval=0.05,
            prepare_config_path=prepare_cfg,
            count_running_fn=zero_mock,
        )
    finally:
        am_lock.chmod(0o600)
    assert code_perm == 1
    assert body_perm["ok"] is False


@pytest.mark.asyncio
async def test_probe_drain_mail_resolves_both_prepare_and_worker_locks_from_host_bridge_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex R3 Medium fix: running ``drain --target mail`` with default §7.4 args
    resolves BOTH prepare state_root and DALTON_RESEARCH_HOME from researchdata-bridge.env.
    """
    probe = _load_probe_module()
    state_root = tmp_path / "md-state"
    locks_dir = state_root / "locks"
    locks_dir.mkdir(parents=True)
    am_lock = locks_dir / "prepare_AM.lock"
    am_lock.write_text("", encoding="utf-8")

    prepare_cfg = tmp_path / "market-digest-prepare.json"
    prepare_cfg.write_text(json.dumps({"state_root": str(state_root)}), encoding="utf-8")

    research_home = tmp_path / "host-research-home"
    images_dir = research_home / "state" / "research-images"
    images_dir.mkdir(parents=True)
    worker_lock = images_dir / "worker.lock"
    worker_lock.write_text("", encoding="utf-8")

    bridge_env = tmp_path / "researchdata-bridge.env"
    bridge_env.write_text(
        f"DALTON_MARKET_DIGEST_PREPARE_CONFIG={prepare_cfg}\n"
        f"DALTON_RESEARCH_HOME={research_home}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(bridge_env))
    monkeypatch.delenv("DALTON_RESEARCH_HOME", raising=False)
    monkeypatch.delenv("DALTON_MARKET_DIGEST_PREPARE_CONFIG", raising=False)
    monkeypatch.delenv("DALTON_MARKET_DIGEST_STATE_ROOT", raising=False)

    async def _zero(_ids: list[str]) -> int:
        return 0

    zero_mock = create_autospec(_zero, side_effect=_zero)

    # 1) Held prepare_AM.lock detected purely via researchdata-bridge.env
    with open(am_lock, "r+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            code_prep, body_prep = await probe.run_drain(
                target="mail",
                automation_ids=[AUTO_ID],
                wait_seconds=0,
                poll_interval=0.05,
                count_running_fn=zero_mock,
            )
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    assert code_prep == 2
    assert "locks/prepare_AM.lock" in body_prep["active_locks"]

    # 2) Held worker.lock detected purely via researchdata-bridge.env
    with open(worker_lock, "r+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            code_worker, body_worker = await probe.run_drain(
                target="mail",
                automation_ids=[AUTO_ID],
                wait_seconds=0,
                poll_interval=0.05,
                count_running_fn=zero_mock,
            )
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    assert code_worker == 2
    assert "state/research-images/worker.lock" in body_worker["active_locks"]

    # 3) Both locks idle -> exit 0
    code_ok, body_ok = await probe.run_drain(
        target="mail",
        automation_ids=[AUTO_ID],
        wait_seconds=0,
        poll_interval=0.05,
        count_running_fn=zero_mock,
    )
    assert code_ok == 0
    assert body_ok["drained"] is True
