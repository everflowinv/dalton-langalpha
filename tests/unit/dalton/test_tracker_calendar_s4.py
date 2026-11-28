"""Step 4 unit tests: tracker calendar gate, caller token signing, bridge forwarding, probe guards, and purge handler.

All test doubles use ``create_autospec``.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace as dc_replace
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

from ptc_agent.agent.backends.sandbox import SandboxBackend
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
from src.dalton.services import integration_data_purge as purge_mod
from src.dalton.services import user_integrations_lifecycle as lifecycle_mod
from src.dalton.services.integration_data_purge import (
    get_tracker_purge_handler,
    register_tracker_purge_handler,
)
from src.dalton.services.user_integrations_oauth import FreshAccessToken
from src.server.database import session_lock

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
    import base64

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

    # IMPORTANT: Do NOT mock authorize_tracker_calendar_execution — run the real function!
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
        # 1) Forged caller token -> 401
        r_forged = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={"operation": "health_check", "arguments": {}},
            headers=_signed_headers(admin_token_for_sig="forged-secret"),
        )
        assert r_forged.status_code == 401

        # 2) Expired caller token -> 401
        r_expired = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json={"operation": "health_check", "arguments": {}},
            headers=_signed_headers(exp=100),
        )
        assert r_expired.status_code == 401

        # 3) Payload mutated while keeping original signature -> 401
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
        tampered_sig_headers = {
            **valid_headers,
            "X-Dalton-Caller-Token": f"{mutated_b64}.{sig_b64}",
        }
        r_tampered_sig = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json=list_body,
            headers=tampered_sig_headers,
        )
        assert r_tampered_sig.status_code == 401
        assert r_tampered_sig.json()["detail"]["code"] == "invalid_caller_token"

        # 4) Valid signature, mismatched run_id vs execution row (real authz) -> 403 run_mismatch
        r_run_mismatch = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json=list_body,
            headers=_signed_headers(run_id="99999999-9999-9999-9999-999999999999"),
        )
        assert r_run_mismatch.status_code == 403
        assert r_run_mismatch.json()["detail"]["code"] == "run_mismatch"

        # 5) Valid signature, different automation_id -> 403 automation_mismatch
        r_other_auto = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json=list_body,
            headers=_signed_headers(automation_id=other_auto_id, execution_id=other_exec_id),
        )
        assert r_other_auto.status_code == 403
        assert r_other_auto.json()["detail"]["code"] == "automation_mismatch"

        # 6) Valid signature, finished (completed) execution -> 403 execution_not_running
        r_finished = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json=list_body,
            headers=_signed_headers(execution_id=finished_exec_id),
        )
        assert r_finished.status_code == 403
        assert r_finished.json()["detail"]["code"] == "execution_not_running"

        # 7) Unsupported operation returns HTTP 400
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
    monkeypatch.setattr(
        integrations_db,
        "has_pending_tracker_reconcile",
        create_autospec(integrations_db.has_pending_tracker_reconcile, return_value=False),
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
    monkeypatch.setattr(
        integrations_db,
        "has_pending_tracker_reconcile",
        create_autospec(integrations_db.has_pending_tracker_reconcile, return_value=False),
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
    monkeypatch.setenv("DALTON_GCAL_BACKEND", "official_mcp")
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


def test_probe_drill_rejects_non_test_database_name(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _load_probe_module()
    monkeypatch.setenv("DB_NAME", "langalpha")
    monkeypatch.setenv("DALTON_TEST_GCAL_CALENDAR_IDS", "test-subcal@group.calendar.google.com")
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


def test_probe_drill_rejects_production_owner_id(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _load_probe_module()
    monkeypatch.setenv("DB_NAME", "langalpha_test")
    monkeypatch.setenv("DALTON_TEST_GCAL_CALENDAR_IDS", "test-subcal@group.calendar.google.com")
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


def test_probe_drill_rejects_non_test_calendar_target(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _load_probe_module()
    monkeypatch.setenv("DB_NAME", "langalpha_test")
    monkeypatch.setenv("DALTON_TEST_GCAL_CALENDAR_IDS", "cal-drill-registered@group.calendar.google.com")
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

    # Even a calendar ID containing "test"/"drill" is rejected if NOT in registered list
    code_unregistered, payload_unregistered = asyncio.run(
        probe.run_calendar_purge_drill(
            database_url="postgresql://user:pass@localhost:5432/langalpha_test",
            owner_id=TEST_OWNER_ID,
            calendar_id="fake-test-drill-unregistered@group.calendar.google.com",
            dry_run=True,
        )
    )
    assert code_unregistered != 0
    assert payload_unregistered["guard_failed"] is True


def test_probe_drill_accepts_valid_non_prod_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _load_probe_module()
    monkeypatch.setenv("DB_NAME", "langalpha_test")
    monkeypatch.setenv("DALTON_TEST_GCAL_CALENDAR_IDS", "cal-drill-subcal@group.calendar.google.com")
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
        integrations_db,
        "has_pending_tracker_reconcile",
        create_autospec(integrations_db.has_pending_tracker_reconcile, return_value=False),
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

    async def _host_acct() -> str:
        return "owner@example.test"

    code, payload = await probe.run_prod_readonly_check(
        owner_id=OWNER_ID,
        expected_calendar_id=CALENDAR_ID,
        gws_account_resolver=create_autospec(_host_acct, side_effect=_host_acct),
        google_client=mcp_client,
        now_et=datetime(2026, 11, 14, 9, 0, tzinfo=ZoneInfo("America/New_York")),
    )
    assert code == 0
    assert payload["ok"] is True
    assert payload["account_matched"] is True
    assert payload["calendar_id_matched"] is True
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


# ---------------------------------------------------------------------------
# 7. Round 2 regression tests (Items 1-9)
# ---------------------------------------------------------------------------


def _build_local_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", BRIDGE_TOKEN)
    monkeypatch.setenv("DALTON_BACKEND_INTERNAL_URL", "http://backend:8000")
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
    assert hc_body["data"] == {"calendar_ok": True}
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

    # On bridge: official_mcp with empty raw_caller_token refuses locally without HTTP
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


def test_probe_drill_parser_forbids_prod_owner_override() -> None:
    probe = _load_probe_module()
    parser = probe.build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(
            [
                "calendar-purge-drill",
                "--database-url",
                "postgresql://u:p@localhost:5432/langalpha_test",
                "--prod-owner-id",
                "00000000-0000-0000-0000-000000000000",
            ]
        )
    assert exc_info.value.code != 0


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
async def test_probe_drill_non_dry_run_guard_failure_never_executes_purge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _load_probe_module()
    monkeypatch.setenv("DB_NAME", "langalpha_test")
    monkeypatch.setenv(
        "DALTON_TEST_GCAL_CALENDAR_IDS",
        "registered-test-cal@group.calendar.google.com",
    )

    @asynccontextmanager
    async def _noop_pool() -> Any:
        yield None

    monkeypatch.setattr(probe, "open_probe_db_pool", _noop_pool)

    begin_mock = create_autospec(lifecycle_mod.begin_disconnect)
    finish_mock = create_autospec(lifecycle_mod.finish_disconnect)
    purge_mock = create_autospec(purge_mod.run_local_purge)
    monkeypatch.setattr(lifecycle_mod, "begin_disconnect", begin_mock)
    monkeypatch.setattr(lifecycle_mod, "finish_disconnect", finish_mock)
    monkeypatch.setattr(purge_mod, "run_local_purge", purge_mock)

    # Case 1: Pool actual current_database() returns prod DB name (mismatches --database-url)
    orig_query_db = probe._query_actual_database_name
    orig_get_conn = integrations_db.get_connection
    prod_db_query = create_autospec(orig_query_db, return_value="langalpha")
    monkeypatch.setattr(probe, "_query_actual_database_name", prod_db_query)
    test_conn = _make_integration_row(
        user_id=TEST_OWNER_ID,
        calendar_id="registered-test-cal@group.calendar.google.com",
    )
    monkeypatch.setattr(
        integrations_db,
        "get_connection",
        create_autospec(orig_get_conn, return_value=test_conn),
    )
    code1, body1 = await probe.run_calendar_purge_drill(
        database_url="postgresql://user:pass@localhost:5432/langalpha_test",
        owner_id=TEST_OWNER_ID,
        calendar_id="registered-test-cal@group.calendar.google.com",
        dry_run=False,
    )
    assert code1 != 0
    assert body1["guard_failed"] is True
    assert begin_mock.await_count == 0
    assert finish_mock.await_count == 0
    assert purge_mock.await_count == 0

    # Case 2: Actual DB is _test, CLI --calendar-id is registered, BUT connection row's
    # preferences.tracker_calendar_id resolves to a different (unregistered) calendar!
    test_db_query = create_autospec(orig_query_db, return_value="langalpha_test")
    monkeypatch.setattr(probe, "_query_actual_database_name", test_db_query)
    hijacked_conn = _make_integration_row(
        user_id=TEST_OWNER_ID,
        calendar_id="prod-real-calendar-with-test-in-name@group.calendar.google.com",
    )
    monkeypatch.setattr(
        integrations_db,
        "get_connection",
        create_autospec(orig_get_conn, return_value=hijacked_conn),
    )
    code2, body2 = await probe.run_calendar_purge_drill(
        database_url="postgresql://user:pass@localhost:5432/langalpha_test",
        owner_id=TEST_OWNER_ID,
        calendar_id="registered-test-cal@group.calendar.google.com",
        dry_run=False,
    )
    assert code2 != 0
    assert body2["guard_failed"] is True
    assert begin_mock.await_count == 0
    assert finish_mock.await_count == 0
    assert purge_mock.await_count == 0


@pytest.mark.asyncio
async def test_finish_disconnect_real_entry_captures_snapshot_before_row_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test real ``finish_disconnect_locked`` order: local_only / sibling disconnect deletes
    the connection row before ``run_local_purge``, yet the pre-delete snapshot still lets the
    tracker purge handler delete remote events on the old epoch's ``tracker_calendar_id``.
    """
    monkeypatch.setenv("DALTON_TRACKER_CALENDAR_OWNER_ID", OWNER_ID)
    monkeypatch.setenv("DALTON_TRACKER_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("DALTON_GCAL_CALENDAR_ID", raising=False)

    pref_cal_id = "pref-only-tracker-cal@group.calendar.google.com"
    wl_path, _apply_path, _roll_path = _seed_tracker_workspace(tmp_path)

    # Connection stored in-memory; deleted by delete_connection_upto_epoch BEFORE run_local_purge
    store: dict[str, IntegrationRow | None] = {
        "conn": _make_integration_row(
            user_id=OWNER_ID,
            scopes=f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS}",
            calendar_id=pref_cal_id,
            access_token="ya29.pre-delete-write-token",
        )
    }
    ledger = dc_replace(
        _make_purge_ledger(
            user_id=OWNER_ID,
            revoke_token=None,  # local_only or active sibling -> NO revoke_token in ledger!
        ),
        remote_revoke_status=RemoteRevokeStatus.NOT_APPLICABLE,
        has_revoke_token=False,
        revoke_token=None,
    )
    final_reconcile_written: list[bool] = []

    async def _fake_get_connection(
        user_id: str, provider: str, service: str, *, decrypt_tokens: bool = False, conn: Any = None
    ) -> IntegrationRow | None:
        return store["conn"]

    async def _fake_delete_upto_epoch(
        user_id: str, provider: str, service: str | None, auth_epoch_upto: int, *, conn: Any = None
    ) -> int:
        assert store["conn"] is not None
        store["conn"] = None
        return 1

    async def _fake_get_purge_ledger(
        purge_id: str, *, decrypt_revoke_token: bool = False, conn: Any = None
    ) -> PurgeLedgerRow:
        if final_reconcile_written:
            return dc_replace(
                ledger,
                local_purge_status=LocalPurgeStatus.COMPLETED,
                tracker_reconcile_needed=final_reconcile_written[-1],
            )
        return ledger

    mcp_client = create_autospec(GoogleCalendarMCPClient, instance=True)
    seen_list_cals: list[str] = []
    deleted_ids: list[str] = []

    async def _fake_mcp(
        *,
        access_token: str,
        tool_name: str,
        arguments: dict[str, Any],
        request_state: str | None = None,
        input_responses: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Connection row MUST already be deleted by the time remote MCP runs!
        assert store["conn"] is None
        assert access_token == "ya29.pre-delete-write-token"
        if tool_name == "list_events":
            seen_list_cals.append(arguments["calendarId"])
            return {
                "events": [
                    {
                        "id": "evt-predelete-1",
                        "summary": "Affirm (AFRM) Earnings — Q1 2027 [BMO]",
                        "description": "Created by Dalton earnings tracker",
                        "start": {"date": "2026-11-20"},
                        "end": {"date": "2026-11-21"},
                    }
                ]
            }
        if tool_name == "delete_event":
            deleted_ids.append(arguments["eventId"])
            return {"deleted": True}
        raise AssertionError(tool_name)

    mcp_client.call_mcp_tool.side_effect = _fake_mcp
    monkeypatch.setattr(tc, "get_google_calendar_client", lambda: mcp_client)
    register_tracker_purge_handler(tc.handle_tracker_calendar_purge)

    class _FakePurgeCursor:
        def __init__(self) -> None:
            self._rows: list[Any] = []
            self._one: Any = None

        async def __aenter__(self) -> _FakePurgeCursor:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            norm = " ".join(sql.split()).upper()
            if "FROM DALTON_THREAD_INTEGRATION_TOUCHES" in norm:
                self._rows = []
            elif "SELECT LOCAL_PURGE_STATUS, LOCAL_PURGE_ATTEMPT_GEN" in norm:
                self._one = {"local_purge_status": "running", "local_purge_attempt_gen": 1}
            elif "UPDATE DALTON_INTEGRATION_PURGE_LEDGER" in norm and isinstance(params, dict):
                final_reconcile_written.append(bool(params.get("tracker_reconcile")))

        async def fetchall(self) -> list[Any]:
            return self._rows

        async def fetchone(self) -> Any:
            return self._one

    class _FakePurgeConn:
        @asynccontextmanager
        async def transaction(self) -> Any:
            yield self

        def cursor(self, **_kwargs: Any) -> _FakePurgeCursor:
            return _FakePurgeCursor()

    @asynccontextmanager
    async def _fake_get_db_conn(_conn: Any = None) -> Any:
        yield _FakePurgeConn()

    @asynccontextmanager
    async def _lock_ok(_key: Any) -> Any:
        yield True

    monkeypatch.setattr(session_lock, "try_own_session_lock", _lock_ok)
    monkeypatch.setattr(
        lifecycle_mod,
        "_drain_refresh_locks",
        create_autospec(lifecycle_mod._drain_refresh_locks, return_value=None),
    )
    monkeypatch.setattr(
        integrations_db,
        "get_connection",
        create_autospec(integrations_db.get_connection, side_effect=_fake_get_connection),
    )
    monkeypatch.setattr(
        integrations_db,
        "delete_connection_upto_epoch",
        create_autospec(
            integrations_db.delete_connection_upto_epoch, side_effect=_fake_delete_upto_epoch
        ),
    )
    monkeypatch.setattr(
        integrations_db,
        "get_purge_ledger",
        create_autospec(integrations_db.get_purge_ledger, side_effect=_fake_get_purge_ledger),
    )
    monkeypatch.setattr(
        integrations_db,
        "claim_local_purge_attempt",
        create_autospec(integrations_db.claim_local_purge_attempt, return_value=1),
    )
    monkeypatch.setattr(
        integrations_db,
        "delete_approvals_for_disconnect",
        create_autospec(integrations_db.delete_approvals_for_disconnect, return_value=0),
    )
    monkeypatch.setattr(purge_mod, "get_db_connection", _fake_get_db_conn)
    monkeypatch.setattr(
        purge_mod,
        "get_checkpointer",
        create_autospec(purge_mod.get_checkpointer, return_value=None),
    )

    res = await lifecycle_mod.finish_disconnect(
        ledger.purge_id,
        revoker=None,
        run_remote_revoke=False,
    )
    assert res is not None
    assert res.deleted_connections == 1
    assert res.local_purge_status == LocalPurgeStatus.COMPLETED
    assert seen_list_cals == [pref_cal_id]
    assert deleted_ids == ["evt-predelete-1"]
    assert final_reconcile_written == [False]
    wl = json.loads(wl_path.read_text(encoding="utf-8"))
    assert wl["confirmed"][0]["calendar_event_id"] is None
    assert "tracker_reconcile_needed" not in wl


@pytest.mark.asyncio
async def test_tracker_purge_locates_sandbox_workspace_via_automation_and_clears_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_TRACKER_CALENDAR_OWNER_ID", OWNER_ID)
    monkeypatch.setenv("DALTON_TRACKER_CALENDAR_AUTOMATION_ID", AUTO_ID)
    monkeypatch.delenv("DALTON_TRACKER_WORKSPACE_DIR", raising=False)

    sandbox_files: dict[str, str] = {
        "state/richard-earnings/watchlist.json": json.dumps(
            {
                "updated": "2026-11-14",
                "confirmed": [
                    {
                        "ticker": "AFRM",
                        "name": "Affirm",
                        "date": "2026-11-20",
                        "timing": "BMO",
                        "calendar_event_id": "evt-sbx-1",
                    }
                ],
                "pending": [],
                "reported": [],
                "pending_delete_event_ids": ["evt-sbx-del"],
            }
        ),
        "state/richard-earnings/scans/2026-11-14/apply.json": json.dumps(
            {"planned": [{"ticker": "AFRM", "old_event_id": "evt-sbx-old"}], "applied": []}
        ),
        "state/richard-earnings/scans/2026-11-14/roll.json": json.dumps(
            {"moved": [{"ticker": "PDD", "calendar_event_id": "evt-sbx-roll"}]}
        ),
    }

    sbx_backend = create_autospec(SandboxBackend, instance=True)

    async def _dl(path: str) -> bytes | None:
        val = sandbox_files.get(path)
        return val.encode("utf-8") if val is not None else None

    async def _wt(path: str, content: str) -> bool:
        sandbox_files[path] = content
        return True

    async def _glob(pattern: str, path: str = ".") -> list[str]:
        if "apply.json" in pattern:
            return ["state/richard-earnings/scans/2026-11-14/apply.json"]
        if "roll.json" in pattern:
            return ["state/richard-earnings/scans/2026-11-14/roll.json"]
        return []

    sbx_backend.adownload_file_bytes.side_effect = _dl
    sbx_backend.awrite_text.side_effect = _wt
    sbx_backend.aglob_paths.side_effect = _glob

    monkeypatch.setattr(
        tc,
        "_lookup_tracker_automation_workspace",
        create_autospec(
            tc._lookup_tracker_automation_workspace, return_value=(WS_ID, OWNER_ID)
        ),
    )
    monkeypatch.setattr(
        tc,
        "_resolve_tracker_workspace_backend",
        create_autospec(
            tc._resolve_tracker_workspace_backend, return_value=sbx_backend
        ),
    )

    write_row = _make_integration_row(
        scopes=f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS}"
    )
    monkeypatch.setattr(
        integrations_db,
        "get_connection",
        create_autospec(integrations_db.get_connection, return_value=write_row),
    )
    mcp_client = create_autospec(GoogleCalendarMCPClient, instance=True)
    mcp_client.call_mcp_tool.return_value = {"events": []}

    handled = await tc.handle_tracker_calendar_purge(
        _make_purge_ledger(),
        google_client=mcp_client,
    )
    assert handled is True
    wl = json.loads(sandbox_files["state/richard-earnings/watchlist.json"])
    assert wl["confirmed"][0]["calendar_event_id"] is None
    assert wl["pending_delete_event_ids"] == []
    assert "tracker_reconcile_needed" not in wl
    apply_doc = json.loads(sandbox_files["state/richard-earnings/scans/2026-11-14/apply.json"])
    assert apply_doc["planned"][0]["old_event_id"] is None
    roll_doc = json.loads(sandbox_files["state/richard-earnings/scans/2026-11-14/roll.json"])
    assert roll_doc["moved"][0]["calendar_event_id"] is None


