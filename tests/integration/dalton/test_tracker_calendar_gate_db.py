"""Real PostgreSQL integration tests for tracker calendar gate execution authorization (Step 4).

Verifies ``POST /api/v1/dalton/mcp/tracker-calendar`` with the REAL
``authorize_tracker_calendar_execution`` against real ``automations`` and
``automation_executions`` rows in PostgreSQL (``_test`` database only).
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import json
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest
import pytest_asyncio

from src.dalton.app import tracker_calendar_router as tc_router
from src.dalton.database import user_integrations as integrations_db
from src.dalton.database.user_integrations import IntegrationRow, IntegrationStatus
from src.dalton.integrations import tracker_calendar as tc
from src.dalton.integrations.constants import (
    GOOGLE_SCOPE_CALENDAR_EVENTS_RO,
    GOOGLE_SCOPE_CALENDAR_LIST_RO,
)
from src.dalton.integrations.google_calendar import GoogleCalendarMCPClient
from src.dalton.sandbox_caller_identity import sign_caller_token
from src.dalton.services.user_integrations_oauth import FreshAccessToken

pytestmark = pytest.mark.integration

BRIDGE_TOKEN = "bridge-admin-secret-s4-int"
CALENDAR_ID = "cal-tracker-int-test@group.calendar.google.com"


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(tc_router.router)
    return app


def _signed_headers(
    *,
    user: str,
    workspace: str,
    thread: str,
    run_id: str,
    automation_id: str,
    execution_id: str,
    run_origin: str = "automation",
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
        admin_token=BRIDGE_TOKEN,
    )
    assert tok is not None
    return {
        "X-Dalton-Bridge-Token": BRIDGE_TOKEN,
        "X-Dalton-Caller-Scope": "per-request",
        "X-Dalton-Caller-Token": tok,
    }


@pytest_asyncio.fixture
async def seeded_tracker_rows(
    patched_get_db_connection: Any,
    dalton_schema: Any,
) -> dict[str, str]:
    ids = {
        "owner_id": str(uuid4()),
        "workspace_id": str(uuid4()),
        "thread_id": str(uuid4()),
        "run_id": str(uuid4()),
        "tracker_auto_id": str(uuid4()),
        "other_auto_id": str(uuid4()),
        "running_exec_id": str(uuid4()),
        "finished_exec_id": str(uuid4()),
        "other_auto_exec_id": str(uuid4()),
    }
    async with patched_get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO users (user_id, email, name, created_at, updated_at)
                VALUES (%s, %s, 'Tracker Owner', NOW(), NOW())
                """,
                (ids["owner_id"], f"tracker_{ids['owner_id'][:8]}@example.test"),
            )
            await cur.execute(
                """
                INSERT INTO workspaces (workspace_id, user_id, name, status, created_at, updated_at)
                VALUES (%s::uuid, %s, 'tracker-ws', 'stopped', NOW(), NOW())
                """,
                (ids["workspace_id"], ids["owner_id"]),
            )
            await cur.execute(
                """
                INSERT INTO automations (
                    automation_id, user_id, name, trigger_type, instruction,
                    workspace_id, metadata, delivery_config, status
                )
                VALUES
                    (%s::uuid, %s, 'richard-earnings', 'cron', 'scan', %s::uuid, '{}'::jsonb, '{}'::jsonb, 'active'),
                    (%s::uuid, %s, 'other-auto', 'cron', 'other', %s::uuid, '{}'::jsonb, '{}'::jsonb, 'active')
                """,
                (
                    ids["tracker_auto_id"],
                    ids["owner_id"],
                    ids["workspace_id"],
                    ids["other_auto_id"],
                    ids["owner_id"],
                    ids["workspace_id"],
                ),
            )
            await cur.execute(
                """
                INSERT INTO automation_executions (
                    automation_execution_id, automation_id, status,
                    conversation_thread_id, conversation_response_id,
                    scheduled_at, started_at
                )
                VALUES
                    (%s::uuid, %s::uuid, 'running', %s::uuid, %s::uuid, NOW(), NOW()),
                    (%s::uuid, %s::uuid, 'completed', %s::uuid, %s::uuid, NOW(), NOW()),
                    (%s::uuid, %s::uuid, 'running', %s::uuid, %s::uuid, NOW(), NOW())
                """,
                (
                    ids["running_exec_id"],
                    ids["tracker_auto_id"],
                    ids["thread_id"],
                    ids["run_id"],
                    ids["finished_exec_id"],
                    ids["tracker_auto_id"],
                    ids["thread_id"],
                    ids["run_id"],
                    ids["other_auto_exec_id"],
                    ids["other_auto_id"],
                    ids["thread_id"],
                    ids["run_id"],
                ),
            )
    try:
        yield ids
    finally:
        async with patched_get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM automation_executions WHERE automation_execution_id = ANY(%s::uuid[])",
                    (
                        [
                            ids["running_exec_id"],
                            ids["finished_exec_id"],
                            ids["other_auto_exec_id"],
                        ],
                    ),
                )
                await cur.execute(
                    "DELETE FROM automations WHERE automation_id = ANY(%s::uuid[])",
                    ([ids["tracker_auto_id"], ids["other_auto_id"]],),
                )
                await cur.execute(
                    "DELETE FROM workspaces WHERE workspace_id = %s::uuid",
                    (ids["workspace_id"],),
                )
                await cur.execute(
                    "DELETE FROM users WHERE user_id = %s",
                    (ids["owner_id"],),
                )


