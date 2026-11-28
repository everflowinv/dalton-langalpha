"""Round 5 unit tests for Personal Calendar Batch 1 Step 3 (read-only narrowing & review fixes).

Covers all 6 mandatory items:
1. Read-only narrowing: tool description & JSON schema omit write actions/params;
   calling create/update/delete returns readable refusal without touching DB/tokens/upstream.
2. LangSmith first-touch buffering: calendar-eligible interactive turns defer export from
   turn start, discard everything when personal calendar is touched, and export after root
   completion when untouched (tested with full start -> end callback lifecycle).
3. Agent-originated turns: `build_graph_config` propagates server `unwatched` verdict
   (including `la` agent turns) into tool config and calendar rejects at entry.
4. Completion notify target parsing: distinguishes personal DM (`feishu:user:`,
   `discord:user:`, `weixin:user:`) vs group (`feishu:chat:`, `discord:channel:`);
   sends body to DM only, sends group notice only after DM succeeds, and supplies a valid
   Feishu Card 2.0 on Feishu group notices so `automation_notifications` never drops `missing_card`.
5. Settings revoke status (backend): `GET /api/v1/dalton/integrations` returns explicit
   terminal purge/revoke statuses in `recent_purges` and serves empty 200 state when a core
   deployment disables all personal capabilities after the last historical disconnect finishes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import ToolMessage
from langchain_core.tracers.schemas import Run
from langgraph.types import Command
from langsmith import Client as LangSmithClient
import pytest

from src.dalton.app import integrations_router as integ_router_mod
from src.dalton.database import automation_notifications as auto_notify_db
from src.dalton.database import user_integrations as db_mod
from src.dalton.database.user_integrations import (
    IntegrationRow,
    IntegrationStatus,
    LocalPurgeStatus,
    PurgeLedgerRow,
    RemoteRevokeStatus,
)
from src.dalton.integrations.feishu_calendar import FeishuCalendarClientProtocol
from src.dalton.integrations.google_calendar import GoogleCalendarClientProtocol
from src.dalton.notify import dispatch as notify_dispatch_mod
from src.dalton.services import personal_integration_guard as guard_mod
from src.dalton.services.personal_integration_guard import (
    PERSONAL_INTEGRATION_GROUP_REDIRECT_NOTICE,
)
from src.dalton.services.user_integrations_oauth import FreshAccessToken
from src.dalton.tools import calendar_tools as cal_tools_mod
from src.dalton.tools.calendar_tools import (
    CALENDAR_WRITE_DISABLED_MESSAGE,
    UNATTENDED_CALENDAR_REFUSAL_MESSAGE,
    create_calendar_tools,
)
from src.observability import langsmith_tracer as tracer_mod
from src.observability.langsmith_tracer import NonRetainingTracer
from src.server.handlers.chat import request_prep as request_prep_mod
from src.server.models.chat import ChatRequest, ThreadOrigin


@pytest.fixture(autouse=True)
def _enable_dalton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DALTON_ENABLED", "1")
    monkeypatch.setenv("DALTON_PROFILE", "everflow")


def _tool_payload(cmd: Command) -> dict[str, Any]:
    msgs = cmd.update["messages"]
    assert len(msgs) == 1
    assert isinstance(msgs[0], ToolMessage)
    return json.loads(str(msgs[0].content))


def _sample_conn(provider: str = "google") -> IntegrationRow:
    now = datetime.now(UTC)
    return IntegrationRow(
        user_id="user-1",
        provider=provider,
        service="calendar",
        oauth_client_id="client-1",
        provider_account_id="acct-1",
        provider_union_id=None,
        provider_email="alice@example.com",
        granted_scopes="openid https://www.googleapis.com/auth/calendar.events",
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
        preferences={},
        has_access_token=True,
        has_refresh_token=True,
        created_at=now,
        updated_at=now,
    )


def _sample_token(provider: str = "google") -> FreshAccessToken:
    return FreshAccessToken(
        access_token="tok-secret",
        provider=provider,
        service="calendar",
        oauth_client_id="client-1",
        provider_account_id="acct-1",
        auth_epoch=1,
        token_generation=1,
        granted_scopes=("https://www.googleapis.com/auth/calendar.events",),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )


# ===========================================================================
# 1. Read-only narrowing: description, schema, and entry refusal
# ===========================================================================


def test_r5_calendar_tool_description_and_schema_are_read_only() -> None:
    tool_obj = create_calendar_tools()[0]
    desc = tool_obj.description or ""
    assert "list_calendars" in desc
    assert "list_events" in desc
    assert "get_event" in desc
    assert "check_availability" in desc
    for forbidden_word in (
        "create_event",
        "update_event",
        "delete_event",
        "新建日程",
        "修改已有日程",
        "删除已有日程",
        "人工确认",
        "确认卡片",
    ):
        assert forbidden_word not in desc

    schema = tool_obj.args_schema.model_json_schema()
    props = schema["properties"]
    assert props["action"]["enum"] == [
        "list_calendars",
        "list_events",
        "get_event",
        "check_availability",
    ]
    for write_param in (
        "summary",
        "description",
        "start_time",
        "end_time",
        "all_day",
        "location",
        "attendees",
        "reminders",
        "use_default_reminders",
    ):
        assert write_param not in props


@pytest.mark.asyncio
@pytest.mark.parametrize("write_action", ["create_event", "update_event", "delete_event"])
async def test_r5_calendar_write_actions_rejected_at_entry_without_external_calls(
    monkeypatch: pytest.MonkeyPatch,
    write_action: str,
) -> None:
    g_cli = create_autospec(GoogleCalendarClientProtocol, instance=True)
    f_cli = create_autospec(FeishuCalendarClientProtocol, instance=True)
    list_conns = create_autospec(db_mod.list_user_connections, return_value=[_sample_conn()])
    token_fn = create_autospec(cal_tools_mod.ensure_fresh_access_token, return_value=_sample_token())
    monkeypatch.setattr(cal_tools_mod.db, "list_user_connections", list_conns)
    monkeypatch.setattr(cal_tools_mod, "ensure_fresh_access_token", token_fn)

    tool_obj = create_calendar_tools(google_client=g_cli, feishu_client=f_cli)[0]
    cfg = {"configurable": {"user_id": "user-1", "thread_id": str(uuid4()), "run_id": str(uuid4())}}

    # Verify direct coroutine invocation
    cmd = await tool_obj.coroutine(
        action=write_action,
        provider="google",
        event_id="ev-1",
        config=cfg,
        tool_call_id=f"tc-{write_action}",
    )
    payload = _tool_payload(cmd)
    assert payload["ok"] is False
    assert payload["error"] == "write_actions_disabled"
    assert payload["message"] == CALENDAR_WRITE_DISABLED_MESSAGE
    assert payload["message"] == "当前版本只支持查看日程和空闲时间"

    # Verify LangChain ToolCall invocation (goes through args_schema parsing too)
    invoked = await tool_obj.ainvoke(
        {
            "name": "dalton_calendar",
            "args": {"action": write_action, "provider": "feishu", "event_id": "ev-2"},
            "id": f"tc-ainvoke-{write_action}",
            "type": "tool_call",
        },
        config=cfg,
    )
    invoked_payload = _tool_payload(invoked)
    assert invoked_payload["ok"] is False
    assert invoked_payload["error"] == "write_actions_disabled"
    assert invoked_payload["message"] == "当前版本只支持查看日程和空闲时间"

    # Zero DB lookups, zero token refreshes, zero upstream calendar API calls
    assert list_conns.call_count == 0
    assert token_fn.call_count == 0
    assert g_cli.call_mcp_tool.call_count == 0
    assert g_cli.list_calendars.call_count == 0
    assert f_cli.create_event.call_count == 0
    assert f_cli.update_event.call_count == 0
    assert f_cli.delete_event.call_count == 0


# ===========================================================================
# 2. LangSmith first-touch buffering (full start -> end callback lifecycle)
# ===========================================================================


def test_r5_langsmith_defers_export_on_calendar_eligible_turn_and_discards_on_first_touch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ls_client = create_autospec(LangSmithClient, instance=True)
    monkeypatch.setattr(
        tracer_mod,
        "_ambient_equivalent",
        lambda: NonRetainingTracer(client=ls_client, project_name="test-proj"),
    )

    req_stub = ChatRequest(messages=[], workspace_id=str(uuid4()))
    cfg = request_prep_mod.build_graph_config(
        thread_id=str(uuid4()),
        user_id="user-1",
        workspace_id=str(uuid4()),
        timezone_str="Asia/Shanghai",
        token_callback=None,
        request=req_stub,
        effective_model="gpt-5",
        extra_configurable={"personal_calendar_tool_loaded": True},
    )
    tracer = next(cb for cb in cfg["callbacks"] if isinstance(cb, NonRetainingTracer))

    root_id = uuid4()
    root_run = Run(
        id=root_id,
        name="LangGraph",
        run_type="chain",
        start_time=datetime.now(UTC),
        inputs={"messages": [{"content": "帮我看看明天和投资人陈总的私密日程 SECRET_FIRST_TURN"}]},
        outputs={"messages": [{"content": "你明天 10:00 有会议"}]},
        extra={},
        serialized={},
        client=ls_client,
    )
    # Full callback lifecycle: root start -> child LLM start (with secret prompt) -> LLM end (calls dalton_calendar) -> tool -> root end
    tracer._start_trace(root_run)
    tracer._on_chain_start(root_run)
    assert ls_client.create_run.call_count == 0

    child_tracer = tracer.copy_with_metadata_defaults(metadata={"langgraph_node": "model"})
    llm_run = Run(
        id=uuid4(),
        parent_run_id=root_id,
        name="ChatOpenAI",
        run_type="llm",
        start_time=datetime.now(UTC),
        inputs={
            "messages": [
                [{"kwargs": {"content": "帮我看看明天和投资人陈总的私密日程 SECRET_FIRST_TURN"}}]
            ]
        },
        outputs={
            "generations": [
                [
                    {
                        "text": "",
                        "message": {
                            "kwargs": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "tc-first-1",
                                        "name": "dalton_calendar",
                                        "args": {"action": "list_events"},
                                    }
                                ],
                            }
                        },
                    }
                ]
            ]
        },
        extra={},
        serialized={},
        client=ls_client,
    )
    child_tracer._start_trace(llm_run)
    child_tracer._on_chat_model_start(llm_run)
    # CRITICAL: Even on the very first model call BEFORE tool call is emitted, create_run MUST NOT fire!
    assert ls_client.create_run.call_count == 0
    assert ls_client.update_run.call_count == 0

    child_tracer._on_llm_end(llm_run)
    child_tracer._end_trace(llm_run)

    cal_run = Run(
        id=uuid4(),
        parent_run_id=root_id,
        name="dalton_calendar",
        run_type="tool",
        start_time=datetime.now(UTC),
        inputs={"action": "list_events", "query": "SECRET_FIRST_TURN"},
        outputs={"output": "明天 10:00 体检 SECRET_RESULT"},
        extra={},
        serialized={},
        client=ls_client,
    )
    child_tracer._start_trace(cal_run)
    child_tracer._on_tool_start(cal_run)
    child_tracer._on_tool_end(cal_run)
    child_tracer._end_trace(cal_run)

    tracer._on_chain_end(root_run)
    tracer._end_trace(root_run)

    # Entire buffered trace discarded on touch: ZERO create_run and ZERO update_run
    assert ls_client.create_run.call_count == 0
    assert ls_client.update_run.call_count == 0


def test_r5_langsmith_defers_then_exports_when_calendar_eligible_turn_does_not_touch_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ls_client = create_autospec(LangSmithClient, instance=True)
    monkeypatch.setattr(
        tracer_mod,
        "_ambient_equivalent",
        lambda: NonRetainingTracer(client=ls_client, project_name="test-proj"),
    )

    req_stub = ChatRequest(messages=[], workspace_id=str(uuid4()))
    cfg = request_prep_mod.build_graph_config(
        thread_id=str(uuid4()),
        user_id="user-1",
        workspace_id=str(uuid4()),
        timezone_str="Asia/Shanghai",
        token_callback=None,
        request=req_stub,
        effective_model="gpt-5",
        extra_configurable={"personal_calendar_tool_loaded": True},
    )
    tracer = next(cb for cb in cfg["callbacks"] if isinstance(cb, NonRetainingTracer))

    root_id = uuid4()
    root_run = Run(
        id=root_id,
        name="LangGraph",
        run_type="chain",
        start_time=datetime.now(UTC),
        inputs={"messages": [{"content": "分析一下 AAPL Q4 财报"}]},
        outputs={"messages": [{"content": "AAPL Q4 营收增长 8%"}]},
        extra={},
        serialized={},
        client=ls_client,
    )
    tracer._start_trace(root_run)
    tracer._on_chain_start(root_run)
    assert ls_client.create_run.call_count == 0

    child_tracer = tracer.copy_with_metadata_defaults(metadata={"langgraph_node": "model"})
    llm_run = Run(
        id=uuid4(),
        parent_run_id=root_id,
        name="ChatOpenAI",
        run_type="llm",
        start_time=datetime.now(UTC),
        inputs={"messages": [[{"kwargs": {"content": "分析一下 AAPL Q4 财报"}}]]},
        outputs={
            "generations": [
                [{"text": "AAPL Q4 营收增长 8%", "message": {"kwargs": {"content": "AAPL Q4 营收增长 8%"}}}]
            ]
        },
        extra={},
        serialized={},
        client=ls_client,
    )
    child_tracer._start_trace(llm_run)
    child_tracer._on_chat_model_start(llm_run)
    child_tracer._on_llm_end(llm_run)
    child_tracer._end_trace(llm_run)
    # Still buffered until root turn ends cleanly
    assert ls_client.create_run.call_count == 0
    assert ls_client.update_run.call_count == 0

    # Root ends without touching calendar -> full trace flushes in order
    tracer._on_chain_end(root_run)
    tracer._end_trace(root_run)
    assert ls_client.create_run.call_count == 2
    assert ls_client.update_run.call_count == 2

    # When calendar tool is NOT loaded, tracer exports immediately on start (no buffering)
    ls_client_unloaded = create_autospec(LangSmithClient, instance=True)
    monkeypatch.setattr(
        tracer_mod,
        "_ambient_equivalent",
        lambda: NonRetainingTracer(client=ls_client_unloaded, project_name="test-proj"),
    )
    cfg_unloaded = request_prep_mod.build_graph_config(
        thread_id=str(uuid4()),
        user_id="user-1",
        workspace_id=str(uuid4()),
        timezone_str="Asia/Shanghai",
        token_callback=None,
        request=req_stub,
        effective_model="gpt-5",
        extra_configurable={"personal_calendar_tool_loaded": False},
    )
    tracer_unloaded = next(
        cb for cb in cfg_unloaded["callbacks"] if isinstance(cb, NonRetainingTracer)
    )
    run_unloaded = Run(
        id=uuid4(),
        name="LangGraph",
        run_type="chain",
        start_time=datetime.now(UTC),
        inputs={"messages": [{"content": "普通查询"}]},
        outputs={},
        extra={},
        serialized={},
        client=ls_client_unloaded,
    )
    tracer_unloaded._start_trace(run_unloaded)
    tracer_unloaded._on_chain_start(run_unloaded)
    assert ls_client_unloaded.create_run.call_count == 1


# ===========================================================================
# 3. Agent-originated turns via `la` propagate unwatched into tool config
# ===========================================================================


@pytest.mark.asyncio
async def test_r5_agent_originated_turn_propagates_unwatched_from_build_graph_config_and_rejects_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    ws_id = str(uuid4())

    # Simulate computer agent initiating a turn in the user's private Home via `la` CLI
    la_request = ChatRequest(
        messages=[],
        workspace_id=ws_id,
        origin=ThreadOrigin(type="agent", agent="cli"),
    )
    assert request_prep_mod.is_unwatched_turn(la_request) is True

    # Construct actual LangGraph config via production `build_graph_config`
    graph_config = request_prep_mod.build_graph_config(
        thread_id=tid,
        user_id="user-1",
        workspace_id=ws_id,
        timezone_str="Asia/Shanghai",
        token_callback=None,
        request=la_request,
        effective_model="gpt-5",
        from_server=False,
    )
    assert graph_config["configurable"].get("unwatched") is True

    g_cli = create_autospec(GoogleCalendarClientProtocol, instance=True)
    f_cli = create_autospec(FeishuCalendarClientProtocol, instance=True)
    list_conns = create_autospec(db_mod.list_user_connections, return_value=[_sample_conn()])
    token_fn = create_autospec(cal_tools_mod.ensure_fresh_access_token, return_value=_sample_token())
    monkeypatch.setattr(cal_tools_mod.db, "list_user_connections", list_conns)
    monkeypatch.setattr(cal_tools_mod, "ensure_fresh_access_token", token_fn)

    tool_obj = create_calendar_tools(google_client=g_cli, feishu_client=f_cli)[0]
    cmd = await tool_obj.coroutine(
        action="list_events",
        provider="google",
        config=graph_config,
        tool_call_id="tc-la-agent",
    )
    payload = _tool_payload(cmd)
    assert payload["ok"] is False
    assert payload["error"] == "unattended_turn_unsupported"
    assert payload["message"] == UNATTENDED_CALENDAR_REFUSAL_MESSAGE
    assert list_conns.call_count == 0
    assert token_fn.call_count == 0
    assert g_cli.call_mcp_tool.call_count == 0

    # Also verify carried_unwatched=True passed explicitly to build_graph_config rejects calendar
    plain_req = ChatRequest(messages=[], workspace_id=ws_id)
    resumed_cfg = request_prep_mod.build_graph_config(
        thread_id=tid,
        user_id="user-1",
        workspace_id=ws_id,
        timezone_str="Asia/Shanghai",
        token_callback=None,
        request=plain_req,
        effective_model="gpt-5",
        unwatched=True,
    )
    assert resumed_cfg["configurable"].get("unwatched") is True
    cmd_resumed = await tool_obj.coroutine(
        action="check_availability",
        provider="google",
        config=resumed_cfg,
        tool_call_id="tc-carried-unwatched",
    )
    assert _tool_payload(cmd_resumed)["error"] == "unattended_turn_unsupported"


# ===========================================================================
# 4. Completion notify target parsing: personal DM vs group + Feishu card
# ===========================================================================


@pytest.mark.asyncio
async def test_r5_notify_distinguishes_personal_dm_and_group_targets_and_gates_group_notice_on_dm_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        guard_mod,
        "is_thread_personal_private",
        create_autospec(guard_mod.is_thread_personal_private, return_value=True),
    )

    async def _always_true(*_a: Any, **_kw: Any) -> bool:
        return True

    async def _noop(*_a: Any, **_kw: Any) -> None:
        return None

    async def _empty_overrides(_uid: str | None) -> dict[str, bool]:
        return {}

    async def _no_verdict(*_a: Any, **_kw: Any) -> None:
        return None

    # (a) Production personal DM targets (feishu:user:, discord:user:, weixin:user:)
    # receive full body directly — NEVER rewritten to bare platform names or treated as groups.
    orig_eval_eligibility = notify_dispatch_mod.evaluate_user_notify_eligibility
    personal_targets = [
        "feishu:user:ou_alice_01",
        "discord:user:1234567890",
        "weixin:user:wx_alice_01",
    ]
    monkeypatch.setattr(
        notify_dispatch_mod,
        "evaluate_user_notify_eligibility",
        create_autospec(
            orig_eval_eligibility,
            return_value=(True, "ok", personal_targets),
        ),
    )
    monkeypatch.setattr(
        notify_dispatch_mod,
        "notify_via_gateway_enabled",
        lambda: False,
    )

    gw_claimed: list[dict[str, Any]] = []

    async def _fake_claim_gw(
        execution_id: str,
        channel: str,
        target: str,
        **kwargs: Any,
    ) -> Any:
        from collections import namedtuple

        ClaimRes = namedtuple("ClaimRes", ["claimed", "delivery_id", "attempt"])
        gw_claimed.append({"channel": channel, "target": target, "payload": kwargs.get("payload")})
        return ClaimRes(True, str(uuid4()), 1)

    async def _fake_post_gw(delivery_id: str, *, target: str) -> dict[str, Any]:
        return {"ok": True, "results": [{"target": target, "ok": True}]}

    monkeypatch.setattr(notify_dispatch_mod, "claim_target_delivery_with_attempt", _fake_claim_gw)
    monkeypatch.setattr(notify_dispatch_mod, "post_automation_gateway_notify", _fake_post_gw)

    posted_a: list[dict[str, Any]] = []

    async def _fake_post_a(targets_arg: Any, text: str, card: Any = None, **_kw: Any) -> dict[str, Any]:
        ts = targets_arg if isinstance(targets_arg, list) else [targets_arg]
        for t in ts:
            posted_a.append({"target": t, "text": text, "card": card})
        return {"ok": True, "results": [{"target": t, "ok": True} for t in ts]}

    ok_a = await notify_dispatch_mod.on_automation_settled(
        automation={"automation_id": str(uuid4()), "user_id": "user-1", "name": "Daily Sync", "delivery_config": {}},
        execution_id=str(uuid4()),
        thread_id=str(uuid4()),
        final_text="【私人日程】14:00 医生复查",
        resolve_enabled_fn=_always_true,
        resolve_overrides_fn=_empty_overrides,
        resolve_targets_fn=lambda _u: _async_val(personal_targets),
        resolve_routes_fn=lambda _u: _async_val({}),
        get_verdict_fn=_no_verdict,
        claim_target_fn=_always_true,
        mark_target_fn=_noop,
        post_notify_fn=_fake_post_a,
        frontend_url_fn=lambda: "https://app.example.com",
    )
    assert ok_a is True
    sent_targets_a = {p["target"] for p in posted_a} | {g["target"] for g in gw_claimed}
    assert sent_targets_a == set(personal_targets)
    assert "feishu" not in sent_targets_a
    assert "discord" not in sent_targets_a
    assert "weixin" not in sent_targets_a
    for entry in posted_a:
        assert "医生复查" in entry["text"]
    assert len(gw_claimed) == 1
    assert "医生复查" in gw_claimed[0]["payload"]["text"]

    # (b) Group targets (`feishu:chat:` and `discord:channel:`): body goes ONLY to personal DM,
    # group receives notice AFTER DM succeeds, and Feishu group notice has a valid Card 2.0
    # that passes `automation_notifications` validation (never dropped with `missing_card`).
    group_and_dm = ["feishu:chat:oc_room_1", "discord:channel:987654321"]
    monkeypatch.setattr(
        notify_dispatch_mod,
        "evaluate_user_notify_eligibility",
        create_autospec(
            orig_eval_eligibility,
            return_value=(True, "ok", group_and_dm),
        ),
    )
    posted_b: list[dict[str, Any]] = []
    claimed_payloads_b: dict[str, dict[str, Any]] = {}

    async def _claim_b(
        _eid: str, _ch: str, tgt: str, **kwargs: Any
    ) -> bool:
        claimed_payloads_b[tgt] = dict(kwargs.get("payload") or {})
        return True

    async def _fake_post_b(targets_arg: Any, text: str, card: Any = None, **_kw: Any) -> dict[str, Any]:
        ts = targets_arg if isinstance(targets_arg, list) else [targets_arg]
        for t in ts:
            posted_b.append({"target": t, "text": text, "card": card})
        return {"ok": True, "results": [{"target": t, "ok": True} for t in ts]}

    ok_b = await notify_dispatch_mod.on_automation_settled(
        automation={"automation_id": str(uuid4()), "user_id": "user-1", "name": "Daily Sync", "delivery_config": {}},
        execution_id=str(uuid4()),
        thread_id=str(uuid4()),
        final_text="【私人日程】16:30 牙医预约",
        resolve_enabled_fn=_always_true,
        resolve_overrides_fn=_empty_overrides,
        resolve_targets_fn=lambda _u: _async_val(["feishu:user:ou_alice_01", "discord:user:1234567890"]),
        resolve_routes_fn=lambda _u: _async_val({}),
        get_verdict_fn=_no_verdict,
        claim_target_fn=_claim_b,
        mark_target_fn=_noop,
        post_notify_fn=_fake_post_b,
        frontend_url_fn=lambda: "https://app.example.com",
    )
    assert ok_b is True
    order_b = [p["target"] for p in posted_b]
    assert order_b.index("feishu:user:ou_alice_01") < order_b.index("feishu:chat:oc_room_1")
    assert order_b.index("discord:user:1234567890") < order_b.index("discord:channel:987654321")

    feishu_group_msg = next(p for p in posted_b if p["target"] == "feishu:chat:oc_room_1")
    assert feishu_group_msg["text"] == PERSONAL_INTEGRATION_GROUP_REDIRECT_NOTICE
    assert isinstance(feishu_group_msg["card"], dict)
    assert feishu_group_msg["card"].get("schema") == "2.0"
    assert "牙医预约" not in json.dumps(feishu_group_msg["card"], ensure_ascii=False)
    # Confirm gateway automation_notifications validator accepts this Feishu group notice card
    parsed_feishu_group = auto_notify_db._parse_target("feishu:chat:oc_room_1")
    assert parsed_feishu_group is not None
    assert parsed_feishu_group["target_kind"] == "group"
    saved_feishu_payload = claimed_payloads_b["feishu:chat:oc_room_1"]
    assert isinstance(saved_feishu_payload.get("card"), dict) and bool(saved_feishu_payload["card"])
    assert auto_notify_db._media_block_code(saved_feishu_payload, saved_feishu_payload["card"]) is None

    discord_group_msg = next(p for p in posted_b if p["target"] == "discord:channel:987654321")
    assert discord_group_msg["text"] == PERSONAL_INTEGRATION_GROUP_REDIRECT_NOTICE
    assert discord_group_msg["card"] is None
    assert "牙医预约" not in discord_group_msg["text"]

    # (c) When personal DM send fails, group MUST NOT receive the redirect notice!
    monkeypatch.setattr(
        notify_dispatch_mod,
        "evaluate_user_notify_eligibility",
        create_autospec(
            orig_eval_eligibility,
            return_value=(True, "ok", ["feishu:chat:oc_room_fail"]),
        ),
    )
    posted_c: list[dict[str, Any]] = []

    async def _fake_post_fail_dm(targets_arg: Any, text: str, card: Any = None, **_kw: Any) -> dict[str, Any]:
        ts = targets_arg if isinstance(targets_arg, list) else [targets_arg]
        for t in ts:
            posted_c.append({"target": t, "text": text, "card": card})
        return {"ok": False, "results": [{"target": t, "ok": False, "error": "dm_delivery_failed"} for t in ts]}

    ok_c = await notify_dispatch_mod.on_automation_settled(
        automation={"automation_id": str(uuid4()), "user_id": "user-1", "name": "Daily Sync", "delivery_config": {}},
        execution_id=str(uuid4()),
        thread_id=str(uuid4()),
        final_text="【私人日程】18:00 体检报告",
        resolve_enabled_fn=_always_true,
        resolve_overrides_fn=_empty_overrides,
        resolve_targets_fn=lambda _u: _async_val(["feishu:user:ou_alice_01"]),
        resolve_routes_fn=lambda _u: _async_val({}),
        get_verdict_fn=_no_verdict,
        claim_target_fn=_always_true,
        mark_target_fn=_noop,
        post_notify_fn=_fake_post_fail_dm,
        frontend_url_fn=lambda: "https://app.example.com",
    )
    assert ok_c is False
    assert [p["target"] for p in posted_c] == ["feishu:user:ou_alice_01"]


async def _async_val(val: Any) -> Any:
    return val


# ===========================================================================
# 5. Settings revoke status (backend): explicit recent_purges + empty 200 state
# ===========================================================================


def test_r5_integrations_list_returns_recent_purges_and_empty_state_after_last_historical_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Core profile with ALL personal integration capabilities disabled
    monkeypatch.setenv("DALTON_PROFILE", "core")
    monkeypatch.setenv("DALTON_CAPABILITIES", "")

    now = datetime.now(UTC)
    superseded_purge = PurgeLedgerRow(
        purge_id="purge-sup-1",
        user_id="user-1",
        provider="google",
        service="calendar",
        oauth_client_id="client-1",
        provider_account_id="acct-1",
        auth_epoch_upto=1,
        trigger_reason="user_disconnect",
        local_purge_status=LocalPurgeStatus.COMPLETED,
        purged_max_turn_by_thread={},
        tracker_reconcile_needed=False,
        local_completed_at=now,
        remote_revoke_status=RemoteRevokeStatus.SUPERSEDED,
        has_revoke_token=False,
        retry_count=0,
        next_retry_at=None,
        last_remote_error=None,
        remote_completed_at=now,
        disconnected_at=now - timedelta(seconds=10),
        completed_at=now,
    )

    monkeypatch.setattr(
        db_mod,
        "list_user_connections",
        create_autospec(db_mod.list_user_connections, return_value=[]),
    )
    monkeypatch.setattr(
        db_mod,
        "list_user_purge_ledgers",
        create_autospec(db_mod.list_user_purge_ledgers, return_value=[superseded_purge]),
    )

    app = FastAPI()
    app.include_router(integ_router_mod.router)
    app.dependency_overrides[integ_router_mod.require_integration_web_session] = lambda: (
        integ_router_mod.IntegrationSessionContext(
            user_id="user-1",
            user_email="alice@example.com",
            auth_mode="local",
            session_id="sess-1",
        )
    )
    client = TestClient(app)

    resp = client.get("/api/v1/dalton/integrations")
    assert resp.status_code == 200
    body = resp.json()
    assert body["connections"] == []
    assert body["active_purges"] == []
    assert body["has_personal_integrations"] is False
    assert len(body["recent_purges"]) == 1
    assert body["recent_purges"][0]["purge_id"] == "purge-sup-1"
    assert body["recent_purges"][0]["remote_revoke_status"] == "superseded"
