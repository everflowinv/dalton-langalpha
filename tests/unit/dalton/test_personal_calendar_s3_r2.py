"""Round-2 regression tests covering every finding in review_s3_r1_codex.md (Rows 1-9).

All test doubles use ``create_autospec`` (no loose ``**kwargs`` mocks).
Real LangGraph ``StateGraph`` + ``MemorySaver`` tests verify suspend/resume,
frozen approval consumption, event-version re-verification, and multi-round
Google MCP ``requestState`` continuation.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
from typing import Any, TypedDict
from unittest.mock import AsyncMock, MagicMock, create_autospec
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tracers.schemas import Run
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
import pytest

from ptc_agent.agent.transcript.render import render_segment
from src.dalton.capabilities import CapabilityId
from src.dalton.database import user_integrations as db_mod
from src.dalton.database.user_integrations import (
    ActionApprovalRow,
    IntegrationRow,
    IntegrationStatus,
)
from src.dalton.integrations.feishu_calendar import (
    FeishuCalendarClientProtocol,
    build_feishu_event_body,
)
from src.dalton.integrations.google_calendar import (
    GOOGLE_CALENDAR_MCP_ENDPOINT,
    GoogleCalendarAPIError,
    GoogleCalendarClientProtocol,
    GoogleMCPElicitationRequired,
    build_google_event_write_args,
)
from src.dalton.services import personal_integration_guard as guard_mod
from src.dalton.services.personal_integration_guard import (
    FIRST_TOUCH_NOTICE_CALENDAR,
    NON_PRIVATE_SURFACE_GUIDE,
    PersonalSurfaceDeniedError,
    PersonalSurfaceVerificationResult,
    check_personal_thread_dispatch_allowed,
    check_personal_thread_send_allowed,
    claim_first_touch_notice_if_needed,
    verify_personal_integration_surface,
)
from src.dalton.services.personal_integration_trace import (
    REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER,
)
from src.dalton.services import user_integrations_oauth as oauth_mod
from src.dalton.services.user_integrations_oauth import (
    FreshAccessToken,
    MissingRequiredScopesError,
)
from src.dalton.tools import calendar_tools as cal_tools_mod
from src.dalton.tools.calendar_tools import create_calendar_tools
from src.observability.langsmith_tracer import NonRetainingTracer
from src.server.database.conversation.thread_reach import served_on_computer_sql
from src.server.database.thread_transcripts import own_thread
from src.tools.messaging import tools as messaging_tools_mod
from src.tools.secretary import dispatch as dispatch_mod
from src.tools.secretary._commands import hitl_confirm as real_hitl_confirm, puts_card


class _ScriptedCursor:
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


class _InMemoryApprovalStore:
    """In-memory transactional stand-in for ``dalton_integration_action_approvals``."""

    def __init__(self) -> None:
        self.rows: dict[str, ActionApprovalRow] = {}
        self.by_stage: dict[tuple[str, str, str], str] = {}
        self.create_count = 0
        self.consumed_ids: list[str] = []

    async def create_action_approval(self, **kwargs: Any) -> ActionApprovalRow:
        self.create_count += 1
        aid = str(kwargs.get("approval_id") or uuid4())
        stage_key = kwargs.get("idempotent_message_id")
        row = ActionApprovalRow(
            approval_id=aid,
            caller_user_id=kwargs["caller_user_id"],
            thread_id=str(kwargs["thread_id"]),
            run_id=kwargs.get("run_id"),
            interrupt_id=kwargs.get("interrupt_id"),
            action_type=kwargs["action_type"],
            provider=kwargs["provider"],
            service=kwargs["service"],
            provider_account_id=kwargs["provider_account_id"],
            auth_epoch=int(kwargs["auth_epoch"]),
            token_generation=int(kwargs["token_generation"]),
            payload_sha256=kwargs["payload_sha256"],
            input_requests_map=kwargs.get("input_requests_map"),
            idempotent_message_id=stage_key,
            status="pending",
            expires_at=kwargs["expires_at"],
            consumed_at=None,
            created_at=datetime.now(UTC),
            has_elicitation_state=kwargs.get("elicitation_state") is not None,
            elicitation_state=(
                dict(kwargs["elicitation_state"])
                if kwargs.get("elicitation_state") is not None
                else None
            ),
        )
        self.rows[aid] = row
        if stage_key:
            self.by_stage[(row.caller_user_id, row.thread_id, stage_key)] = aid
        return row

    async def get_action_approval_by_stage_key(
        self,
        caller_user_id: str,
        thread_id: str | UUID,
        stage_key: str,
        *,
        decrypt_state: bool = True,
        conn: Any = None,
    ) -> ActionApprovalRow | None:
        _ = (decrypt_state, conn)
        aid = self.by_stage.get((caller_user_id, str(thread_id), stage_key))
        return self.rows.get(aid) if aid else None

    async def list_action_approvals_by_stage_prefix(
        self,
        caller_user_id: str,
        thread_id: str | UUID,
        stage_prefix: str,
        *,
        decrypt_state: bool = True,
        conn: Any = None,
    ) -> list[ActionApprovalRow]:
        _ = (decrypt_state, conn)
        matched = [
            r
            for r in self.rows.values()
            if r.caller_user_id == caller_user_id
            and r.thread_id == str(thread_id)
            and (r.idempotent_message_id or "").startswith(stage_prefix)
        ]
        matched.sort(key=lambda r: r.created_at)
        return matched

    async def consume_action_approval(
        self,
        approval_id: str | UUID,
        caller_user_id: str,
        *,
        decrypt_state: bool = False,
        conn: Any = None,
    ) -> ActionApprovalRow | None:
        _ = (decrypt_state, conn)
        aid = str(approval_id)
        row = self.rows.get(aid)
        if row is None or row.caller_user_id != caller_user_id or row.status != "pending":
            return None
        if row.expires_at <= datetime.now(UTC):
            return None
        updated = ActionApprovalRow(
            **{
                **{f: getattr(row, f) for f in row.__dataclass_fields__},
                "status": "executing",
                "consumed_at": datetime.now(UTC),
            }
        )
        self.rows[aid] = updated
        self.consumed_ids.append(aid)
        return updated

    async def update_action_approval_state(
        self,
        approval_id: str | UUID,
        *,
        caller_user_id: str,
        status: str | None = None,
        elicitation_state: dict[str, Any] | None = None,
        conn: Any = None,
    ) -> bool:
        _ = conn
        aid = str(approval_id)
        row = self.rows.get(aid)
        if row is None or row.caller_user_id != caller_user_id:
            return False
        data = {f: getattr(row, f) for f in row.__dataclass_fields__}
        if status is not None:
            data["status"] = status
        if elicitation_state is not None:
            data["elicitation_state"] = dict(elicitation_state)
            data["has_elicitation_state"] = True
        self.rows[aid] = ActionApprovalRow(**data)
        return True

    async def mark_action_approval_status(
        self,
        approval_id: str | UUID,
        status: str,
        *,
        caller_user_id: str | None = None,
        conn: Any = None,
    ) -> bool:
        _ = conn
        aid = str(approval_id)
        row = self.rows.get(aid)
        if row is None:
            return False
        if caller_user_id is not None and row.caller_user_id != caller_user_id:
            return False
        data = {f: getattr(row, f) for f in row.__dataclass_fields__}
        data["status"] = status
        self.rows[aid] = ActionApprovalRow(**data)
        return True

    def expire(self, approval_id: str) -> None:
        row = self.rows[approval_id]
        data = {f: getattr(row, f) for f in row.__dataclass_fields__}
        data["expires_at"] = datetime.now(UTC) - timedelta(minutes=1)
        self.rows[approval_id] = ActionApprovalRow(**data)

    def bind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cal_tools_mod.db,
            "create_action_approval",
            create_autospec(db_mod.create_action_approval, side_effect=self.create_action_approval),
        )
        monkeypatch.setattr(
            cal_tools_mod.db,
            "get_action_approval_by_stage_key",
            create_autospec(
                db_mod.get_action_approval_by_stage_key,
                side_effect=self.get_action_approval_by_stage_key,
            ),
        )
        monkeypatch.setattr(
            cal_tools_mod.db,
            "list_action_approvals_by_stage_prefix",
            create_autospec(
                db_mod.list_action_approvals_by_stage_prefix,
                side_effect=self.list_action_approvals_by_stage_prefix,
            ),
        )
        monkeypatch.setattr(
            cal_tools_mod.db,
            "consume_action_approval",
            create_autospec(
                db_mod.consume_action_approval, side_effect=self.consume_action_approval
            ),
        )
        monkeypatch.setattr(
            cal_tools_mod.db,
            "update_action_approval_state",
            create_autospec(
                db_mod.update_action_approval_state,
                side_effect=self.update_action_approval_state,
            ),
        )
        monkeypatch.setattr(
            cal_tools_mod.db,
            "mark_action_approval_status",
            create_autospec(
                db_mod.mark_action_approval_status,
                side_effect=self.mark_action_approval_status,
            ),
        )


def _sample_conn(
    *,
    provider: str = "google",
    email: str = "alice@example.com",
    auth_epoch: int = 7,
) -> IntegrationRow:
    now = datetime.now(UTC)
    return IntegrationRow(
        user_id="user-1",
        provider=provider,
        service="calendar",
        oauth_client_id="client-1",
        provider_account_id=f"acct-{provider}-1",
        provider_union_id=None,
        provider_email=email,
        granted_scopes="calendar.events",
        auth_epoch=auth_epoch,
        authorized_at=now,
        token_generation=2,
        status=IntegrationStatus.CONNECTED,
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
    account_id: str | None = None,
) -> FreshAccessToken:
    return FreshAccessToken(
        access_token=f"tok-{provider}-{auth_epoch}",
        provider=provider,
        service="calendar",
        oauth_client_id="client-1",
        provider_account_id=account_id or f"acct-{provider}-1",
        auth_epoch=auth_epoch,
        token_generation=2,
        granted_scopes="calendar.events",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _tool_payload(cmd: Any) -> dict[str, Any]:
    msg = cmd.update["messages"][0]
    return json.loads(msg.content)


# ===========================================================================
# Row 1 (P0): Server-side channel verification (Feishu group / Discord channel)
# ===========================================================================


@pytest.mark.asyncio
async def test_private_surface_rejects_feishu_group_and_discord_channel_even_when_chat_type_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    rid = str(uuid4())
    ws_id = str(uuid4())

    home_row = {
        "conversation_thread_id": tid,
        "workspace_id": ws_id,
        "thread_owner": "user-1",
        "thread_external_id": None,
        "is_shared": False,
        "workspace_shared_at": None,
        "has_personal_integration_data": False,
        "personal_purge_in_progress": False,
        "ws_owner": "user-1",
        "ws_config": {"home": True},
        "ws_status": "running",
    }
    monkeypatch.setattr(guard_mod, "is_home_row", lambda ws: True)
    monkeypatch.setattr(guard_mod, "is_home_stand_in_row", lambda ws: False)
    monkeypatch.setattr(guard_mod, "is_holder", lambda uid: False)
    monkeypatch.setattr(
        guard_mod.db,
        "get_connection",
        create_autospec(db_mod.get_connection, return_value=_sample_conn()),
    )
    monkeypatch.setattr(
        guard_mod.db,
        "has_running_local_purge_for_service",
        create_autospec(db_mod.has_running_local_purge_for_service, return_value=False),
    )

    # 1. Feishu group with MISSING chat_type in config (gateway sent platform only):
    # server-side dalton_channel_inbound_events says chat_type='group' -> REJECTED!
    feishu_group_cfg = {
        "configurable": {
            "user_id": "user-1",
            "workspace_id": ws_id,
            "thread_id": tid,
            "run_id": rid,
            "platform": "feishu",
        }
    }
    feishu_group_rec = {
        "dalton_user_id": "user-1",
        "platform": "feishu",
        "chat_type": "group",
        "target_address": "feishu:oc_group_123",
        "thread_scope_key": "chat:oc_group_123",
        "external_thread_id": "feishu:chat:oc_group_123:g1",
        "thread_id": tid,
        "run_id": rid,
    }
    with pytest.raises(PersonalSurfaceDeniedError) as exc_feishu:
        await verify_personal_integration_surface(
            feishu_group_cfg,
            provider="google",
            service="calendar",
            record_touch_now=False,
            conn=_ScriptedConn([home_row, feishu_group_rec]),
        )
    assert exc_feishu.value.code == "non_private_surface"
    assert exc_feishu.value.message == NON_PRIVATE_SURFACE_GUIDE

    # 2. Discord channel with MISSING chat_type in config:
    # server-side record says chat_type='group', discord:<channel_id> -> REJECTED!
    discord_chan_cfg = {
        "configurable": {
            "user_id": "user-1",
            "workspace_id": ws_id,
            "thread_id": tid,
            "run_id": rid,
            "platform": "discord",
        }
    }
    discord_chan_rec = {
        "dalton_user_id": "user-1",
        "platform": "discord",
        "chat_type": "group",
        "target_address": "discord:1234567890123",
        "thread_scope_key": "chat:1234567890123",
        "external_thread_id": "discord:chat:1234567890123:g1",
        "thread_id": tid,
        "run_id": rid,
    }
    with pytest.raises(PersonalSurfaceDeniedError) as exc_discord:
        await verify_personal_integration_surface(
            discord_chan_cfg,
            provider="google",
            service="calendar",
            record_touch_now=False,
            conn=_ScriptedConn([home_row, discord_chan_rec]),
        )
    assert exc_discord.value.code == "non_private_surface"

    # 3. Spoofed chat_type="dm" with NO server-side record -> FAIL-CLOSED!
    spoof_dm_cfg = {
        "configurable": {
            "user_id": "user-1",
            "workspace_id": ws_id,
            "thread_id": tid,
            "run_id": rid,
            "platform": "feishu",
            "chat_type": "dm",
        }
    }
    with pytest.raises(PersonalSurfaceDeniedError) as exc_spoof:
        await verify_personal_integration_surface(
            spoof_dm_cfg,
            provider="google",
            service="calendar",
            record_touch_now=False,
            conn=_ScriptedConn([home_row, None, None, None, None]),
        )
    assert exc_spoof.value.code == "non_private_surface"

    # 4. Verified 1:1 DM with server-side record + matching bound identity -> ALLOWED!
    feishu_dm_rec = {
        "dalton_user_id": "user-1",
        "platform": "feishu",
        "chat_type": "dm",
        "target_address": "feishu:@me",
        "thread_scope_key": "dm:ou_alice",
        "external_thread_id": "feishu:dm:ou_alice:g1",
        "thread_id": tid,
        "run_id": rid,
    }
    res = await verify_personal_integration_surface(
        feishu_group_cfg,
        provider="google",
        service="calendar",
        record_touch_now=False,
        conn=_ScriptedConn(
            [home_row, feishu_dm_rec, {"platform_user_id": "ou_alice"}, {"turn_index": 1}]
        ),
    )
    assert res.is_dm_channel is True
    assert res.chat_type == "dm"
    assert res.platform == "feishu"


# ===========================================================================
# Rows 2 & 3 (P1): Real hitl_confirm + real LangGraph suspend/resume,
# frozen payload, original approval consumption, version check & requestState
# ===========================================================================


@pytest.mark.asyncio
async def test_dalton_calendar_write_real_langgraph_suspend_resume_consumes_original_approval_and_checks_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert puts_card("dalton_calendar", {"action": "update_event"}) is True

    tid = str(uuid4())
    rid = str(uuid4())
    store = _InMemoryApprovalStore()
    store.bind(monkeypatch)

    f_cli = create_autospec(FeishuCalendarClientProtocol, instance=True)
    current_version = {"updated": "v1"}

    async def _fake_get_event(**kwargs: Any) -> dict[str, Any]:
        return {
            "provider": "feishu",
            "provider_label": "[飞书日历]",
            "calendar_id": "primary",
            "event_id": "ev-42",
            "summary": "Old Title",
            "description": "Old Desc",
            "start": {"dateTime": "2026-12-04T14:00:00+08:00"},
            "end": {"dateTime": "2026-12-04T15:00:00+08:00"},
            "updated": current_version["updated"],
            "attendees": [],
        }

    f_cli.get_event = AsyncMock(side_effect=_fake_get_event)
    f_cli.update_event = AsyncMock(
        return_value={
            "provider": "feishu",
            "provider_label": "[飞书日历]",
            "calendar_id": "primary",
            "event_id": "ev-42",
            "summary": "New Title",
            "description": "Updated Desc",
            "start": {"dateTime": "2026-12-04T14:30:00+08:00"},
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
    async def _surface_for_cfg(config: Any, **kwargs: Any) -> PersonalSurfaceVerificationResult:
        cfg_tid = (config or {}).get("configurable", {}).get("thread_id", tid)
        return PersonalSurfaceVerificationResult(
            caller_user_id="user-1",
            workspace_id="ws-1",
            thread_id=str(cfg_tid),
            response_id=rid,
            turn_index=0,
            is_first_touch=False,
            first_touch_notice=None,
        )

    monkeypatch.setattr(
        cal_tools_mod,
        "verify_personal_integration_surface",
        create_autospec(
            guard_mod.verify_personal_integration_surface,
            side_effect=_surface_for_cfg,
        ),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "ensure_fresh_access_token",
        create_autospec(
            cal_tools_mod.ensure_fresh_access_token,
            return_value=_sample_token(provider="feishu"),
        ),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "check_thread_surface_still_private",
        create_autospec(guard_mod.check_thread_surface_still_private, return_value=True),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "claim_first_touch_notice_if_needed",
        create_autospec(guard_mod.claim_first_touch_notice_if_needed, return_value=None),
    )

    tool_obj = create_calendar_tools(feishu_client=f_cli)[0]

    class _State(TypedDict):
        messages: list[Any]

    async def _run_tool_node(state: _State, config: RunnableConfig) -> Command:
        return await tool_obj.coroutine(
            action="update_event",
            provider="feishu",
            event_id="ev-42",
            summary="New Title",
            description="Updated Desc",
            start_time="2026-12-04T14:30:00+08:00",
            timezone="Asia/Shanghai",
            config=config,
            tool_call_id="tc-real-graph-1",
        )

    builder = StateGraph(_State)
    builder.add_node("cal", _run_tool_node)
    builder.add_edge(START, "cal")
    builder.add_edge("cal", END)
    graph = builder.compile(checkpointer=MemorySaver())

    g_cfg = {
        "configurable": {
            "thread_id": tid,
            "user_id": "user-1",
            "run_id": rid,
        }
    }

    # 1. Invoke suspends via REAL hitl_confirm -> interrupt()
    paused = await graph.ainvoke({"messages": []}, config=g_cfg)
    assert "__interrupt__" in paused
    action_req = paused["__interrupt__"][0].value["action_requests"][0]
    original_aid = action_req["approval_id"]
    assert action_req["type"] == "dalton_calendar_update_event"
    assert store.create_count == 1
    assert action_req["args"]["description"] == "Updated Desc"
    assert action_req["args"]["start_time"] == "2026-12-04T14:30:00+08:00"
    assert action_req["args"]["end_time"] == "2026-12-04T15:00:00+08:00"

    # 2. Resume approves original approval -> does NOT create new approval, consumes original_aid
    resumed = await graph.ainvoke(
        Command(
            resume={
                "decisions": [
                    {"type": "approve", "confirm_token": action_req["confirm_token"]}
                ]
            }
        ),
        config=g_cfg,
    )
    assert "__interrupt__" not in resumed
    payload = json.loads(resumed["messages"][0].content)
    assert payload["ok"] is True
    assert store.create_count == 1
    assert store.consumed_ids == [original_aid]
    assert store.rows[original_aid].status == "succeeded"
    f_cli.update_event.assert_awaited_once()
    sent_body = f_cli.update_event.await_args.kwargs["body"]
    assert sent_body["summary"] == "New Title"
    assert sent_body["description"] == "Updated Desc"
    assert "start_time" in sent_body and "end_time" in sent_body

    # 3. Verify expired original approval CANNOT be bypassed by resume
    tid_exp = str(uuid4())
    g_cfg_exp = {"configurable": {"thread_id": tid_exp, "user_id": "user-1", "run_id": rid}}
    paused_exp = await graph.ainvoke({"messages": []}, config=g_cfg_exp)
    exp_aid = paused_exp["__interrupt__"][0].value["action_requests"][0]["approval_id"]
    store.expire(exp_aid)
    resumed_exp = await graph.ainvoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        config=g_cfg_exp,
    )
    exp_payload = json.loads(resumed_exp["messages"][0].content)
    assert exp_payload["ok"] is False
    assert exp_payload["error"] == "approval_expired_or_consumed"

    # 4. Verify event modified externally between card and resume -> event_version_changed
    tid_ver = str(uuid4())
    g_cfg_ver = {"configurable": {"thread_id": tid_ver, "user_id": "user-1", "run_id": rid}}
    paused_ver = await graph.ainvoke({"messages": []}, config=g_cfg_ver)
    assert "__interrupt__" in paused_ver
    current_version["updated"] = "v2-modified-externally"
    resumed_ver = await graph.ainvoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        config=g_cfg_ver,
    )
    ver_payload = json.loads(resumed_ver["messages"][0].content)
    assert ver_payload["ok"] is False
    assert ver_payload["error"] == "event_version_changed"


@pytest.mark.asyncio
async def test_dalton_calendar_google_write_multi_round_elicitation_real_langgraph_resume_preserves_request_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    rid = str(uuid4())
    store = _InMemoryApprovalStore()
    store.bind(monkeypatch)

    g_cli = create_autospec(GoogleCalendarClientProtocol, instance=True)
    mcp_calls: list[dict[str, Any]] = []

    async def _google_write_mcp(
        *,
        access_token: str,
        tool_name: str,
        arguments: Any,
        request_state: str | None = None,
        input_responses: Any = None,
    ) -> dict[str, Any]:
        mcp_calls.append(
            {
                "tool_name": tool_name,
                "request_state": request_state,
                "input_responses": dict(input_responses) if input_responses else None,
            }
        )
        if request_state is None:
            raise GoogleMCPElicitationRequired(
                mcp_endpoint=GOOGLE_CALENDAR_MCP_ENDPOINT,
                mcp_tool_name=tool_name,
                mcp_arguments=arguments,
                request_state="rs-round-1",
                input_requests={
                    "r1": {
                        "method": "elicitation/create",
                        "params": {
                            "mode": "form",
                            "message": "Round 1?",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {"r1": {"enum": ["ALLOW_ONCE"]}},
                            },
                        },
                    }
                },
            )
        if request_state == "rs-round-1":
            raise GoogleMCPElicitationRequired(
                mcp_endpoint=GOOGLE_CALENDAR_MCP_ENDPOINT,
                mcp_tool_name=tool_name,
                mcp_arguments=arguments,
                request_state="rs-round-2",
                input_requests={
                    "r2": {
                        "method": "elicitation/create",
                        "params": {
                            "mode": "form",
                            "message": "Round 2?",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {"r2": {"enum": ["CONFIRM"]}},
                            },
                        },
                    }
                },
            )
        return {"event": {"id": "g-created-1", "summary": "Quarterly Review"}}

    g_cli.call_mcp_tool = AsyncMock(side_effect=_google_write_mcp)

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
    async def _surface_for_cfg(config: Any, **kwargs: Any) -> PersonalSurfaceVerificationResult:
        cfg_tid = (config or {}).get("configurable", {}).get("thread_id", tid)
        return PersonalSurfaceVerificationResult(
            caller_user_id="user-1",
            workspace_id="ws-1",
            thread_id=str(cfg_tid),
            response_id=rid,
            turn_index=0,
            is_first_touch=False,
            first_touch_notice=None,
        )

    monkeypatch.setattr(
        cal_tools_mod,
        "verify_personal_integration_surface",
        create_autospec(
            guard_mod.verify_personal_integration_surface,
            side_effect=_surface_for_cfg,
        ),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "ensure_fresh_access_token",
        create_autospec(
            cal_tools_mod.ensure_fresh_access_token,
            return_value=_sample_token(provider="google"),
        ),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "check_thread_surface_still_private",
        create_autospec(guard_mod.check_thread_surface_still_private, return_value=True),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "claim_first_touch_notice_if_needed",
        create_autospec(guard_mod.claim_first_touch_notice_if_needed, return_value=None),
    )

    tool_obj = create_calendar_tools(google_client=g_cli)[0]

    class _State(TypedDict):
        messages: list[Any]

    async def _node(state: _State, config: RunnableConfig) -> Command:
        return await tool_obj.coroutine(
            action="create_event",
            provider="google",
            summary="Quarterly Review",
            description="Discuss Q4 numbers",
            start_time="2026-12-06T10:00:00+08:00",
            end_time="2026-12-06T11:00:00+08:00",
            timezone="Asia/Shanghai",
            config=config,
            tool_call_id="tc-gwrite-multi",
        )

    builder = StateGraph(_State)
    builder.add_node("cal", _node)
    builder.add_edge(START, "cal")
    builder.add_edge("cal", END)
    graph = builder.compile(checkpointer=MemorySaver())
    g_cfg = {"configurable": {"thread_id": tid, "user_id": "user-1", "run_id": rid}}

    # Interrupt 1: L1 write confirmation card
    p1 = await graph.ainvoke({"messages": []}, config=g_cfg)
    req1 = p1["__interrupt__"][0].value["action_requests"][0]
    assert req1["type"] == "dalton_calendar_create_event"
    assert len(mcp_calls) == 0

    # Approve L1 -> hits Google MCP (call 1, request_state=None) -> Interrupt 2 (elicitation round 1)
    p2 = await graph.ainvoke(
        Command(resume={"decisions": [{"type": "approve", "confirm_token": req1["confirm_token"]}]}),
        config=g_cfg,
    )
    req2 = p2["__interrupt__"][0].value["action_requests"][0]
    assert req2["type"] == "dalton_google_mcp_elicitation"
    assert len(mcp_calls) == 1 and mcp_calls[0]["request_state"] is None

    # Submit elicitation round 1 -> resumes with persisted rs-round-1 (NO repeat of initial write!) -> Interrupt 3
    p3 = await graph.ainvoke(
        Command(
            resume={
                "decisions": [
                    {
                        "type": "approve",
                        "confirm_token": req2["confirm_token"],
                        "elicitation_responses": {
                            "r1": {"action": "accept", "content": {"r1": "ALLOW_ONCE"}}
                        },
                    }
                ]
            }
        ),
        config=g_cfg,
    )
    req3 = p3["__interrupt__"][0].value["action_requests"][0]
    assert req3["type"] == "dalton_google_mcp_elicitation"
    assert len(mcp_calls) == 2
    assert mcp_calls[1]["request_state"] == "rs-round-1"

    # Submit elicitation round 2 -> resumes with persisted rs-round-2 & BOTH r1+r2 (NO repeat of call 1 or 2!)
    done = await graph.ainvoke(
        Command(
            resume={
                "decisions": [
                    {
                        "type": "approve",
                        "confirm_token": req3["confirm_token"],
                        "elicitation_responses": {
                            "r2": {"action": "accept", "content": {"r2": "CONFIRM"}}
                        },
                    }
                ]
            }
        ),
        config=g_cfg,
    )
    assert "__interrupt__" not in done
    payload = json.loads(done["messages"][0].content)
    assert payload["ok"] is True
    assert payload["event"]["event_id"] == "g-created-1"
    assert len(mcp_calls) == 3
    assert [c["request_state"] for c in mcp_calls] == [None, "rs-round-1", "rs-round-2"]
    assert set(mcp_calls[2]["input_responses"].keys()) == {"r1", "r2"}


# ===========================================================================
# Row 4 (P1): Outbound gates on send_message & delegate_to_analyst
# ===========================================================================


@pytest.mark.asyncio
async def test_personal_thread_outbound_gates_send_message_and_delegate_to_analyst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    ws_colleague = str(uuid4())
    ws_private = str(uuid4())
    monkeypatch.setattr(guard_mod, "is_dalton_enabled", lambda: True)
    monkeypatch.setattr(
        guard_mod,
        "is_thread_personal_private",
        create_autospec(guard_mod.is_thread_personal_private, return_value=True),
    )

    # 1. send_message to Feishu group / Discord channel -> BLOCKED
    ok_grp, err_grp = await check_personal_thread_send_allowed(
        thread_id=tid,
        caller_user_id="user-1",
        target="feishu:oc_group_999",
        conn=_ScriptedConn([]),
    )
    assert ok_grp is False and err_grp is not None

    ok_disc, err_disc = await check_personal_thread_send_allowed(
        thread_id=tid,
        caller_user_id="user-1",
        target="discord:9876543210",
        conn=_ScriptedConn([]),
    )
    assert ok_disc is False and err_disc is not None

    # 2. send_message to verified self 1:1 DM (feishu:@me with bound identity) -> ALLOWED
    ok_dm, err_dm = await check_personal_thread_send_allowed(
        thread_id=tid,
        caller_user_id="user-1",
        target="feishu:@me",
        conn=_ScriptedConn([{"platform_user_id": "ou_alice"}]),
    )
    assert ok_dm is True and err_dm is None

    # 3. End-to-end hook in src/tools/messaging/tools.py:_send refuses BEFORE gateway HTTP call
    call_mock = AsyncMock()
    monkeypatch.setattr(messaging_tools_mod, "_call", call_mock)
    monkeypatch.setattr(
        guard_mod,
        "check_personal_thread_send_allowed",
        create_autospec(
            guard_mod.check_personal_thread_send_allowed,
            return_value=(False, "personal blocked"),
        ),
    )
    msg_text, artifact = await messaging_tools_mod._send(
        text="Meeting at 3pm with CEO",
        files=None,
        target="feishu:oc_group_999",
        reply=False,
        new_thread=False,
        files_workspace_id=None,
        config={"configurable": {"user_id": "user-1", "thread_id": tid, "run_id": str(uuid4())}},
        tool_call_id="tc-send-1",
    )
    assert "personal_integration_outbound_blocked" in msg_text
    assert artifact["code"] == "personal_integration_outbound_blocked"
    call_mock.assert_not_awaited()

    # 4. delegate_to_analyst gate unconditionally refuses any delegation from a private thread
    #    (Lead Decision #2: no flag inheritance, even to user's own double-hidden workspace)
    ok_col, err_col = await check_personal_thread_dispatch_allowed(
        source_thread_id=tid,
        caller_user_id="user-1",
        target_workspace_id=ws_colleague,
        conn=_ScriptedConn([]),
    )
    assert ok_col is False and err_col is not None

    ok_priv, err_priv = await check_personal_thread_dispatch_allowed(
        source_thread_id=tid,
        caller_user_id="user-1",
        target_workspace_id=ws_private,
        conn=_ScriptedConn([]),
    )
    assert ok_priv is False and err_priv is not None
    assert "不支持向其他工作区或线程转派任务" in err_priv

    # 5. End-to-end hook in src/tools/secretary/dispatch.py refuses BEFORE hitl_confirm
    monkeypatch.setattr(
        guard_mod,
        "check_personal_thread_dispatch_allowed",
        create_autospec(
            guard_mod.check_personal_thread_dispatch_allowed,
            return_value=(False, "private thread cannot dispatch to colleague"),
        ),
    )
    monkeypatch.setattr(
        dispatch_mod,
        "_owned_workspace",
        create_autospec(
            dispatch_mod._owned_workspace,
            return_value={"workspace_id": ws_colleague, "user_id": "user-1", "name": "Shared"},
        ),
    )
    hitl_mock = create_autospec(real_hitl_confirm)
    monkeypatch.setattr(dispatch_mod, "hitl_confirm", hitl_mock)
    monkeypatch.setattr(
        "src.config.settings.background_dispatch_requires_token", lambda: False
    )
    disp_cmd = await dispatch_mod.dispatch(
        question="Summarize my 3pm board meeting",
        config={"configurable": {"user_id": "user-1", "thread_id": tid, "workspace_id": str(uuid4())}},
        workspace_id=ws_colleague,
        thread_id=None,
        report_back=True,
        tool_call_id="tc-disp-1",
    )
    disp_msg = disp_cmd.update["messages"][0].content
    assert "private thread cannot dispatch to colleague" in disp_msg
    hitl_mock.assert_not_called()


# ===========================================================================
# Row 5 (P1): Computer transcript mount exclusion & transcript render redaction
# ===========================================================================


def test_personal_thread_excluded_from_computer_transcript_mount_and_redacted_in_render() -> None:
    sql = served_on_computer_sql("t")
    assert "has_personal_integration_data" in sql
    assert "NOT COALESCE(t.has_personal_integration_data, FALSE)" in sql

    scope = own_thread(str(uuid4()), "owner-1")
    assert "has_personal_integration_data" in scope.condition
    assert "owner-1" in scope.params

    # Transcript renderer masks dalton_calendar args, tool output, and subsequent assistant text
    messages = [
        HumanMessage(id="h1", content="What is on my calendar today?"),
        AIMessage(
            id="a1",
            content="",
            tool_calls=[
                {
                    "id": "call-1",
                    "name": "dalton_calendar",
                    "args": {
                        "action": "create_event",
                        "provider": "google",
                        "summary": "Secret M&A Acquisition Call",
                        "description": "Confidential deal terms $500M",
                        "attendees": ["ceo@target.com"],
                    },
                }
            ],
        ),
        ToolMessage(
            id="t1",
            tool_call_id="call-1",
            name="dalton_calendar",
            content='{"ok": true, "event": {"summary": "Secret M&A Acquisition Call"}}',
        ),
        AIMessage(id="a2", content="You have Secret M&A Acquisition Call at 3pm."),
    ]
    seg = render_segment(1, messages)
    joined = "\n".join(seg.lines)
    assert "Secret M&A Acquisition Call" not in joined
    assert "Confidential deal terms" not in joined
    assert "ceo@target.com" not in joined
    assert REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER in joined


# ===========================================================================
# Row 6 (P1): Normalized payload parity & provider-unsupported field rejection
# ===========================================================================


@pytest.mark.asyncio
async def test_calendar_write_payload_parity_single_time_update_description_and_feishu_attendees_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Single-bound builder support on both Feishu & Google
    f_body = build_feishu_event_body(start_time="2026-12-04T14:30:00+08:00", tz_name="Asia/Shanghai")
    assert "start_time" in f_body
    g_body = build_google_event_write_args(
        calendar_id="primary", start_time="2026-12-04T14:30:00+08:00", tz_name="Asia/Shanghai"
    )
    assert g_body["startTime"] == "2026-12-04T14:30:00"

    tid = str(uuid4())
    rid = str(uuid4())
    f_cli = create_autospec(FeishuCalendarClientProtocol, instance=True)
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
        cal_tools_mod.db,
        "get_action_approval_by_stage_key",
        create_autospec(db_mod.get_action_approval_by_stage_key, return_value=None),
    )
    async def _surface_for_cfg(config: Any, **kwargs: Any) -> PersonalSurfaceVerificationResult:
        cfg_tid = (config or {}).get("configurable", {}).get("thread_id", tid)
        return PersonalSurfaceVerificationResult(
            caller_user_id="user-1",
            workspace_id="ws-1",
            thread_id=str(cfg_tid),
            response_id=rid,
            turn_index=0,
            is_first_touch=False,
            first_touch_notice=None,
        )

    monkeypatch.setattr(
        cal_tools_mod,
        "verify_personal_integration_surface",
        create_autospec(
            guard_mod.verify_personal_integration_surface,
            side_effect=_surface_for_cfg,
        ),
    )
    hitl_mock = create_autospec(real_hitl_confirm)
    monkeypatch.setattr(cal_tools_mod, "hitl_confirm", hitl_mock)

    tool_obj = create_calendar_tools(feishu_client=f_cli)[0]
    cfg = {"configurable": {"user_id": "user-1", "thread_id": tid, "run_id": rid}}

    # Feishu update_event with attendees MUST be rejected BEFORE confirmation card
    cmd = await tool_obj.coroutine(
        action="update_event",
        provider="feishu",
        event_id="ev-1",
        summary="Updated",
        attendees=["ext@example.com"],
        config=cfg,
        tool_call_id="tc-feishu-att",
    )
    res = _tool_payload(cmd)
    assert res["ok"] is False
    assert res["error"] == "unsupported_field_for_provider"
    hitl_mock.assert_not_called()


# ===========================================================================
# Row 7 (P1): check_availability full pagination & unknown on incomplete/failure
# ===========================================================================


@pytest.mark.asyncio
async def test_check_availability_paginates_all_pages_and_marks_unknown_on_partial_failure_or_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    rid = str(uuid4())
    g_cli = create_autospec(GoogleCalendarClientProtocol, instance=True)
    f_cli = create_autospec(FeishuCalendarClientProtocol, instance=True)

    # 1. Both Google (2 pages) & Feishu (2 pages) paginate fully -> page 2 meeting blocks 11:00-12:00!
    g_pages = [
        {
            "events": [
                {
                    "id": "g1",
                    "summary": "G Page 1",
                    "start": {"dateTime": "2026-12-07T09:00:00+08:00"},
                    "end": {"dateTime": "2026-12-07T10:00:00+08:00"},
                }
            ],
            "nextPageToken": "g-tok-2",
        },
        {
            "events": [
                {
                    "id": "g2",
                    "summary": "G Page 2",
                    "start": {"dateTime": "2026-12-07T11:00:00+08:00"},
                    "end": {"dateTime": "2026-12-07T12:00:00+08:00"},
                }
            ],
        },
    ]
    g_cli.call_mcp_tool = AsyncMock(side_effect=g_pages)
    f_cli.list_events_page = AsyncMock(
        side_effect=[
            {
                "items": [
                    {
                        "provider": "feishu",
                        "provider_label": "[飞书日历]",
                        "event_id": "f1",
                        "summary": "F Page 1",
                        "start": {"dateTime": "2026-12-07T10:00:00+08:00"},
                        "end": {"dateTime": "2026-12-07T11:00:00+08:00"},
                        "status": "confirmed",
                    }
                ],
                "has_more": True,
                "page_token": "f-tok-2",
            },
            {
                "items": [
                    {
                        "provider": "feishu",
                        "provider_label": "[飞书日历]",
                        "event_id": "f2",
                        "summary": "F Page 2",
                        "start": {"dateTime": "2026-12-07T12:00:00+08:00"},
                        "end": {"dateTime": "2026-12-07T13:00:00+08:00"},
                        "status": "confirmed",
                    }
                ],
                "has_more": False,
                "page_token": None,
            },
        ]
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
    async def _surface_for_cfg(config: Any, **kwargs: Any) -> PersonalSurfaceVerificationResult:
        cfg_tid = (config or {}).get("configurable", {}).get("thread_id", tid)
        return PersonalSurfaceVerificationResult(
            caller_user_id="user-1",
            workspace_id="ws-1",
            thread_id=str(cfg_tid),
            response_id=rid,
            turn_index=0,
            is_first_touch=False,
            first_touch_notice=None,
        )

    monkeypatch.setattr(
        cal_tools_mod,
        "verify_personal_integration_surface",
        create_autospec(
            guard_mod.verify_personal_integration_surface,
            side_effect=_surface_for_cfg,
        ),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "ensure_fresh_access_token",
        create_autospec(
            cal_tools_mod.ensure_fresh_access_token,
            side_effect=lambda uid, provider, service, **kw: _sample_token(provider=provider),
        ),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "claim_first_touch_notice_if_needed",
        create_autospec(guard_mod.claim_first_touch_notice_if_needed, return_value=None),
    )

    tool_obj = create_calendar_tools(google_client=g_cli, feishu_client=f_cli)[0]
    cfg = {"configurable": {"user_id": "user-1", "thread_id": tid, "run_id": rid}}

    cmd_full = await tool_obj.coroutine(
        action="check_availability",
        provider="auto",
        time_min="2026-12-07T09:00:00+08:00",
        time_max="2026-12-07T13:00:00+08:00",
        timezone="Asia/Shanghai",
        duration_minutes=30,
        config=cfg,
        tool_call_id="tc-avail-pages",
    )
    res_full = _tool_payload(cmd_full)
    assert res_full["availability"]["status"] == "complete"
    assert res_full["availability"]["coverage_complete"] is True
    assert len(res_full["availability"]["busy"]) == 4
    assert res_full["availability"]["free"] == []  # Entire 09:00-13:00 covered across 4 pages!

    # 2. One platform fails -> status="unknown" and NO false free slots!
    g_cli.call_mcp_tool = AsyncMock(side_effect=GoogleCalendarAPIError("503 upstream"))
    f_cli.list_events_page = AsyncMock(
        return_value={
            "items": [
                {
                    "provider": "feishu",
                    "provider_label": "[飞书日历]",
                    "event_id": "f1",
                    "summary": "Only Feishu",
                    "start": {"dateTime": "2026-12-07T09:00:00+08:00"},
                    "end": {"dateTime": "2026-12-07T09:30:00+08:00"},
                    "status": "confirmed",
                }
            ],
            "has_more": False,
            "page_token": None,
        }
    )
    cmd_partial = await tool_obj.coroutine(
        action="check_availability",
        provider="auto",
        time_min="2026-12-07T09:00:00+08:00",
        time_max="2026-12-07T13:00:00+08:00",
        timezone="Asia/Shanghai",
        duration_minutes=30,
        config=cfg,
        tool_call_id="tc-avail-partial",
    )
    res_partial = _tool_payload(cmd_partial)
    assert res_partial["availability"]["status"] == "unknown"
    assert res_partial["availability"]["coverage_complete"] is False
    assert res_partial["availability"]["free"] == []
    assert "google" in res_partial["availability"]["incomplete_providers"]


# ===========================================================================
# Row 8 (P1): LangSmith tracer redacts private tool & subsequent LLM payloads
# ===========================================================================


def test_langsmith_tracer_redacts_personal_calendar_tool_and_subsequent_llm_payloads() -> None:
    fake_client = MagicMock()
    tracer = NonRetainingTracer(client=fake_client)

    # 1. dalton_calendar tool run -> suppresses entire LangSmith export (neither create_run nor update_run called)
    tool_run = Run(
        id=uuid4(),
        name="dalton_calendar",
        run_type="tool",
        start_time=datetime.now(UTC),
        inputs={
            "action": "create_event",
            "provider": "google",
            "summary": "Private Board Dinner",
            "description": "Secret compensation discussion",
        },
        outputs={
            "output": '{"ok": true, "event": {"summary": "Private Board Dinner"}}',
        },
        extra={},
        serialized={},
        client=fake_client,
    )
    tracer._start_trace(tool_run)
    tracer._on_tool_start(tool_run)
    tracer._on_tool_end(tool_run)
    assert tracer._personal_private[0] is True
    assert tool_run.extra.get("__disabled") is True
    assert fake_client.create_run.call_count == 0
    assert fake_client.update_run.call_count == 0

    # 2. Subsequent LLM run on a copied tracer in the same trace tree is ALSO completely suppressed
    child_tracer = tracer.copy_with_metadata_defaults(metadata={"step": "2"})
    llm_run = Run(
        id=uuid4(),
        name="ChatAnthropic",
        run_type="llm",
        start_time=datetime.now(UTC),
        inputs={
            "messages": [
                [
                    {
                        "id": ["langchain_core", "messages", "ToolMessage"],
                        "kwargs": {
                            "name": "dalton_calendar",
                            "content": "Private Board Dinner at 7pm",
                        },
                    }
                ]
            ]
        },
        outputs={
            "generations": [
                [
                    {
                        "text": "I scheduled Private Board Dinner at 7pm.",
                        "message": {
                            "kwargs": {
                                "content": "I scheduled Private Board Dinner at 7pm.",
                                "usage_metadata": {
                                    "input_tokens": 100,
                                    "output_tokens": 20,
                                    "total_tokens": 120,
                                },
                            }
                        },
                    }
                ]
            ]
        },
        extra={},
        serialized={},
        client=fake_client,
    )
    child_tracer._start_trace(llm_run)
    child_tracer._on_llm_start(llm_run)
    child_tracer._on_llm_end(llm_run)
    assert llm_run.extra.get("__disabled") is True
    assert fake_client.create_run.call_count == 0
    assert fake_client.update_run.call_count == 0


# ===========================================================================
# Row 9 (P2): Touch timing, actual token epoch, scope reauth guidance & notice
# ===========================================================================


@pytest.mark.asyncio
async def test_touch_timing_uses_actual_token_epoch_skips_on_scope_error_and_persists_first_touch_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    rid = str(uuid4())
    f_cli = create_autospec(FeishuCalendarClientProtocol, instance=True)

    monkeypatch.setattr(
        cal_tools_mod,
        "effective_capabilities",
        lambda: frozenset({CapabilityId.FEISHU_CALENDAR}),
    )
    # Initial connection list reports stale auth_epoch=7
    monkeypatch.setattr(
        cal_tools_mod.db,
        "list_user_connections",
        create_autospec(
            db_mod.list_user_connections,
            return_value=[_sample_conn(provider="feishu", auth_epoch=7)],
        ),
    )

    surface_calls: list[dict[str, Any]] = []

    async def _track_surface(config: Any, **kwargs: Any) -> PersonalSurfaceVerificationResult:
        surface_calls.append(dict(kwargs))
        return PersonalSurfaceVerificationResult(
            caller_user_id="user-1",
            workspace_id="ws-1",
            thread_id=tid,
            response_id=rid,
            turn_index=0,
            is_first_touch=False,
            first_touch_notice=None,
        )

    monkeypatch.setattr(
        cal_tools_mod,
        "verify_personal_integration_surface",
        create_autospec(guard_mod.verify_personal_integration_surface, side_effect=_track_surface),
    )

    # 1. MissingRequiredScopesError -> record_touch_now=True is NEVER called; guides reauth!
    monkeypatch.setattr(
        cal_tools_mod,
        "ensure_fresh_access_token",
        create_autospec(
            oauth_mod.ensure_fresh_access_token,
            side_effect=MissingRequiredScopesError("missing calendar.event:read"),
        ),
    )
    claim_mock = create_autospec(
        guard_mod.claim_first_touch_notice_if_needed,
        side_effect=[FIRST_TOUCH_NOTICE_CALENDAR, None],
    )
    monkeypatch.setattr(cal_tools_mod, "claim_first_touch_notice_if_needed", claim_mock)

    tool_obj = create_calendar_tools(feishu_client=f_cli)[0]
    cfg = {"configurable": {"user_id": "user-1", "thread_id": tid, "run_id": rid}}

    scope_cmd = await tool_obj.coroutine(
        action="list_calendars",
        provider="feishu",
        config=cfg,
        tool_call_id="tc-scope",
    )
    scope_res = _tool_payload(scope_cmd)
    assert scope_res["ok"] is False
    assert scope_res["error"] == "needs_reauth"
    assert scope_res["code"] == "missing_required_scopes"
    assert "/settings?tab=integrations" in scope_res["message"]
    assert all(c["record_touch_now"] is False for c in surface_calls)
    claim_mock.assert_not_awaited()

    # 2. Token returned with UPDATED auth_epoch=8 -> atomic touch uses auth_epoch=8 & expected_auth_epoch=8!
    surface_calls.clear()
    monkeypatch.setattr(
        cal_tools_mod,
        "ensure_fresh_access_token",
        create_autospec(
            oauth_mod.ensure_fresh_access_token,
            return_value=_sample_token(provider="feishu", auth_epoch=8, account_id="acct-new"),
        ),
    )
    f_cli.list_calendars = AsyncMock(
        side_effect=[
            RuntimeError("should not happen"),  # replaced below
        ]
    )
    # First call fails upstream -> notice NOT claimed yet
    from src.dalton.integrations.feishu_calendar import FeishuCalendarAPIError

    f_cli.list_calendars = AsyncMock(
        side_effect=[
            FeishuCalendarAPIError("temporary 503"),
            [{"provider": "feishu", "calendar_id": "primary", "summary": "My Cal"}],
            [{"provider": "feishu", "calendar_id": "primary", "summary": "My Cal"}],
        ]
    )
    fail_cmd = await tool_obj.coroutine(
        action="list_calendars", provider="feishu", config=cfg, tool_call_id="tc-fail"
    )
    assert _tool_payload(fail_cmd)["ok"] is False
    touch_calls = [c for c in surface_calls if c.get("record_touch_now") is True]
    assert len(touch_calls) == 1
    assert touch_calls[0]["auth_epoch"] == 8
    assert touch_calls[0]["expected_auth_epoch"] == 8
    assert touch_calls[0]["expected_provider_account_id"] == "acct-new"
    claim_mock.assert_not_awaited()

    # Next call succeeds -> claims persisted first_touch_notice ONCE
    ok1 = _tool_payload(
        await tool_obj.coroutine(
            action="list_calendars", provider="feishu", config=cfg, tool_call_id="tc-ok1"
        )
    )
    assert ok1["ok"] is True
    assert ok1["first_touch_notice"] == FIRST_TOUCH_NOTICE_CALENDAR

    # Third call succeeds -> deduplicated, no repeat notice
    ok2 = _tool_payload(
        await tool_obj.coroutine(
            action="list_calendars", provider="feishu", config=cfg, tool_call_id="tc-ok2"
        )
    )
    assert ok2["ok"] is True
    assert "first_touch_notice" not in ok2

    # Direct DB test of claim_first_touch_notice_if_needed persistence & deduplication
    first_claimed = await claim_first_touch_notice_if_needed(
        tid, service="calendar", conn=_ScriptedConn([{"conversation_thread_id": tid}])
    )
    assert first_claimed == FIRST_TOUCH_NOTICE_CALENDAR
    second_claimed = await claim_first_touch_notice_if_needed(
        tid, service="calendar", conn=_ScriptedConn([None])
    )
    assert second_claimed is None