@pytest.mark.asyncio
async def test_tracker_calendar_gate_real_pg_authorization_rejections_and_success(
    seeded_tracker_rows: dict[str, str],
    patched_get_db_connection: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert tc.get_db_connection is patched_get_db_connection
    ids = seeded_tracker_rows
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", BRIDGE_TOKEN)
    monkeypatch.setenv("DALTON_TRACKER_CALENDAR_OWNER_ID", ids["owner_id"])
    monkeypatch.setenv("DALTON_TRACKER_CALENDAR_AUTOMATION_ID", ids["tracker_auto_id"])
    monkeypatch.setenv("DALTON_GCAL_BACKEND", "official_mcp")
    monkeypatch.setenv("DALTON_GCAL_CALENDAR_ID", CALENDAR_ID)
    monkeypatch.setenv("DALTON_CAP_FUND_AUTOMATIONS", "true")
    monkeypatch.setattr(tc_router, "is_dalton_enabled", lambda: True)
    monkeypatch.setattr(tc_router, "require_capability", lambda *a, **k: None)

    now = datetime.now(UTC)
    ro_row = IntegrationRow(
        user_id=ids["owner_id"],
        provider="google",
        service="calendar",
        oauth_client_id="123456-int.apps.googleusercontent.com",
        provider_account_id="sub-int-owner",
        provider_union_id=None,
        provider_email="owner@example.test",
        granted_scopes=f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS_RO}",
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
        preferences={"tracker_calendar_id": CALENDAR_ID},
        has_access_token=True,
        has_refresh_token=True,
        created_at=now,
        updated_at=now,
        access_token="ya29.int-token",
        refresh_token="1//int-refresh",
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
                access_token="ya29.int-token",
                provider="google",
                service="calendar",
                oauth_client_id=ro_row.oauth_client_id,
                provider_account_id=ro_row.provider_account_id,
                auth_epoch=1,
                token_generation=1,
                granted_scopes=ro_row.granted_scopes,
                expires_at=now + timedelta(hours=1),
            ),
        ),
    )
    mcp_client = create_autospec(GoogleCalendarMCPClient, instance=True)
    mcp_client.call_mcp_tool.return_value = {"events": []}
    monkeypatch.setattr(tc_router, "get_router_tracker_google_client", lambda: mcp_client)

    list_body = {
        "operation": "list_events",
        "arguments": {
            "time_min": "2026-11-14T00:00:00Z",
            "time_max": "2026-11-15T00:00:00Z",
        },
    }
    app = _make_app()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1) Mutated payload with original signature -> 401
        valid_h = _signed_headers(
            user=ids["owner_id"],
            workspace=ids["workspace_id"],
            thread=ids["thread_id"],
            run_id=ids["run_id"],
            automation_id=ids["tracker_auto_id"],
            execution_id=ids["running_exec_id"],
        )
        p_b64, s_b64 = valid_h["X-Dalton-Caller-Token"].split(".")
        raw = json.loads(base64.urlsafe_b64decode(p_b64 + "=" * ((4 - len(p_b64) % 4) % 4)))
        raw["run_id"] = str(uuid4())
        tampered_b64 = (
            base64.urlsafe_b64encode(
                json.dumps(raw, separators=(",", ":"), sort_keys=True).encode("utf-8")
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        r_sig = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json=list_body,
            headers={**valid_h, "X-Dalton-Caller-Token": f"{tampered_b64}.{s_b64}"},
        )
        assert r_sig.status_code == 401

        # 2) Valid signature, mismatched run_id against real PG execution row -> 403 run_mismatch
        r_run = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json=list_body,
            headers=_signed_headers(
                user=ids["owner_id"],
                workspace=ids["workspace_id"],
                thread=ids["thread_id"],
                run_id=str(uuid4()),
                automation_id=ids["tracker_auto_id"],
                execution_id=ids["running_exec_id"],
            ),
        )
        assert r_run.status_code == 403
        assert r_run.json()["detail"]["code"] == "run_mismatch"

        # 3) Different automation -> 403 automation_mismatch
        r_auto = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json=list_body,
            headers=_signed_headers(
                user=ids["owner_id"],
                workspace=ids["workspace_id"],
                thread=ids["thread_id"],
                run_id=ids["run_id"],
                automation_id=ids["other_auto_id"],
                execution_id=ids["other_auto_exec_id"],
            ),
        )
        assert r_auto.status_code == 403
        assert r_auto.json()["detail"]["code"] == "automation_mismatch"

        # 4) Finished execution -> 403 execution_not_running
        r_fin = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json=list_body,
            headers=_signed_headers(
                user=ids["owner_id"],
                workspace=ids["workspace_id"],
                thread=ids["thread_id"],
                run_id=ids["run_id"],
                automation_id=ids["tracker_auto_id"],
                execution_id=ids["finished_exec_id"],
            ),
        )
        assert r_fin.status_code == 403
        assert r_fin.json()["detail"]["code"] == "execution_not_running"

        # 5) Matching running execution -> 200 OK
        r_ok = await client.post(
            "/api/v1/dalton/mcp/tracker-calendar",
            json=list_body,
            headers=valid_h,
        )
        assert r_ok.status_code == 200
        assert r_ok.json()["payload"]["ok"] is True