@pytest.mark.asyncio
async def test_unreachable_workspace_keeps_pg_reconcile_flag_and_weekly_scan_consumes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_TRACKER_CALENDAR_OWNER_ID", OWNER_ID)
    monkeypatch.setenv("DALTON_TRACKER_CALENDAR_AUTOMATION_ID", AUTO_ID)
    monkeypatch.delenv("DALTON_TRACKER_WORKSPACE_DIR", raising=False)

    # 1. Disconnect when automation workspace cannot be located -> handle_tracker_calendar_purge returns False
    monkeypatch.setattr(
        tc,
        "_lookup_tracker_automation_workspace",
        create_autospec(tc._lookup_tracker_automation_workspace, return_value=(None, None)),
    )
    write_row = _make_integration_row(
        scopes=f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS}"
    )
    monkeypatch.setattr(
        integrations_db,
        "get_connection",
        create_autospec(integrations_db.get_connection, return_value=write_row),
    )
    mcp_client = create_autospec(GoogleCalendarMCPClient, instance=True)
    mcp_client.call_mcp_tool.return_value = {"events": []}

    handled = await tc.handle_tracker_calendar_purge(
        _make_purge_ledger(),
        google_client=mcp_client,
    )
    # Even though remote had 0 events, local workspace was NOT found -> keep tracker_reconcile_needed=TRUE!
    assert handled is False

    # 2. Reconnect: official_mcp health_check surfaces persistent PG tracker_reconcile_needed=True,
    #    and ack_reconcile=True clears it in PG.
    pg_flag = {"pending": True}

    async def _fake_has_pending(_uid: str, *, conn: Any = None) -> bool:
        return pg_flag["pending"]

    async def _fake_clear_pending(_uid: str, *, conn: Any = None) -> int:
        pg_flag["pending"] = False
        return 1

    monkeypatch.setattr(
        integrations_db,
        "has_pending_tracker_reconcile",
        create_autospec(integrations_db.has_pending_tracker_reconcile, side_effect=_fake_has_pending),
    )
    monkeypatch.setattr(
        integrations_db,
        "clear_pending_tracker_reconcile",
        create_autospec(
            integrations_db.clear_pending_tracker_reconcile, side_effect=_fake_clear_pending
        ),
    )
    monkeypatch.setattr(
        tc,
        "ensure_fresh_access_token",
        create_autospec(
            tc.ensure_fresh_access_token,
            return_value=FreshAccessToken(
                access_token="ya29.fresh-token",
                provider="google",
                service="calendar",
                oauth_client_id=write_row.oauth_client_id,
                provider_account_id="sub-owner-1",
                auth_epoch=2,
                token_generation=1,
                granted_scopes=f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS}",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ),
        ),
    )

    hc_payload, _ = await tc.execute_tracker_calendar_official_mcp(
        owner_id=OWNER_ID,
        operation="health_check",
        arguments={},
        google_client=mcp_client,
    )
    assert hc_payload["ok"] is True
    assert hc_payload["data"]["tracker_reconcile_needed"] is True

    # 3. Weekly scan self-test consumes backend marker -> persists locally & clears stale event IDs;
    #    then cmd_apply reconciles & sends health_check(ack_reconcile=True) to clear PG flag.
    mod = _load_richard_earnings_module()
    ws = tmp_path / "ws"
    (ws / "state" / "richard-earnings" / "scans" / "2026-11-14").mkdir(parents=True)
    (ws / "state" / "richard-earnings" / "watchlist.json").write_text(
        json.dumps(
            {
                "updated": "2026-11-10",
                "confirmed": [
                    {
                        "ticker": "AFRM",
                        "name": "Affirm",
                        "date": "2026-11-20",
                        "timing": "BMO",
                        "quarter": "Q1 2027",
                        "calendar_event_id": "stale-old-id",
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
                "earnings_date": "2026-11-20",
                "timing": "BMO",
                "quarter_label": "Q1 2027",
                "confidence": "high",
                "source_url": "https://ir.example.test",
                "evidence": "press release",
                "notes": None,
                "group": "A",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    calls: list[dict[str, Any]] = []

    def _fake_research(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        op = kwargs.get("operation")
        if op == "health_check":
            if kwargs.get("ack_reconcile"):
                pg_flag["pending"] = False
                return {"ok": True, "status": "ok", "data": {"calendar_ok": True, "tracker_reconcile_cleared": True}}
            return hc_payload
        if op == "list_events":
            return {"ok": True, "status": "ok", "data": {"events": []}}
        if op == "insert_event":
            return {"ok": True, "status": "ok", "data": {"id": "evt-reconciled-1"}}
        raise AssertionError(f"unexpected {op}")

    import argparse

    orig_call = mod.call_research
    orig_now = mod.now_et
    try:
        mod.now_et = create_autospec(
            orig_now,
            return_value=datetime(2026, 11, 14, 7, 0, tzinfo=ZoneInfo("America/New_York")),
        )
        mod.call_research = create_autospec(orig_call, side_effect=_fake_research)
        ns = argparse.Namespace(workspace_dir=str(ws))
        st = mod.cmd_self_test(ns)
        assert st["tracker_reconcile_needed"] is True
        wl_after_st = json.loads((ws / "state" / "richard-earnings" / "watchlist.json").read_text(encoding="utf-8"))
        assert wl_after_st["tracker_reconcile_needed"] is True
        assert wl_after_st["tracker_reconcile_source"] == "backend"
        assert wl_after_st["confirmed"][0]["calendar_event_id"] is None

        res_apply = mod.cmd_apply(ns)
        assert res_apply["ok"] is True
    finally:
        mod.call_research = orig_call
        mod.now_et = orig_now

    assert pg_flag["pending"] is False
    assert any(c.get("operation") == "health_check" and c.get("ack_reconcile") is True for c in calls)
    wl_final = json.loads((ws / "state" / "richard-earnings" / "watchlist.json").read_text(encoding="utf-8"))
    assert wl_final["confirmed"][0]["calendar_event_id"] == "evt-reconciled-1"
    assert "tracker_reconcile_needed" not in wl_final


def test_richard_earnings_preserves_old_slot_on_query_failure_and_keeps_flag_on_partial_scan(
    tmp_path: Path,
) -> None:
    import argparse

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
                    },
                    {
                        "ticker": "NVDA",
                        "name": "NVIDIA",
                        "date": "2026-11-21",
                        "timing": "AMC",
                        "quarter": "Q3 FY2027",
                        "calendar_event_id": "evt-nvda-1",
                        "source": "https://ir.nvidia.test",
                        "confidence": "high",
                    },
                ],
                "pending": [],
                "reported": [],
            }
        ),
        encoding="utf-8",
    )
    # Only scan AFRM (NVDA is NOT scanned yet -> partial universe scan)
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

    # Run 1: old-date list_events fails with timeout -> slot MUST be preserved in pending_reconcile_slots
    run1_responses = [
        {"ok": True, "status": "ok", "data": {"events": []}},  # new date list
        {"ok": True, "status": "ok", "data": {"id": "evt-afrm-new"}},  # insert new date
        {"ok": False, "status": "timeout", "error": "timeout"},  # old date list FAILS!
    ]
    orig_call = mod.call_research
    orig_now = mod.now_et
    try:
        mod.now_et = create_autospec(
            orig_now,
            return_value=datetime(2026, 11, 14, 7, 0, tzinfo=ZoneInfo("America/New_York")),
        )
        mod.call_research = create_autospec(orig_call, side_effect=lambda **kw: run1_responses.pop(0))
        ns = argparse.Namespace(workspace_dir=str(ws))
        r1 = mod.cmd_apply(ns)
        assert r1["ok"] is False

        wl1 = json.loads((ws / "state" / "richard-earnings" / "watchlist.json").read_text(encoding="utf-8"))
        assert wl1["tracker_reconcile_needed"] is True
        assert wl1["pending_reconcile_slots"] == [
            {
                "ticker": "AFRM",
                "date": "2026-11-20",
                "timing": "BMO",
                "keep_event_id": "evt-afrm-new",
            }
        ]

        # Run 2: retry succeeds on preserved old date/timing, BUT NVDA still unscanned ->
        # pending_reconcile_slots is cleared while tracker_reconcile_needed STAYS True!
        run2_calls: list[dict[str, Any]] = []
        run2_responses = [
            {
                "ok": True,
                "status": "ok",
                "data": {
                    "events": [
                        {
                            "id": "evt-afrm-stale-20",
                            "summary": "Affirm (AFRM) Earnings — Q1 2027 [BMO]",
                            "description": "Created by Dalton earnings tracker",
                            "start": {"dateTime": "2026-11-20T08:00:00-05:00", "timeZone": "America/New_York"},
                            "end": {"dateTime": "2026-11-20T08:30:00-05:00", "timeZone": "America/New_York"},
                        }
                    ]
                },
            },
            {"ok": True, "status": "ok", "data": {"deleted": True, "id": "evt-afrm-stale-20"}},
        ]

        def _run2_research(**kw: Any) -> dict[str, Any]:
            run2_calls.append(dict(kw))
            return run2_responses.pop(0)

        mod.call_research = create_autospec(orig_call, side_effect=_run2_research)
        r2 = mod.cmd_apply(ns)
        assert r2["ok"] is True
        assert [c["operation"] for c in run2_calls] == ["list_events", "delete_event"]
        assert run2_calls[1]["event_id"] == "evt-afrm-stale-20"
        wl2 = json.loads((ws / "state" / "richard-earnings" / "watchlist.json").read_text(encoding="utf-8"))
        assert "pending_reconcile_slots" not in wl2
        # Partial universe scan (NVDA missing from recs) -> MUST keep tracker_reconcile_needed!
        assert wl2["tracker_reconcile_needed"] is True
    finally:
        mod.call_research = orig_call
        mod.now_et = orig_now


@pytest.mark.asyncio
async def test_probe_drain_mail_checks_prepare_and_execution_locks_and_fails_on_unreadable(
    tmp_path: Path,
) -> None:
    probe = _load_probe_module()
    state_root = tmp_path / "md-state"
    locks_dir = state_root / "locks"
    locks_dir.mkdir(parents=True)

    prepare_cfg = tmp_path / "market-digest-prepare.json"
    prepare_cfg.write_text(json.dumps({"state_root": str(state_root)}), encoding="utf-8")

    async def _zero(_ids: list[str]) -> int:
        return 0

    zero_mock = create_autospec(_zero, side_effect=_zero)

    # 1) Held locks/prepare_AM.lock -> exit 2 with lock reported
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

    # 2) Held locks/execution_<uuid>.lock -> exit 2 with lock reported
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

    # 3) No locks held -> exit 0 drained
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

    # 4) Unreadable lock file (chmod 0o000 or symlink) -> exit 1 failure (NOT idle!)
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

    # 1) Host gws account mismatch -> exits 1 immediately with ZERO MCP calls
    async def _wrong_acct() -> str:
        return "someone-else@example.test"

    code_acct, body_acct = await probe.run_prod_readonly_check(
        owner_id=OWNER_ID,
        expected_calendar_id=CALENDAR_ID,
        gws_account_resolver=create_autospec(_wrong_acct, side_effect=_wrong_acct),
        google_client=mcp_client,
    )
    assert code_acct != 0
    assert body_acct["ok"] is False
    assert body_acct["account_matched"] is False
    assert mcp_client.call_mcp_tool.call_count == 0

    # 2) Target sub-calendar mismatch -> exits 1 immediately with ZERO MCP calls
    async def _right_acct() -> str:
        return "owner@example.test"

    code_cal, body_cal = await probe.run_prod_readonly_check(
        owner_id=OWNER_ID,
        expected_calendar_id="different-calendar@group.calendar.google.com",
        gws_account_resolver=create_autospec(_right_acct, side_effect=_right_acct),
        google_client=mcp_client,
    )
    assert code_cal != 0
    assert body_cal["ok"] is False
    assert body_cal["account_matched"] is True
    assert body_cal["calendar_id_matched"] is False
    assert mcp_client.call_mcp_tool.call_count == 0
