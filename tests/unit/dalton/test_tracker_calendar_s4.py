"""Step 4 unit tests: tracker calendar gate, caller token signing, bridge forwarding, probe guards, and purge handler.

All test doubles use ``create_autospec``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

from fastapi import FastAPI
import httpx
import pytest

from ptc_agent.agent.tools.code_execution import _bound_caller_identity
from src.dalton.app import tracker_calendar_router as tc_router
from src.dalton.database import user_integrations as integrations_db
from src.dalton.database.user_integrations import (
    IntegrationRow,
    IntegrationStatus,
    LocalPurgeStatus,
    PurgeLedgerRow,
    RemoteRevokeStatus,
    TriggerReason,
)
from src.dalton.integrations import tracker_calendar as tc
from src.dalton.integrations.constants import (
    GOOGLE_SCOPE_CALENDAR_EVENTS,
    GOOGLE_SCOPE_CALENDAR_EVENTS_RO,
    GOOGLE_SCOPE_CALENDAR_LIST_RO,
)
from src.dalton.integrations.google_calendar import GoogleCalendarMCPClient
from src.dalton.integrations.oauth_clients import GoogleOAuthClient
from src.dalton.sandbox_caller_identity import (
    caller_env,
    sign_caller_token,
    verify_caller_token,
)
from src.dalton.services.integration_data_purge import (
    get_tracker_purge_handler,
    register_tracker_purge_handler,
)
from src.dalton.services.user_integrations_oauth import FreshAccessToken

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402
import source_identity  # noqa: E402

OWNER_ID = "991a99ad-8ff2-4bd8-bf08-e5963a248798"
TEST_OWNER_ID = "11111111-2222-3333-4444-555555555555"
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


def _make_purge_ledger(
    *,
    user_id: str = OWNER_ID,
    provider: str = "google",
    service: str | None = "calendar",
    trigger_reason: str = TriggerReason.USER_DISCONNECT,
    revoke_token: str | None = "1//revoke-token",
) -> PurgeLedgerRow:
    now = datetime.now(UTC)
    return PurgeLedgerRow(
        purge_id="99999999-9999-4999-8999-999999999999",
        user_id=user_id,
        provider=provider,
        service=service,
        oauth_client_id="123456-test.apps.googleusercontent.com",
        provider_account_id="sub-owner-1",
        auth_epoch_upto=1,
        trigger_reason=trigger_reason,
        local_purge_status=LocalPurgeStatus.RUNNING,
        purged_max_turn_by_thread={},
        tracker_reconcile_needed=False,
        local_completed_at=None,
        remote_revoke_status=RemoteRevokeStatus.PENDING,
        has_revoke_token=bool(revoke_token),
        retry_count=0,
        next_retry_at=None,
        last_remote_error=None,
        remote_completed_at=None,
        disconnected_at=now,
        completed_at=None,
        revoke_token=revoke_token,
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

    # Expired token rejected
    assert verify_caller_token(valid, admin_token=BRIDGE_TOKEN, now_ts=now + 61) is None

    # Forged with wrong secret rejected
    assert verify_caller_token(valid, admin_token="wrong-secret", now_ts=now) is None

    # Tampered run_id inside payload rejected
    import base64

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


@pytest.mark.asyncio
async def test_tracker_calendar_gate_rejects_forged_expired_tampered_and_unknown_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_gate_env(monkeypatch, backend_mode="official_mcp")
    app = _make_app()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Forged caller token
        r_forged = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={"operation": "health_check", "arguments": {}},
            headers=_signed_headers(admin_token_for_sig="forged-secret"),
        )
        assert r_forged.status_code == 401

        # Expired caller token
        r_expired = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={"operation": "health_check", "arguments": {}},
            headers=_signed_headers(exp=100),
        )
        assert r_expired.status_code == 401

        # Tampered run_id (re-signed with mismatched run_id vs running PG execution)
        authz_mock = create_autospec(
            tc.authorize_tracker_calendar_execution,
            return_value={"ok": False, "code": "run_mismatch"},
        )
        monkeypatch.setattr(tc_router, "authorize_tracker_calendar_execution", authz_mock)
        r_tampered = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={
                "operation": "list_events",
                "arguments": {
                    "time_min": "2026-11-14T00:00:00Z",
                    "time_max": "2026-11-15T00:00:00Z",
                },
            },
            headers=_signed_headers(run_id="99999999-9999-9999-9999-999999999999"),
        )
        assert r_tampered.status_code == 403
        assert r_tampered.json()["detail"]["code"] == "run_mismatch"

        # Unsupported operation returns HTTP 400
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
    get_conn_mock = create_autospec(integrations_db.get_connection, return_value=write_row)
    monkeypatch.setattr(integrations_db, "get_connection", get_conn_mock)

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
        # 1) list_events spanning EST (-05:00) and EDT (-04:00)
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

        # 2) insert_event -> create_event
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

        # 3a) delete_event accepts legacy marker via _gcal_is_tracker
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

        # 3b) delete_event refuses non-tracker event without calling delete_event
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

        # 4) health_check uses pageSize=1
        r_hc = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={"operation": "health_check", "arguments": {}},
            headers=headers,
        )
        assert r_hc.status_code == 200
        assert r_hc.json()["payload"] == {
            "ok": True,
            "status": "ok",
            "data": {"calendar_ok": True},
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
    get_conn_mock = create_autospec(integrations_db.get_connection, return_value=ro_row)
    monkeypatch.setattr(integrations_db, "get_connection", get_conn_mock)

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
        # insert_event refused at entry without calling Google MCP
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

        # delete_event refused at entry without calling Google MCP
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

        # health_check & list_events work normally under read-only grant
        r_hc = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={"operation": "health_check", "arguments": {}},
            headers=headers,
        )
        assert r_hc.status_code == 200
        assert r_hc.json()["payload"]["data"] == {"calendar_ok": True}

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

    # Build a real ResearchDataBridge instance for the legacy-gws callback
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("DALTON_BRIDGE_TEST_MODE", "0")
    empty_env = tmp_path / "bridge.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(empty_env))
    gws_bin = tmp_path / "gws"
    gws_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("DALTON_GWS_BIN", str(gws_bin))
    bridge = rb.ResearchDataBridge(rb.BridgeConfig())

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
    assert body["payload"] == {"ok": True, "status": "ok", "data": {"calendar_ok": True}}
    assert body["calendar_id"] == CALENDAR_ID
    assert len(captured_posts) == 1
    assert captured_posts[0]["url"] == "http://127.0.0.1:8960/internal/tracker-calendar/legacy-gws"
    assert captured_posts[0]["json"]["timeout_s"] == 25.0  # clamped to min(eff_timeout, 25.0)
    assert len(gws_calls) == 1
    assert gws_calls[0][1:4] == ["calendar", "events", "list"]
    params = json.loads(gws_calls[0][5])
    assert params["maxResults"] == 1
    assert params["calendarId"] == CALENDAR_ID

    # source_identity returns empty sources for health_check
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
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("DALTON_BRIDGE_TEST_MODE", "0")
    empty_env = tmp_path / "bridge.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(empty_env))
    bridge = rb.ResearchDataBridge(rb.BridgeConfig())

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


# ---------------------------------------------------------------------------
# 4. Probe script non-production guard rejections (all 3 guards) + readonly check + drain
# ---------------------------------------------------------------------------


def test_probe_drill_rejects_non_test_database_name() -> None:
    probe = _load_probe_module()
    code, payload = asyncio.run(
        probe.run_calendar_purge_drill(
            database_url="postgresql://user:pass@localhost:5432/langalpha",
            owner_id=TEST_OWNER_ID,
            calendar_id="test-subcal@group.calendar.google.com",
            dry_run=True,
        )
    )
    assert code != 0
    assert payload["guard_failed"] is True
    assert any("_test" in r for r in payload["reasons"])


def test_probe_drill_rejects_production_owner_id() -> None:
    probe = _load_probe_module()
    code, payload = asyncio.run(
        probe.run_calendar_purge_drill(
            database_url="postgresql://user:pass@localhost:5432/langalpha_test",
            owner_id=probe.PROD_TRACKER_CALENDAR_OWNER_ID,
            calendar_id="test-subcal@group.calendar.google.com",
            dry_run=True,
        )
    )
    assert code != 0
    assert payload["guard_failed"] is True
    assert any("production tracker owner" in r for r in payload["reasons"])


def test_probe_drill_rejects_non_test_calendar_target() -> None:
    probe = _load_probe_module()
    code_primary, payload_primary = asyncio.run(
        probe.run_calendar_purge_drill(
            database_url="postgresql://user:pass@localhost:5432/langalpha_test",
            owner_id=TEST_OWNER_ID,
            calendar_id="primary",
            dry_run=True,
        )
    )
    assert code_primary != 0
    assert payload_primary["guard_failed"] is True

    code_unmarked, payload_unmarked = asyncio.run(
        probe.run_calendar_purge_drill(
            database_url="postgresql://user:pass@localhost:5432/langalpha_test",
            owner_id=TEST_OWNER_ID,
            calendar_id="richard-prod-earnings@group.calendar.google.com",
            dry_run=True,
        )
    )
    assert code_unmarked != 0
    assert payload_unmarked["guard_failed"] is True


def test_probe_drill_accepts_valid_non_prod_configuration() -> None:
    probe = _load_probe_module()
    code, payload = asyncio.run(
        probe.run_calendar_purge_drill(
            database_url="postgresql://user:pass@localhost:5432/langalpha_test",
            owner_id=TEST_OWNER_ID,
            calendar_id="cal-drill-subcal@group.calendar.google.com",
            dry_run=True,
        )
    )
    assert code == 0
    assert payload["ok"] is True
    assert payload["guard_passed"] is True


@pytest.mark.asyncio
async def test_probe_prod_readonly_check_skips_write_roundtrip_when_readonly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _load_probe_module()
    ro_row = _make_integration_row(
        scopes=f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS_RO}"
    )
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

    code, payload = await probe.run_prod_readonly_check(
        owner_id=OWNER_ID,
        expected_calendar_id=CALENDAR_ID,
        google_client=mcp_client,
        now_et=datetime(2026, 11, 14, 9, 0, tzinfo=ZoneInfo("America/New_York")),
    )
    assert code == 0
    assert payload["ok"] is True
    assert payload["health_check"]["ok"] is True
    assert payload["list_events_400d"]["ok"] is True
    assert payload["write_roundtrip"] == {
        "status": "skipped",
        "reason": "跳过：需要写权限",
    }
    assert mcp_client.call_mcp_tool.call_count == 2


@pytest.mark.asyncio
async def test_probe_drain_returns_zero_when_idle_and_two_on_timeout(tmp_path: Path) -> None:
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


# ---------------------------------------------------------------------------
# 5. Tracker purge handler: two paths (write-scoped delete vs read-only reconcile)
# ---------------------------------------------------------------------------


def _seed_tracker_workspace(ws: Path) -> tuple[Path, Path, Path]:
    state = ws / "state" / "richard-earnings"
    scan_day = state / "scans" / "2026-11-14"
    scan_day.mkdir(parents=True)
    wl_path = state / "watchlist.json"
    wl_path.write_text(
        json.dumps(
            {
                "updated": "2026-11-14",
                "confirmed": [
                    {"ticker": "AFRM", "name": "Affirm", "date": "2026-11-20", "timing": "BMO", "calendar_event_id": "evt-tracker-1"}
                ],
                "pending": [],
                "reported": [{"ticker": "PDD", "calendar_event_id": "evt-old-rep"}],
                "pending_delete_event_ids": ["evt-pending-del"],
            }
        ),
        encoding="utf-8",
    )
    apply_path = scan_day / "apply.json"
    apply_path.write_text(
        json.dumps(
            {
                "planned": [{"ticker": "AFRM", "old_event_id": "evt-prev-1"}],
                "applied": [{"ticker": "AFRM", "event_id": "evt-tracker-1"}],
            }
        ),
        encoding="utf-8",
    )
    roll_path = scan_day / "roll.json"
    roll_path.write_text(
        json.dumps({"moved": [{"ticker": "PDD", "calendar_event_id": "evt-old-rep"}]}),
        encoding="utf-8",
    )
    return wl_path, apply_path, roll_path


@pytest.mark.asyncio
async def test_tracker_purge_handler_deletes_remote_tracker_events_and_local_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert get_tracker_purge_handler() is tc.handle_tracker_calendar_purge
    monkeypatch.setenv("DALTON_TRACKER_CALENDAR_OWNER_ID", OWNER_ID)
    wl_path, apply_path, roll_path = _seed_tracker_workspace(tmp_path)

    write_row = _make_integration_row(
        scopes=f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS}"
    )
    monkeypatch.setattr(
        integrations_db,
        "get_connection",
        create_autospec(integrations_db.get_connection, return_value=write_row),
    )

    mcp_client = create_autospec(GoogleCalendarMCPClient, instance=True)
    deleted_ids: list[str] = []

    async def _fake_mcp(
        *,
        access_token: str,
        tool_name: str,
        arguments: dict[str, Any],
        request_state: str | None = None,
        input_responses: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if tool_name == "list_events":
            return {
                "events": [
                    {
                        "id": "evt-tracker-1",
                        "summary": "Affirm (AFRM) Earnings — Q1 2027 [BMO]",
                        "description": "Created by Dalton earnings tracker",
                        "start": {"date": "2026-11-20"},
                        "end": {"date": "2026-11-21"},
                    },
                    {
                        "id": "evt-personal-keep",
                        "summary": "Dentist appointment",
                        "description": "personal",
                        "start": {"date": "2026-11-22"},
                        "end": {"date": "2026-11-23"},
                    },
                ]
            }
        if tool_name == "delete_event":
            deleted_ids.append(arguments["eventId"])
            return {"deleted": True}
        raise AssertionError(tool_name)

    mcp_client.call_mcp_tool.side_effect = _fake_mcp

    handled = await tc.handle_tracker_calendar_purge(
        _make_purge_ledger(),
        workspace_dirs=[tmp_path],
        google_client=mcp_client,
    )
    assert handled is True
    assert deleted_ids == ["evt-tracker-1"]

    wl = json.loads(wl_path.read_text(encoding="utf-8"))
    assert wl["confirmed"][0]["calendar_event_id"] is None
    assert wl["reported"][0]["calendar_event_id"] is None
    assert wl["pending_delete_event_ids"] == []
    assert "tracker_reconcile_needed" not in wl

    apply_doc = json.loads(apply_path.read_text(encoding="utf-8"))
    assert apply_doc["planned"][0]["old_event_id"] is None
    assert apply_doc["applied"][0]["event_id"] is None

    roll_doc = json.loads(roll_path.read_text(encoding="utf-8"))
    assert roll_doc["moved"][0]["calendar_event_id"] is None


@pytest.mark.asyncio
async def test_tracker_purge_handler_readonly_marks_reconcile_and_keeps_local_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_TRACKER_CALENDAR_OWNER_ID", OWNER_ID)
    wl_path, apply_path, roll_path = _seed_tracker_workspace(tmp_path)

    ro_row = _make_integration_row(
        scopes=f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS_RO}"
    )
    monkeypatch.setattr(
        integrations_db,
        "get_connection",
        create_autospec(integrations_db.get_connection, return_value=ro_row),
    )

    mcp_client = create_autospec(GoogleCalendarMCPClient, instance=True)
    oauth_client = create_autospec(GoogleOAuthClient, instance=True)

    handled = await tc.handle_tracker_calendar_purge(
        _make_purge_ledger(),
        workspace_dirs=[tmp_path],
        google_client=mcp_client,
        oauth_client=oauth_client,
    )
    # Returning False instructs run_local_purge to set tracker_reconcile_needed=TRUE without raising
    assert handled is False
    assert mcp_client.call_mcp_tool.call_count == 0

    wl = json.loads(wl_path.read_text(encoding="utf-8"))
    assert wl["confirmed"][0]["calendar_event_id"] is None
    assert wl["tracker_reconcile_needed"] is True

    apply_doc = json.loads(apply_path.read_text(encoding="utf-8"))
    assert apply_doc["applied"][0]["event_id"] is None

    roll_doc = json.loads(roll_path.read_text(encoding="utf-8"))
    assert roll_doc["moved"][0]["calendar_event_id"] is None


# ---------------------------------------------------------------------------
# 6. richard_earnings.py reconnect first-run stale event reclamation
# ---------------------------------------------------------------------------


def test_richard_earnings_reconnect_reclaims_stale_event_when_old_id_cleared(
    tmp_path: Path,
) -> None:
    mod = _load_richard_earnings_module()
    ws = tmp_path / "ws"
    (ws / "state" / "richard-earnings" / "scans" / "2026-11-14").mkdir(parents=True)
    (ws / "state" / "richard-earnings" / "watchlist.json").write_text(
        json.dumps(
            {
                "updated": "2026-11-10",
                "tracker_reconcile_needed": True,
                "confirmed": [
                    {
                        "ticker": "AFRM",
                        "name": "Affirm",
                        "date": "2026-11-20",
                        "timing": "BMO",
                        "quarter": "Q1 2027",
                        "calendar_event_id": None,
                        "source": "https://ir.example.test",
                        "confidence": "high",
                    }
                ],
                "pending": [],
                "reported": [],
            }
        ),
        encoding="utf-8",
    )
    (ws / "state" / "richard-earnings" / "scans" / "2026-11-14" / "A.jsonl").write_text(
        json.dumps(
            {
                "ticker": "AFRM",
                "name": "Affirm",
                "status": "announced",
                "earnings_date": "2026-11-25",
                "timing": "AMC",
                "quarter_label": "Q1 2027",
                "confidence": "high",
                "source_url": "https://ir.example.test/new",
                "evidence": "press release",
                "notes": None,
                "group": "A",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    calls: list[dict[str, Any]] = []
    responses = [
        # 1. list_events on new date 2026-11-25 -> empty
        {"ok": True, "status": "ok", "data": {"events": []}},
        # 2. insert_event on new date
        {"ok": True, "status": "ok", "data": {"id": "evt-new-25"}},
        # 3. list_events on old date 2026-11-20 to reclaim stale event
        {
            "ok": True,
            "status": "ok",
            "data": {
                "events": [
                    {
                        "id": "evt-stale-20",
                        "summary": "Affirm (AFRM) Earnings — Q1 2027 [BMO]",
                        "description": "Created by Dalton earnings tracker",
                        "start": {"dateTime": "2026-11-20T08:00:00-05:00", "timeZone": "America/New_York"},
                        "end": {"dateTime": "2026-11-20T08:30:00-05:00", "timeZone": "America/New_York"},
                    }
                ]
            },
        },
        # 4. delete_event on stale event
        {"ok": True, "status": "ok", "data": {"deleted": True, "id": "evt-stale-20"}},
    ]

    orig_call = mod.call_research
    orig_now = mod.now_et

    def _fake_research(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return responses.pop(0)

    try:
        import argparse

        mod.now_et = create_autospec(
            orig_now,
            return_value=datetime(2026, 11, 14, 7, 0, tzinfo=ZoneInfo("America/New_York")),
        )
        mod.call_research = create_autospec(orig_call, side_effect=_fake_research)
        ns = argparse.Namespace(workspace_dir=str(ws))
        result = mod.cmd_apply(ns)
    finally:
        mod.call_research = orig_call
        mod.now_et = orig_now

    assert result["ok"] is True
    assert [c["operation"] for c in calls] == [
        "list_events",
        "insert_event",
        "list_events",
        "delete_event",
    ]
    assert calls[3]["event_id"] == "evt-stale-20"
    saved = json.loads((ws / "state" / "richard-earnings" / "watchlist.json").read_text(encoding="utf-8"))
    assert saved["confirmed"][0]["calendar_event_id"] == "evt-new-25"
    assert "tracker_reconcile_needed" not in saved
    # Restore default purge handler registration sanity check
    register_tracker_purge_handler(tc.handle_tracker_calendar_purge)
