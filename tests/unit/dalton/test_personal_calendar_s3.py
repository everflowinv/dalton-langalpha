"""Unit tests for Batch 1 Step 3: private surface guard, HITLDecision, calendar clients & dalton_calendar.

All external clients and database helpers use ``create_autospec`` doubles (no network or live PG).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, create_autospec
from uuid import uuid4

from fastapi import HTTPException
import pytest

from src.dalton.capabilities import CapabilityId
from src.dalton.database import user_integrations as db_mod
from src.dalton.database.user_integrations import (
    ActionApprovalRow,
    IntegrationRow,
    IntegrationStatus,
)
from src.dalton.integrations.feishu_calendar import (
    FeishuCalendarClient,
    FeishuCalendarClientProtocol,
)
from src.dalton.integrations.google_calendar import (
    GOOGLE_CALENDAR_MCP_ENDPOINT,
    GoogleCalendarClientProtocol,
    GoogleCalendarMCPClient,
    GoogleMCPElicitationRequired,
    build_google_list_events_args,
    build_mcp_tools_call_payload,
    compute_free_busy_slots,
    format_local_datetime_for_tz,
    parse_google_mcp_response,
)
from src.dalton.integrations.lock_guard import (
    AdvisoryLockHeldDuringExternalCallError,
    advisory_lock_http_guard,
    track_advisory_lock,
)
from src.dalton.services import personal_integration_approval as approval_mod
from src.dalton.services import personal_integration_guard as guard_mod
from src.dalton.services.personal_integration_approval import (
    DALTON_INTEGRATION_APPROVAL_TYPES,
    canonical_payload_sha256,
    validate_elicitation_responses,
    verify_personal_integration_interrupt_resume,
)
from src.dalton.services.personal_integration_guard import (
    FIRST_TOUCH_NOTICE_CALENDAR,
    PERSONAL_INTEGRATION_NOT_SHARABLE_CODE,
    PersonalIntegrationNotSharableError,
    PersonalSurfaceDeniedError,
    PersonalSurfaceVerificationResult,
    assert_thread_sharable_tx,
    enforce_personal_thread_owner_only,
    verify_personal_integration_surface,
)
from src.dalton.services.user_integrations_oauth import FreshAccessToken
from src.dalton.tools import build_dalton_tools
from src.dalton.tools import calendar_tools as cal_tools_mod
from src.dalton.tools.calendar_tools import create_calendar_tools
from src.server.handlers import checkpoint_handler as cp_handler
from src.server.models.chat import (
    HITLDecision,
    HITLResponse,
    serialize_hitl_response_map,
)
from src.tools.secretary._commands import hitl_confirm as real_hitl_confirm


class _ScriptedCursor:
    """Minimal cursor double returning scripted rows in order."""

    def __init__(self, rows: list[dict[str, Any] | None]) -> None:
        self._rows = list(rows)
        self.executed: list[tuple[str, Any]] = []
        self.rowcount = 1

    async def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    async def fetchone(self) -> dict[str, Any] | None:
        return self._rows.pop(0) if self._rows else None

    async def fetchall(self) -> list[dict[str, Any]]:
        return []

    async def __aenter__(self) -> _ScriptedCursor:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _ScriptedConn:
    def __init__(self, rows: list[dict[str, Any] | None]) -> None:
        self.cur = _ScriptedCursor(rows)

    def cursor(self, row_factory: Any = None) -> _ScriptedCursor:
        _ = row_factory
        return self.cur

    @asynccontextmanager
    async def transaction(self):
        yield self


def _sample_conn(
    *,
    user_id: str = "user-1",
    provider: str = "google",
    service: str = "calendar",
    status: IntegrationStatus = IntegrationStatus.CONNECTED,
    email: str = "alice@example.com",
    auth_epoch: int = 7,
) -> IntegrationRow:
    now = datetime.now(UTC)
    return IntegrationRow(
        user_id=user_id,
        provider=provider,
        service=service,
        oauth_client_id="12345-client.apps.googleusercontent.com",
        provider_account_id=f"acct-{provider}-1",
        provider_union_id=None,
        provider_email=email,
        granted_scopes=(
            "openid email https://www.googleapis.com/auth/calendar.calendarlist.readonly "
            "https://www.googleapis.com/auth/calendar.events"
            if provider == "google"
            else "offline_access calendar:calendar:readonly calendar:calendar.event:read "
            "calendar:calendar.event:create calendar:calendar.event:update calendar:calendar.event:delete"
        ),
        auth_epoch=auth_epoch,
        authorized_at=now,
        token_generation=2,
        status=status,
        refresh_inflight_gen=None,
        refresh_started_at=None,
        expires_at=now + timedelta(hours=1),
        last_refresh_at=now,
        needs_reauth_at=None,
        last_error_code=None,
        reauth_notified_at=None,
        preferences={},
        has_access_token=True,
        has_refresh_token=True,
        created_at=now,
        updated_at=now,
    )


def _sample_token(
    *,
    provider: str = "google",
    auth_epoch: int = 7,
) -> FreshAccessToken:
    return FreshAccessToken(
        access_token=f"tok-{provider}-short-lived",
        provider=provider,
        service="calendar",
        oauth_client_id="12345-client.apps.googleusercontent.com",
        provider_account_id=f"acct-{provider}-1",
        auth_epoch=auth_epoch,
        token_generation=2,
        granted_scopes="calendar.events",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _tool_payload(cmd: Any) -> dict[str, Any]:
    msg = cmd.update["messages"][0]
    return json.loads(msg.content)


# ===========================================================================
# 1. Private thread guard, first-touch notice & sharing/visibility gates (§4.4)
# ===========================================================================


@pytest.mark.asyncio
async def test_private_surface_allows_home_records_touch_and_emits_first_touch_notice_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    rid = str(uuid4())
    ws_id = str(uuid4())
    cfg = {
        "configurable": {
            "user_id": "user-1",
            "workspace_id": ws_id,
            "thread_id": tid,
            "run_id": rid,
            "turn_index": 2,
            "agent_role": "chief_of_staff",
        }
    }
    home_row = {
        "conversation_thread_id": tid,
        "workspace_id": ws_id,
        "thread_owner": "user-1",
        "is_shared": False,
        "workspace_shared_at": None,
        "has_personal_integration_data": False,
        "personal_purge_in_progress": False,
        "ws_owner": "user-1",
        "ws_config": {"home": True},
        "ws_status": "running",
    }

    monkeypatch.setattr(guard_mod, "is_home_row", lambda ws: bool((ws.get("config") or {}).get("home")))
    monkeypatch.setattr(guard_mod, "is_home_stand_in_row", lambda ws: False)
    monkeypatch.setattr(guard_mod, "is_holder", lambda uid: False)
    get_conn_mock = create_autospec(db_mod.get_connection, return_value=_sample_conn())
    has_purge_mock = create_autospec(db_mod.has_running_local_purge_for_service, return_value=False)
    record_touch_mock = create_autospec(db_mod.record_touch, side_effect=[True, False])
    monkeypatch.setattr(guard_mod.db, "get_connection", get_conn_mock)
    monkeypatch.setattr(guard_mod.db, "has_running_local_purge_for_service", has_purge_mock)
    monkeypatch.setattr(guard_mod.db, "record_touch", record_touch_mock)

    first = await verify_personal_integration_surface(
        cfg,
        provider="google",
        service="calendar",
        auth_epoch=7,
        provider_account_id="acct-google-1",
        conn=_ScriptedConn([home_row]),
    )
    assert first.is_first_touch is True
    assert first.first_touch_notice == FIRST_TOUCH_NOTICE_CALENDAR
    record_touch_mock.assert_awaited_once()

    second = await verify_personal_integration_surface(
        cfg,
        provider="google",
        service="calendar",
        auth_epoch=7,
        provider_account_id="acct-google-1",
        conn=_ScriptedConn([home_row]),
    )
    assert second.is_first_touch is False
    assert second.first_touch_notice is None


@pytest.mark.asyncio
async def test_private_surface_rejects_default_cos_ro_and_colleague_dispatch_rw_and_group_and_shared_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    rid = str(uuid4())
    ws_id = str(uuid4())
    base_cfg = {
        "configurable": {
            "user_id": "user-1",
            "workspace_id": ws_id,
            "thread_id": tid,
            "run_id": rid,
            "turn_index": 0,
            "agent_role": "analyst",
        }
    }

    # Group chat short-circuits before DB
    group_cfg = {
        "configurable": {
            **base_cfg["configurable"],
            "platform": "feishu",
            "chat_type": "group",
        }
    }
    monkeypatch.setattr(guard_mod, "is_dalton_sharing_auth_enabled", lambda: True)
    monkeypatch.setattr(
        guard_mod,
        "is_run_outbound_blocked",
        create_autospec(guard_mod.is_run_outbound_blocked, return_value=False),
    )
    with pytest.raises(PersonalSurfaceDeniedError) as exc_group:
        await verify_personal_integration_surface(
            group_cfg, provider="google", service="calendar", record_touch_now=False
        )
    assert exc_group.value.code == "non_private_surface"

    # Non-Home workspace with no dalton_workspace_sharing row -> default cos_access='ro' -> rejected
    ws_thread_row = {
        "conversation_thread_id": tid,
        "workspace_id": ws_id,
        "thread_owner": "user-1",
        "is_shared": False,
        "workspace_shared_at": None,
        "has_personal_integration_data": False,
        "personal_purge_in_progress": False,
        "ws_owner": "user-1",
        "ws_config": {},
        "ws_status": "running",
    }
    monkeypatch.setattr(guard_mod, "is_home_row", lambda ws: False)
    monkeypatch.setattr(guard_mod, "is_home_stand_in_row", lambda ws: False)
    monkeypatch.setattr(guard_mod, "is_holder", lambda uid: False)
    monkeypatch.setattr(guard_mod, "is_colleague_dispatch_enabled", lambda: False)
    monkeypatch.setattr(guard_mod.db, "get_connection", create_autospec(db_mod.get_connection, return_value=_sample_conn()))
    monkeypatch.setattr(guard_mod.db, "has_running_local_purge_for_service", create_autospec(db_mod.has_running_local_purge_for_service, return_value=False))

    # Scripted rows: thread+ws row, shared_workspaces=None, dalton_workspace_sharing=None
    with pytest.raises(PersonalSurfaceDeniedError) as exc_ro:
        await verify_personal_integration_surface(
            base_cfg,
            provider="google",
            service="calendar",
            record_touch_now=False,
            conn=_ScriptedConn([ws_thread_row, None, None]),
        )
    assert exc_ro.value.code == "non_private_surface"

    # Colleague dispatch enabled with absent dalton_user_sharing -> default cos_rw_enabled=True -> rejected
    monkeypatch.setattr(guard_mod, "is_colleague_dispatch_enabled", lambda: True)
    with pytest.raises(PersonalSurfaceDeniedError) as exc_rw:
        await verify_personal_integration_surface(
            base_cfg,
            provider="google",
            service="calendar",
            record_touch_now=False,
            conn=_ScriptedConn([ws_thread_row, None, {"cos_access": "hidden", "web_access": "hidden"}, None]),
        )
    assert exc_rw.value.code == "non_private_surface"

    # Organization shared_workspaces row present -> rejected
    with pytest.raises(PersonalSurfaceDeniedError) as exc_shared_ws:
        await verify_personal_integration_surface(
            base_cfg,
            provider="google",
            service="calendar",
            record_touch_now=False,
            conn=_ScriptedConn([ws_thread_row, {"?column?": 1}]),
        )
    assert exc_shared_ws.value.code == "non_private_surface"


@pytest.mark.asyncio
async def test_private_surface_allows_double_hidden_personal_workspace_and_blocks_during_purge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    rid = str(uuid4())
    ws_id = str(uuid4())
    cfg = {
        "configurable": {
            "user_id": "user-1",
            "workspace_id": ws_id,
            "thread_id": tid,
            "run_id": rid,
            "turn_index": 1,
            "agent_role": "analyst",
        }
    }
    ws_thread_row = {
        "conversation_thread_id": tid,
        "workspace_id": ws_id,
        "thread_owner": "user-1",
        "is_shared": False,
        "workspace_shared_at": None,
        "has_personal_integration_data": False,
        "personal_purge_in_progress": False,
        "ws_owner": "user-1",
        "ws_config": {},
        "ws_status": "running",
    }
    monkeypatch.setattr(guard_mod, "is_dalton_sharing_auth_enabled", lambda: True)
    monkeypatch.setattr(guard_mod, "is_colleague_dispatch_enabled", lambda: True)
    monkeypatch.setattr(guard_mod, "is_home_row", lambda ws: False)
    monkeypatch.setattr(guard_mod, "is_home_stand_in_row", lambda ws: False)
    monkeypatch.setattr(guard_mod, "is_holder", lambda uid: False)
    monkeypatch.setattr(
        guard_mod,
        "is_run_outbound_blocked",
        create_autospec(guard_mod.is_run_outbound_blocked, return_value=False),
    )
    monkeypatch.setattr(guard_mod.db, "get_connection", create_autospec(db_mod.get_connection, return_value=_sample_conn()))
    monkeypatch.setattr(guard_mod.db, "has_running_local_purge_for_service", create_autospec(db_mod.has_running_local_purge_for_service, return_value=False))
    monkeypatch.setattr(guard_mod.db, "record_touch", create_autospec(db_mod.record_touch, return_value=True))

    # cos_rw_enabled=False and web_access='hidden' -> allowed
    res = await verify_personal_integration_surface(
        cfg,
        provider="google",
        service="calendar",
        auth_epoch=7,
        provider_account_id="acct-google-1",
        conn=_ScriptedConn(
            [
                ws_thread_row,
                None,
                {"cos_access": "hidden", "web_access": "hidden"},
                {"cos_rw_enabled": False},
            ]
        ),
    )
    assert res.is_first_touch is True

    # personal_purge_in_progress=True -> paused with purge wait code
    purging_row = {**ws_thread_row, "personal_purge_in_progress": True}
    with pytest.raises(PersonalSurfaceDeniedError) as exc_purge:
        await verify_personal_integration_surface(
            cfg,
            provider="google",
            service="calendar",
            record_touch_now=False,
            conn=_ScriptedConn([purging_row]),
        )
    assert exc_purge.value.code == "personal_data_purge_in_progress"
    assert exc_purge.value.is_purge_wait is True


@pytest.mark.asyncio
async def test_private_surface_rejects_outbound_blocked_analyst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    cfg = {
        "configurable": {
            "user_id": "user-1",
            "thread_id": tid,
            "agent_role": "analyst",
        }
    }
    monkeypatch.setattr(guard_mod, "is_dalton_sharing_auth_enabled", lambda: True)
    monkeypatch.setattr(
        guard_mod,
        "is_run_outbound_blocked",
        create_autospec(guard_mod.is_run_outbound_blocked, return_value=True),
    )
    with pytest.raises(PersonalSurfaceDeniedError) as exc:
        await verify_personal_integration_surface(
            cfg, provider="google", service="calendar", record_touch_now=False
        )
    assert exc.value.code == "colleague_outbound_blocked"


@pytest.mark.asyncio
async def test_sharing_gates_block_public_link_and_workspace_share_and_create_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    monkeypatch.setattr(guard_mod, "is_dalton_enabled", lambda: True)

    # assert_thread_sharable_tx raises 403 personal_integration_thread_not_sharable
    conn = _ScriptedConn([{"has_personal_integration_data": True}])
    with pytest.raises(PersonalIntegrationNotSharableError) as exc:
        await assert_thread_sharable_tx(conn, tid)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == PERSONAL_INTEGRATION_NOT_SHARABLE_CODE
    assert "FOR UPDATE" in conn.cur.executed[0][0]

    # create_thread refuses shared_with_workspace=True + has_personal_integration_data=True
    from src.server.database.conversation.threads_write import create_thread

    with pytest.raises(PersonalIntegrationNotSharableError):
        await create_thread(
            conversation_thread_id=tid,
            workspace_id=str(uuid4()),
            current_status="completed",
            created_by="user-1",
            shared_with_workspace=True,
            has_personal_integration_data=True,
        )


@pytest.mark.asyncio
async def test_require_thread_access_and_thread_access_return_404_for_non_owner_even_after_workspace_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    monkeypatch.setattr(guard_mod, "is_dalton_enabled", lambda: True)

    # Non-owner gets 404 on enforce_personal_thread_owner_only
    with pytest.raises(HTTPException) as exc:
        await enforce_personal_thread_owner_only(
            tid,
            "colleague-2",
            thread_owner="owner-1",
            meta={"has_personal_integration_data": True, "user_id": "owner-1"},
        )
    assert exc.value.status_code == 404

    # Owner is permitted
    await enforce_personal_thread_owner_only(
        tid,
        "owner-1",
        thread_owner="owner-1",
        meta={"has_personal_integration_data": True, "user_id": "owner-1"},
    )

    # Full require_thread_access blocks colleague even on web/cos after workspace opened
    from src.dalton import sharing as sharing_mod

    monkeypatch.setattr(sharing_mod, "_multiuser_auth_enabled", lambda: True)
    monkeypatch.setattr(sharing_mod, "_sharing_auth_enabled", lambda: True)
    monkeypatch.setattr(
        sharing_mod.conv_db,
        "get_thread_auth_meta",
        AsyncMock(
            return_value={
                "user_id": "owner-1",
                "thread_user_id": "owner-1",
                "workspace_owner_id": "owner-1",
                "workspace_id": str(uuid4()),
                "has_personal_integration_data": True,
            }
        ),
    )
    for ch in ("web", "cos"):
        with pytest.raises(HTTPException) as exc_ch:
            await sharing_mod.require_thread_access(tid, "colleague-2", channel=ch, need="read")
        assert exc_ch.value.status_code == 404


# ===========================================================================
# 2. HITLDecision extension & approval resume verification (§5.2)
# ===========================================================================


def test_hitl_decision_new_fields_and_serialize_hitl_response_map() -> None:
    decision = HITLDecision(
        type="approve",
        confirm_token="tok-abc",
        second_confirm_acknowledged=True,
        confirmed_payload_sha256="sha-deadbeef",
        elicitation_responses={
            "approval_choice": {
                "action": "accept",
                "content": {"approval_choice": "ALLOW_ONCE"},
            }
        },
    )
    resp = HITLResponse(decisions=[decision])
    serialized = serialize_hitl_response_map({"int-1": resp})
    d0 = serialized["int-1"]["decisions"][0]
    assert d0["confirm_token"] == "tok-abc"
    assert d0["second_confirm_acknowledged"] is True
    assert d0["confirmed_payload_sha256"] == "sha-deadbeef"
    assert d0["elicitation_responses"] == {
        "approval_choice": {
            "action": "accept",
            "content": {"approval_choice": "ALLOW_ONCE"},
        }
    }


def test_checkpoint_handler_includes_dalton_integration_approval_types() -> None:
    for t in DALTON_INTEGRATION_APPROVAL_TYPES:
        assert t in cp_handler.WEB_CONFIRM_ACTION_TYPES
        assert cp_handler._is_web_confirm_interrupt({"value": {"action_requests": [{"type": t}]}})


@pytest.mark.asyncio
async def test_verify_personal_integration_interrupt_resume_l1_l2_and_scope_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    aid = str(uuid4())
    payload_sha = canonical_payload_sha256({"action": "create_event", "summary": "Sync"})
    approval_row = ActionApprovalRow(
        approval_id=aid,
        caller_user_id="user-1",
        thread_id=tid,
        run_id=None,
        interrupt_id=None,
        action_type="dalton_calendar_create_event",
        provider="google",
        service="calendar",
        provider_account_id="acct-google-1",
        auth_epoch=7,
        token_generation=2,
        payload_sha256=payload_sha,
        input_requests_map=None,
        idempotent_message_id=None,
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        consumed_at=None,
        created_at=datetime.now(UTC),
    )

    monkeypatch.setattr(approval_mod.db, "get_action_approval", create_autospec(db_mod.get_action_approval, return_value=approval_row))
    monkeypatch.setattr(approval_mod.db, "get_connection", create_autospec(db_mod.get_connection, return_value=_sample_conn()))
    monkeypatch.setattr(approval_mod, "check_thread_surface_still_private", create_autospec(guard_mod.check_thread_surface_still_private, return_value=True))

    principal = SimpleNamespace(user_id="user-1", auth="session")
    action_req = {
        "type": "dalton_calendar_create_event",
        "approval_id": aid,
        "caller_user_id": "user-1",
        "confirm_token": "tok-123",
        "requires_second_confirm": True,
    }

    # Missing L2 second_confirm_acknowledged -> 400
    with pytest.raises(HTTPException) as exc_l2:
        await verify_personal_integration_interrupt_resume(
            thread_id=tid,
            action_req=action_req,
            answer=HITLResponse(decisions=[HITLDecision(type="approve", confirm_token="tok-123")]),
            principal=principal,
        )
    assert exc_l2.value.status_code == 400
    assert exc_l2.value.detail["code"] == "second_confirmation_required"

    # Valid L2 receipt -> returns True
    ok = await verify_personal_integration_interrupt_resume(
        thread_id=tid,
        action_req=action_req,
        answer=HITLResponse(
            decisions=[
                HITLDecision(
                    type="approve",
                    confirm_token="tok-123",
                    second_confirm_acknowledged=True,
                    confirmed_payload_sha256=payload_sha,
                )
            ]
        ),
        principal=principal,
    )
    assert ok is True

    # Surface drifted to non-private -> returns False (caller stamps scope_changed=True)
    monkeypatch.setattr(approval_mod, "check_thread_surface_still_private", create_autospec(guard_mod.check_thread_surface_still_private, return_value=False))
    drifted = await verify_personal_integration_interrupt_resume(
        thread_id=tid,
        action_req=action_req,
        answer=HITLResponse(
            decisions=[
                HITLDecision(
                    type="approve",
                    confirm_token="tok-123",
                    second_confirm_acknowledged=True,
                    confirmed_payload_sha256=payload_sha,
                )
            ]
        ),
        principal=principal,
    )
    assert drifted is False


def test_verify_personal_integration_interrupt_resume_google_elicitation_schema_and_decline() -> None:
    input_requests = {
        "approval_choice": {
            "method": "elicitation/create",
            "params": {
                "mode": "form",
                "message": "Allow creating event?",
                "requestedSchema": {
                    "type": "object",
                    "required": ["approval_choice"],
                    "properties": {
                        "approval_choice": {
                            "title": "Decision",
                            "type": "string",
                            "enum": ["ALLOW_ONCE", "DENY_ONCE"],
                            "enumNames": ["Allow once", "Don't allow"],
                        }
                    },
                },
            },
        }
    }

    ok, err, is_dec = validate_elicitation_responses(
        input_requests,
        {"approval_choice": {"action": "accept", "content": {"approval_choice": "ALLOW_ONCE"}}},
    )
    assert (ok, err, is_dec) == (True, None, False)

    ok_bad, err_bad, _ = validate_elicitation_responses(
        input_requests,
        {"approval_choice": {"action": "accept", "content": {"approval_choice": "INVALID"}}},
    )
    assert ok_bad is False and err_bad is not None

    ok_dec, err_dec, is_dec_flag = validate_elicitation_responses(
        input_requests,
        {"approval_choice": {"action": "decline"}},
    )
    assert (ok_dec, err_dec, is_dec_flag) == (True, None, True)

    ok_cancel, err_cancel, is_cancel_flag = validate_elicitation_responses(
        input_requests,
        {"approval_choice": {"action": "cancel"}},
    )
    assert (ok_cancel, err_cancel, is_cancel_flag) == (True, None, True)


# ===========================================================================
# 3. Google & Feishu calendar clients + dalton_calendar tool (§3.1, §5.1, §5.2)
# ===========================================================================


def test_google_calendar_mcp_stateless_meta_local_tz_and_elicitation_parsing() -> None:
    # Local time + IANA DST conversion (America/New_York winter vs summer)
    assert format_local_datetime_for_tz("2026-01-15T15:00:00Z", "America/New_York") == "2026-01-15T10:00:00"
    assert format_local_datetime_for_tz("2026-07-15T15:00:00Z", "America/New_York") == "2026-07-15T11:00:00"

    list_args = build_google_list_events_args(
        calendar_id="primary",
        time_min="2026-12-01T00:00:00Z",
        time_max="2026-12-08T00:00:00Z",
        tz_name="Asia/Shanghai",
        query="earnings",
        limit=25,
    )
    assert list_args["timeZone"] == "Asia/Shanghai"
    assert list_args["startTime"] == "2026-12-01T08:00:00"
    assert list_args["fullText"] == "earnings"
    assert list_args["pageSize"] == 25

    # Payload carries stateless _meta, requestState, and keyed inputResponses
    rpc = build_mcp_tools_call_payload(
        "create_event",
        {"calendarId": "primary", "summary": "Sync"},
        request_state="opaque-state-1",
        input_responses={
            "approval_choice": {"action": "accept", "content": {"approval_choice": "ALLOW_ONCE"}}
        },
    )
    assert rpc["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"] == {
        "elicitation": {"form": {}}
    }
    assert rpc["params"]["requestState"] == "opaque-state-1"
    assert "approval_choice" in rpc["params"]["inputResponses"]

    # Official InputRequiredResult shape raises GoogleMCPElicitationRequired
    official_resp = {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {
            "resultType": "input_required",
            "requestState": "secret-state-token",
            "inputRequests": {
                "approval_choice": {
                    "method": "elicitation/create",
                    "params": {
                        "mode": "form",
                        "message": "Confirm creating calendar event?",
                        "requestedSchema": {
                            "type": "object",
                            "properties": {
                                "approval_choice": {
                                    "title": "Choice",
                                    "enum": ["ALLOW_ONCE", "DENY_ONCE"],
                                    "enumNames": ["Allow once", "Don't allow"],
                                }
                            },
                        },
                    },
                }
            },
        },
    }
    with pytest.raises(GoogleMCPElicitationRequired) as exc:
        parse_google_mcp_response(
            official_resp,
            mcp_endpoint=GOOGLE_CALENDAR_MCP_ENDPOINT,
            mcp_tool_name="create_event",
            mcp_arguments={"summary": "Sync"},
        )
    assert exc.value.request_state == "secret-state-token"
    assert "secret-state-token" not in repr(exc.value)
    assert "approval_choice" in exc.value.input_requests


@pytest.mark.asyncio
async def test_google_and_feishu_calendar_clients_enforce_no_advisory_lock_held() -> None:
    g_client = GoogleCalendarMCPClient()
    f_client = FeishuCalendarClient()

    with advisory_lock_http_guard(True), track_advisory_lock("test_lock", 999):
        with pytest.raises(AdvisoryLockHeldDuringExternalCallError):
            await g_client.list_calendars(access_token="tok")
        with pytest.raises(AdvisoryLockHeldDuringExternalCallError):
            await g_client.call_mcp_tool(
                access_token="tok", tool_name="list_events", arguments={}
            )
        with pytest.raises(AdvisoryLockHeldDuringExternalCallError):
            await f_client.list_calendars(access_token="tok")


def test_dalton_calendar_capability_mounting_single_and_dual_and_unconnected_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Core default: neither google_calendar nor feishu_calendar enabled -> not mounted
    monkeypatch.setenv("DALTON_PROFILE", "core")
    monkeypatch.delenv("DALTON_CAP_GOOGLE_CALENDAR", raising=False)
    monkeypatch.delenv("DALTON_CAP_FEISHU_CALENDAR", raising=False)
    names_off = {t.name for t in build_dalton_tools("chief_of_staff", None, enabled=True)}
    assert "dalton_calendar" not in names_off

    # Single capability google_calendar -> mounted
    monkeypatch.setenv("DALTON_CAP_GOOGLE_CALENDAR", "true")
    names_g = {t.name for t in build_dalton_tools("chief_of_staff", None, enabled=True)}
    assert "dalton_calendar" in names_g

    # Single capability feishu_calendar -> mounted
    monkeypatch.setenv("DALTON_CAP_GOOGLE_CALENDAR", "false")
    monkeypatch.setenv("DALTON_CAP_FEISHU_CALENDAR", "true")
    names_f = {t.name for t in build_dalton_tools("analyst", None, enabled=True)}
    assert "dalton_calendar" in names_f


@pytest.mark.asyncio
async def test_dalton_calendar_unconnected_returns_readable_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cal_tools_mod,
        "effective_capabilities",
        lambda: frozenset({CapabilityId.GOOGLE_CALENDAR}),
    )
    monkeypatch.setattr(
        cal_tools_mod.db,
        "list_user_connections",
        create_autospec(db_mod.list_user_connections, return_value=[]),
    )
    tool_obj = create_calendar_tools()[0]
    cmd = await tool_obj.coroutine(
        action="list_events",
        config={"configurable": {"user_id": "user-1", "thread_id": str(uuid4())}},
        tool_call_id="tc-1",
    )
    res = _tool_payload(cmd)
    assert res["ok"] is False
    assert res["error"] == "not_connected"
    assert "/settings?tab=integrations" in res["message"]


@pytest.mark.asyncio
async def test_dalton_calendar_dual_provider_read_merges_with_labels_and_write_asks_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    rid = str(uuid4())
    g_cli = create_autospec(GoogleCalendarClientProtocol, instance=True)
    f_cli = create_autospec(FeishuCalendarClientProtocol, instance=True)

    g_cli.call_mcp_tool = AsyncMock(
        return_value={
            "events": [
                {
                    "id": "g-ev-1",
                    "summary": "Google Standup",
                    "start": {"dateTime": "2026-12-02T09:00:00+08:00"},
                    "end": {"dateTime": "2026-12-02T09:30:00+08:00"},
                }
            ]
        }
    )
    f_cli.list_events = AsyncMock(
        return_value=[
            {
                "provider": "feishu",
                "provider_label": "[飞书日历]",
                "calendar_id": "primary",
                "event_id": "f-ev-1",
                "summary": "飞书周会",
                "start": {"dateTime": "2026-12-02T10:00:00+08:00"},
                "end": {"dateTime": "2026-12-02T11:00:00+08:00"},
                "status": "confirmed",
            }
        ]
    )
    f_cli.list_events_page = AsyncMock(
        return_value={
            "items": [
                {
                    "provider": "feishu",
                    "provider_label": "[飞书日历]",
                    "calendar_id": "primary",
                    "event_id": "f-ev-1",
                    "summary": "飞书周会",
                    "start": {"dateTime": "2026-12-02T10:00:00+08:00"},
                    "end": {"dateTime": "2026-12-02T11:00:00+08:00"},
                    "status": "confirmed",
                }
            ],
            "has_more": False,
            "page_token": None,
        }
    )

    monkeypatch.setattr(
        cal_tools_mod,
        "effective_capabilities",
        lambda: frozenset({CapabilityId.GOOGLE_CALENDAR, CapabilityId.FEISHU_CALENDAR}),
    )
    monkeypatch.setattr(
        cal_tools_mod.db,
        "list_user_connections",
        create_autospec(
            db_mod.list_user_connections,
            return_value=[_sample_conn(provider="google"), _sample_conn(provider="feishu")],
        ),
    )
    monkeypatch.setattr(
        cal_tools_mod.db,
        "get_action_approval_by_stage_key",
        create_autospec(db_mod.get_action_approval_by_stage_key, return_value=None),
    )
    surf_calls = 0

    async def _fake_surface(config: Any, *, provider: str, **kwargs: Any) -> PersonalSurfaceVerificationResult:
        nonlocal surf_calls
        surf_calls += 1
        return PersonalSurfaceVerificationResult(
            caller_user_id="user-1",
            workspace_id="ws-1",
            thread_id=tid,
            response_id=rid,
            turn_index=0,
            is_first_touch=(surf_calls == 1),
            first_touch_notice=FIRST_TOUCH_NOTICE_CALENDAR if surf_calls == 1 else None,
        )

    monkeypatch.setattr(cal_tools_mod, "verify_personal_integration_surface", _fake_surface)
    monkeypatch.setattr(
        cal_tools_mod,
        "claim_first_touch_notice_if_needed",
        create_autospec(
            guard_mod.claim_first_touch_notice_if_needed,
            side_effect=[FIRST_TOUCH_NOTICE_CALENDAR, None, None],
        ),
    )

    async def _fake_token(user_id: str, provider: str, service: str, **kwargs: Any) -> FreshAccessToken:
        return _sample_token(provider=provider)

    monkeypatch.setattr(cal_tools_mod, "ensure_fresh_access_token", _fake_token)

    tool_obj = create_calendar_tools(google_client=g_cli, feishu_client=f_cli)[0]
    cfg = {"configurable": {"user_id": "user-1", "thread_id": tid, "run_id": rid}}

    # Read with auto merges both providers with labels & emits first_touch_notice
    read_cmd = await tool_obj.coroutine(
        action="list_events",
        provider="auto",
        time_min="2026-12-02T00:00:00+08:00",
        time_max="2026-12-02T23:59:59+08:00",
        timezone="Asia/Shanghai",
        config=cfg,
        tool_call_id="tc-read",
    )
    read_res = _tool_payload(read_cmd)
    assert read_res["ok"] is True
    assert read_res["first_touch_notice"] == FIRST_TOUCH_NOTICE_CALENDAR
    labels = {ev["provider_label"] for ev in read_res["events"]}
    assert labels == {"[Google 日历]", "[飞书日历]"}

    # Check availability merges busy slots and returns free windows
    avail_cmd = await tool_obj.coroutine(
        action="check_availability",
        provider="auto",
        time_min="2026-12-02T08:00:00+08:00",
        time_max="2026-12-02T12:00:00+08:00",
        timezone="Asia/Shanghai",
        duration_minutes=30,
        config=cfg,
        tool_call_id="tc-avail",
    )
    avail_res = _tool_payload(avail_cmd)
    assert avail_res["ok"] is True
    assert len(avail_res["availability"]["busy"]) == 2
    assert len(avail_res["availability"]["free"]) >= 2

    # Write with auto asks user to pick a platform
    write_cmd = await tool_obj.coroutine(
        action="create_event",
        provider="auto",
        summary="1:1",
        start_time="2026-12-02T14:00:00+08:00",
        end_time="2026-12-02T14:30:00+08:00",
        config=cfg,
        tool_call_id="tc-write",
    )
    write_res = _tool_payload(write_cmd)
    assert write_res["needs_provider_choice"] is True
    assert set(write_res["available_providers"]) == {"google", "feishu"}


def test_dalton_calendar_check_availability_computes_busy_and_free_slots() -> None:
    events = [
        {
            "provider_label": "[Google 日历]",
            "summary": "Morning Sync",
            "status": "confirmed",
            "start": {"dateTime": "2026-12-03T09:00:00+08:00"},
            "end": {"dateTime": "2026-12-03T10:00:00+08:00"},
        },
        {
            "provider_label": "[飞书日历]",
            "summary": "Overlap Call",
            "status": "confirmed",
            "start": {"dateTime": "2026-12-03T09:30:00+08:00"},
            "end": {"dateTime": "2026-12-03T10:30:00+08:00"},
        },
    ]
    res = compute_free_busy_slots(
        events,
        time_min="2026-12-03T08:00:00+08:00",
        time_max="2026-12-03T12:00:00+08:00",
        tz_name="Asia/Shanghai",
        min_slot_minutes=30,
    )
    assert len(res["free"]) == 2
    assert res["free"][0]["duration_minutes"] == 60
    assert res["free"][1]["duration_minutes"] == 90


@pytest.mark.asyncio
async def test_dalton_calendar_write_l1_and_l2_attendees_hitl_confirmation_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    rid = str(uuid4())
    f_cli = create_autospec(FeishuCalendarClientProtocol, instance=True)
    f_cli.create_event = AsyncMock(
        return_value={
            "provider": "feishu",
            "provider_label": "[飞书日历]",
            "calendar_id": "primary",
            "event_id": "feishu-new-1",
            "summary": "Client Review",
            "start": {"dateTime": "2026-12-04T14:00:00+08:00"},
            "end": {"dateTime": "2026-12-04T15:00:00+08:00"},
        }
    )

    monkeypatch.setattr(
        cal_tools_mod,
        "effective_capabilities",
        lambda: frozenset({CapabilityId.FEISHU_CALENDAR}),
    )
    monkeypatch.setattr(
        cal_tools_mod.db,
        "list_user_connections",
        create_autospec(db_mod.list_user_connections, return_value=[_sample_conn(provider="feishu")]),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "verify_personal_integration_surface",
        AsyncMock(
            return_value=PersonalSurfaceVerificationResult(
                caller_user_id="user-1",
                workspace_id="ws-1",
                thread_id=tid,
                response_id=rid,
                turn_index=0,
                is_first_touch=False,
                first_touch_notice=None,
            )
        ),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "ensure_fresh_access_token",
        AsyncMock(return_value=_sample_token(provider="feishu")),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "check_thread_surface_still_private",
        AsyncMock(return_value=True),
    )

    created_approvals: dict[str, ActionApprovalRow] = {}

    async def _fake_create_approval(**kwargs: Any) -> ActionApprovalRow:
        row = ActionApprovalRow(
            approval_id=str(kwargs["approval_id"]),
            caller_user_id=kwargs["caller_user_id"],
            thread_id=str(kwargs["thread_id"]),
            run_id=kwargs.get("run_id"),
            interrupt_id=None,
            action_type=kwargs["action_type"],
            provider=kwargs["provider"],
            service=kwargs["service"],
            provider_account_id=kwargs["provider_account_id"],
            auth_epoch=kwargs["auth_epoch"],
            token_generation=kwargs["token_generation"],
            payload_sha256=kwargs["payload_sha256"],
            input_requests_map=kwargs.get("input_requests_map"),
            idempotent_message_id=None,
            status="pending",
            expires_at=kwargs["expires_at"],
            consumed_at=None,
            created_at=datetime.now(UTC),
            elicitation_state=kwargs.get("elicitation_state"),
        )
        created_approvals[row.approval_id] = row
        return row

    async def _fake_consume(aid: str, uid: str, **kwargs: Any) -> ActionApprovalRow | None:
        return created_approvals.get(aid)

    monkeypatch.setattr(cal_tools_mod.db, "create_action_approval", _fake_create_approval)
    monkeypatch.setattr(cal_tools_mod.db, "consume_action_approval", _fake_consume)
    monkeypatch.setattr(
        cal_tools_mod.db,
        "get_action_approval_by_stage_key",
        create_autospec(db_mod.get_action_approval_by_stage_key, return_value=None),
    )
    monkeypatch.setattr(
        cal_tools_mod.db,
        "mark_action_approval_status",
        create_autospec(db_mod.mark_action_approval_status, return_value=True),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "claim_first_touch_notice_if_needed",
        create_autospec(guard_mod.claim_first_touch_notice_if_needed, return_value=None),
    )

    captured_requests: list[dict[str, Any]] = []

    def _fake_hitl_confirm(action_type: str, payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        req = {"type": action_type, **payload}
        captured_requests.append(req)
        return (
            True,
            {
                "decisions": [
                    {
                        "type": "approve",
                        "confirm_token": req["confirm_token"],
                        "second_confirm_acknowledged": True,
                        "confirmed_payload_sha256": req["payload_sha256"],
                    }
                ]
            },
        )

    monkeypatch.setattr(
        cal_tools_mod,
        "hitl_confirm",
        create_autospec(real_hitl_confirm, side_effect=_fake_hitl_confirm),
    )

    tool_obj = create_calendar_tools(feishu_client=f_cli)[0]
    cfg = {"configurable": {"user_id": "user-1", "thread_id": tid, "run_id": rid}}

    # L2 write with external attendee
    cmd = await tool_obj.coroutine(
        action="create_event",
        provider="feishu",
        summary="Client Review",
        start_time="2026-12-04T14:00:00+08:00",
        end_time="2026-12-04T15:00:00+08:00",
        timezone="Asia/Shanghai",
        attendees=["bob@external.com"],
        reminders=[{"minutes": 15}],
        config=cfg,
        tool_call_id="tc-l2",
    )
    res = _tool_payload(cmd)
    assert res["ok"] is True
    assert res["event"]["event_id"] == "feishu-new-1"
    assert captured_requests[0]["risk_level"] == "L2"
    assert captured_requests[0]["requires_second_confirm"] is True
    assert captured_requests[0]["type"] == "dalton_calendar_create_event"
    f_cli.create_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_dalton_calendar_google_mcp_multi_round_elicitation_accept_and_decline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    rid = str(uuid4())
    g_cli = create_autospec(GoogleCalendarClientProtocol, instance=True)

    calls_seen: list[dict[str, Any]] = []

    async def _multi_round_call(
        *,
        access_token: str,
        tool_name: str,
        arguments: Any,
        request_state: str | None = None,
        input_responses: Any = None,
    ) -> dict[str, Any]:
        calls_seen.append(
            {
                "tool_name": tool_name,
                "request_state": request_state,
                "input_responses": input_responses,
            }
        )
        if len(calls_seen) == 1:
            raise GoogleMCPElicitationRequired(
                mcp_endpoint=GOOGLE_CALENDAR_MCP_ENDPOINT,
                mcp_tool_name=tool_name,
                mcp_arguments=arguments,
                request_state="state-round-1",
                input_requests={
                    "step_one": {
                        "method": "elicitation/create",
                        "params": {
                            "mode": "form",
                            "message": "Round 1 confirm",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {"step_one": {"enum": ["ALLOW_ONCE", "DENY_ONCE"]}},
                            },
                        },
                    }
                },
            )
        if len(calls_seen) == 2:
            raise GoogleMCPElicitationRequired(
                mcp_endpoint=GOOGLE_CALENDAR_MCP_ENDPOINT,
                mcp_tool_name=tool_name,
                mcp_arguments=arguments,
                request_state="state-round-2",
                input_requests={
                    "step_two": {
                        "method": "elicitation/create",
                        "params": {
                            "mode": "form",
                            "message": "Round 2 confirm",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {"step_two": {"enum": ["CONFIRM"]}},
                            },
                        },
                    }
                },
            )
        return {"events": [{"id": "g-ev-99", "summary": "Verified Event"}]}

    g_cli.call_mcp_tool = AsyncMock(side_effect=_multi_round_call)

    monkeypatch.setattr(
        cal_tools_mod,
        "effective_capabilities",
        lambda: frozenset({CapabilityId.GOOGLE_CALENDAR}),
    )
    monkeypatch.setattr(
        cal_tools_mod.db,
        "list_user_connections",
        create_autospec(db_mod.list_user_connections, return_value=[_sample_conn(provider="google")]),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "verify_personal_integration_surface",
        AsyncMock(
            return_value=PersonalSurfaceVerificationResult(
                caller_user_id="user-1",
                workspace_id="ws-1",
                thread_id=tid,
                response_id=rid,
                turn_index=0,
                is_first_touch=False,
                first_touch_notice=None,
            )
        ),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "ensure_fresh_access_token",
        AsyncMock(return_value=_sample_token(provider="google")),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "check_thread_surface_still_private",
        AsyncMock(return_value=True),
    )

    stored: dict[str, ActionApprovalRow] = {}

    async def _store(**kwargs: Any) -> ActionApprovalRow:
        row = ActionApprovalRow(
            approval_id=str(kwargs["approval_id"]),
            caller_user_id=kwargs["caller_user_id"],
            thread_id=str(kwargs["thread_id"]),
            run_id=kwargs.get("run_id"),
            interrupt_id=None,
            action_type=kwargs["action_type"],
            provider=kwargs["provider"],
            service=kwargs["service"],
            provider_account_id=kwargs["provider_account_id"],
            auth_epoch=kwargs["auth_epoch"],
            token_generation=kwargs["token_generation"],
            payload_sha256=kwargs["payload_sha256"],
            input_requests_map=kwargs.get("input_requests_map"),
            idempotent_message_id=None,
            status="pending",
            expires_at=kwargs["expires_at"],
            consumed_at=None,
            created_at=datetime.now(UTC),
            elicitation_state=kwargs.get("elicitation_state"),
        )
        stored[row.approval_id] = row
        return row

    monkeypatch.setattr(cal_tools_mod.db, "create_action_approval", _store)
    monkeypatch.setattr(
        cal_tools_mod.db,
        "get_action_approval_by_stage_key",
        create_autospec(db_mod.get_action_approval_by_stage_key, return_value=None),
    )
    monkeypatch.setattr(
        cal_tools_mod.db,
        "consume_action_approval",
        AsyncMock(side_effect=lambda aid, uid, **kw: stored.get(aid)),
    )
    monkeypatch.setattr(
        cal_tools_mod.db,
        "update_action_approval_state",
        create_autospec(db_mod.update_action_approval_state, return_value=True),
    )
    monkeypatch.setattr(
        cal_tools_mod.db,
        "mark_action_approval_status",
        create_autospec(db_mod.mark_action_approval_status, return_value=True),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "claim_first_touch_notice_if_needed",
        create_autospec(guard_mod.claim_first_touch_notice_if_needed, return_value=None),
    )

    round_answers: list[tuple[bool, dict[str, Any]]] = [
        (
            True,
            {
                "decisions": [
                    {
                        "type": "approve",
                        "elicitation_responses": {
                            "step_one": {"action": "accept", "content": {"step_one": "ALLOW_ONCE"}}
                        },
                    }
                ]
            },
        ),
        (
            True,
            {
                "decisions": [
                    {
                        "type": "approve",
                        "elicitation_responses": {
                            "step_two": {"action": "accept", "content": {"step_two": "CONFIRM"}}
                        },
                    }
                ]
            },
        ),
    ]
    monkeypatch.setattr(
        cal_tools_mod,
        "hitl_confirm",
        create_autospec(
            real_hitl_confirm,
            side_effect=lambda action_type, payload: round_answers.pop(0),
        ),
    )

    tool_obj = create_calendar_tools(google_client=g_cli)[0]
    cmd = await tool_obj.coroutine(
        action="list_events",
        provider="google",
        time_min="2026-12-05T00:00:00+08:00",
        time_max="2026-12-06T00:00:00+08:00",
        config={"configurable": {"user_id": "user-1", "thread_id": tid, "run_id": rid}},
        tool_call_id="tc-elic",
    )
    res = _tool_payload(cmd)
    assert res["ok"] is True
    assert res["events"][0]["event_id"] == "g-ev-99"
    # Verify round 2 preserved step_one response alongside step_two (§7.6)
    assert calls_seen[2]["request_state"] == "state-round-2"
    assert set(calls_seen[2]["input_responses"].keys()) == {"step_one", "step_two"}

    # Verify decline sends decline response to /mcp/v1 and returns user_cancelled
    decline_seen: list[dict[str, Any]] = []

    async def _decline_call(
        *,
        access_token: str,
        tool_name: str,
        arguments: Any,
        request_state: str | None = None,
        input_responses: Any = None,
    ) -> dict[str, Any]:
        decline_seen.append({"request_state": request_state, "input_responses": input_responses})
        if len(decline_seen) == 1:
            raise GoogleMCPElicitationRequired(
                mcp_endpoint=GOOGLE_CALENDAR_MCP_ENDPOINT,
                mcp_tool_name=tool_name,
                mcp_arguments=arguments,
                request_state="state-decline",
                input_requests={
                    "approval_choice": {
                        "method": "elicitation/create",
                        "params": {"mode": "form", "message": "Confirm?"},
                    }
                },
            )
        return {"cancelled": True}

    g_cli.call_mcp_tool = AsyncMock(side_effect=_decline_call)
    monkeypatch.setattr(
        cal_tools_mod,
        "hitl_confirm",
        create_autospec(
            real_hitl_confirm,
            return_value=(
                False,
                {
                    "decisions": [
                        {
                            "type": "reject",
                            "elicitation_responses": {"approval_choice": {"action": "decline"}},
                        }
                    ]
                },
            ),
        ),
    )
    dec_cmd = await tool_obj.coroutine(
        action="list_events",
        provider="google",
        time_min="2026-12-05T00:00:00+08:00",
        time_max="2026-12-06T00:00:00+08:00",
        config={"configurable": {"user_id": "user-1", "thread_id": tid, "run_id": rid}},
        tool_call_id="tc-dec",
    )
    dec_res = _tool_payload(dec_cmd)
    assert dec_res["ok"] is False
    assert dec_res["error"] == "user_cancelled"
    assert decline_seen[1]["input_responses"] == {"approval_choice": {"action": "decline"}}
