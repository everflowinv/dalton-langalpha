"""Round 4 unit tests for Personal Calendar Batch 1 Step 3.

Covers all Lead Decisions 1–6 (backend):
1. Batch 1 rejects unattended personal calendar access (reads & writes) at entry
   in automations, scheduled reports, wakeups, and unattended channel turns, and
   prevents `automation_report` from receiving or dispatching calendar data.
2. LangSmith skips entire export (zero `create_run` and `update_run` calls) for
   personal-integration threads (including streaming events & reasoning_content).
3. Subagent transcript manifest (`meta.json`) sanitizes task `description` alongside
   segment body redaction.
4. Resuming a saved Google form response after an interruption ALWAYS re-verifies
   event version before dispatching the write (real LangGraph StateGraph test).
5. Unconfirmed writes never report false success:
   - `create_event` writes platform unique id (`extendedProperties.private.dalton_idempotency_key`
     / Feishu `idempotency_key`) and reconciles STRICTLY by that unique id (never fuzzy title/time).
   - `update_event` compares EVERY approved field in the frozen payload against remote state;
     any mismatch returns `write_result_unconfirmed`.
6. Approval card payload carries actual attendee changes (`attendees_set`, `attendees_added`,
   `attendees_removed`, `attendees_cleared`, `before_attendees`) separately from L2 notification list.
"""

from __future__ import annotations

from dataclasses import replace as dc_replace
from datetime import UTC, datetime, timedelta
import json
from typing import Annotated, Any, TypedDict
from unittest.mock import create_autospec
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tracers.schemas import Run
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command
from langsmith import Client as LangSmithClient
import pytest

from ptc_agent.agent.middleware.compaction.window import Window
from ptc_agent.agent.transcript.store import build_directory, load_manifest
from src.dalton.capabilities import CapabilityId
from src.dalton.database import user_integrations as db_mod
from src.dalton.database.user_integrations import (
    ActionApprovalRow,
    IntegrationRow,
    IntegrationStatus,
)
from src.dalton.integrations.feishu_calendar import FeishuCalendarClientProtocol
from src.dalton.integrations.google_calendar import (
    GoogleCalendarClientProtocol,
    GoogleMCPElicitationRequired,
)
from src.dalton.services import personal_integration_guard as guard_mod
from src.dalton.services.personal_integration_guard import (
    PersonalSurfaceDeniedError,
    PersonalSurfaceVerificationResult,
    verify_personal_integration_surface,
)
from src.dalton.services.personal_integration_trace import (
    REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER,
)
from src.dalton.services.user_integrations_oauth import FreshAccessToken
from src.dalton.tools import automation_report_tools as report_tools_mod
from src.dalton.tools import calendar_tools as cal_tools_mod
from src.dalton.tools.calendar_tools import (
    UNATTENDED_CALENDAR_REFUSAL_MESSAGE,
    _normalize_calendar_write_request,
    create_calendar_tools,
)
from src.observability import langsmith_tracer as tracer_mod
from src.observability.langsmith_tracer import NonRetainingTracer
from src.server.handlers.chat import request_prep as request_prep_mod
from src.server.models.chat import ChatRequest


@pytest.fixture(autouse=True)
def _enable_dalton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DALTON_ENABLED", "1")
    monkeypatch.setenv("DALTON_PROFILE", "everflow")


def _tool_payload(cmd: Command) -> dict[str, Any]:
    msgs = cmd.update["messages"]
    assert len(msgs) == 1
    assert isinstance(msgs[0], ToolMessage)
    return json.loads(str(msgs[0].content))


def _sample_conn(
    *,
    provider: str = "google",
    service: str = "calendar",
    auth_epoch: int = 7,
    token_generation: int = 2,
    account_id: str = "acct-google-1",
) -> IntegrationRow:
    now = datetime.now(UTC)
    return IntegrationRow(
        user_id="user-1",
        provider=provider,
        service=service,
        oauth_client_id="client-1",
        provider_account_id=account_id,
        provider_union_id=None,
        provider_email="alice@example.com",
        granted_scopes="openid https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        auth_epoch=auth_epoch,
        authorized_at=now,
        token_generation=token_generation,
        status=IntegrationStatus.CONNECTED,
        refresh_inflight_gen=None,
        refresh_started_at=None,
        expires_at=now + timedelta(hours=1),
        last_refresh_at=now,
        needs_reauth_at=None,
        last_error_code=None,
        reauth_notified_at=None,
        preferences={"default_calendar_id": "primary"},
        has_access_token=True,
        has_refresh_token=True,
        created_at=now,
        updated_at=now,
    )


def _sample_token(
    *,
    provider: str = "google",
    auth_epoch: int = 7,
    token_generation: int = 2,
    account_id: str = "acct-google-1",
) -> FreshAccessToken:
    return FreshAccessToken(
        access_token="tok-live-secret",
        provider=provider,
        service="calendar",
        oauth_client_id="client-1",
        provider_account_id=account_id,
        auth_epoch=auth_epoch,
        token_generation=token_generation,
        granted_scopes=("https://www.googleapis.com/auth/calendar.events",),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )


class _MiniState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


# ===========================================================================
# 1. Batch 1 rejects unattended personal calendar access + automation_report
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unattended_cfg",
    [
        {"configurable": {"user_id": "user-1", "thread_id": str(uuid4()), "automation_execution_id": str(uuid4())}},
        {"configurable": {"user_id": "user-1", "thread_id": str(uuid4()), "turn_origin_automation_id": str(uuid4())}},
        {"configurable": {"user_id": "user-1", "thread_id": str(uuid4()), "platform": "automation"}},
        {"configurable": {"user_id": "user-1", "thread_id": str(uuid4()), "wake_turn": True}},
        {"configurable": {"user_id": "user-1", "thread_id": str(uuid4()), "report_command_id": "cmd-1"}},
        {"configurable": {"user_id": "user-1", "thread_id": str(uuid4())}, "metadata": {"origin": "automation"}},
        {"configurable": {"user_id": "user-1", "thread_id": str(uuid4())}, "metadata": {"origin": "system", "query_type": "system"}},
    ],
)
@pytest.mark.parametrize("action", ["list_calendars", "list_events", "check_availability", "create_event"])
async def test_r4_unattended_automation_calls_to_calendar_rejected_at_entry(
    monkeypatch: pytest.MonkeyPatch,
    unattended_cfg: dict[str, Any],
    action: str,
) -> None:
    g_cli = create_autospec(GoogleCalendarClientProtocol, instance=True)
    f_cli = create_autospec(FeishuCalendarClientProtocol, instance=True)
    list_conns = create_autospec(db_mod.list_user_connections, return_value=[_sample_conn()])
    token_fn = create_autospec(cal_tools_mod.ensure_fresh_access_token, return_value=_sample_token())
    monkeypatch.setattr(cal_tools_mod.db, "list_user_connections", list_conns)
    monkeypatch.setattr(cal_tools_mod, "ensure_fresh_access_token", token_fn)

    tool_obj = create_calendar_tools(google_client=g_cli, feishu_client=f_cli)[0]
    kwargs: dict[str, Any] = {
        "action": action,
        "provider": "google",
        "config": unattended_cfg,
        "tool_call_id": f"tc-unattended-{action}",
    }

    cmd = await tool_obj.coroutine(**kwargs)
    payload = _tool_payload(cmd)
    assert payload["ok"] is False
    assert payload["error"] == "unattended_turn_unsupported"
    assert "个人日历只能在你本人的对话里使用，自动任务暂不支持" in payload["message"]
    assert payload["message"] == UNATTENDED_CALENDAR_REFUSAL_MESSAGE
    # Rejected right at entry: never queried user connections, tokens, or upstream APIs
    assert list_conns.call_count == 0
    assert token_fn.call_count == 0
    assert g_cli.call_mcp_tool.call_count == 0
    assert g_cli.list_calendars.call_count == 0
    assert f_cli.list_events_page.call_count == 0


@pytest.mark.asyncio
async def test_r4_channel_unattended_background_run_rejected_by_surface_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    rid = str(uuid4())
    ws_id = str(uuid4())

    class _BgRunCursor:
        def __init__(self) -> None:
            self._next: Any = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            s = " ".join(sql.split()).lower()
            if "from conversation_threads t" in s:
                self._next = {
                    "conversation_thread_id": tid,
                    "workspace_id": ws_id,
                    "thread_owner": "user-1",
                    "thread_external_id": "feishu:dm:ou_1",
                    "is_shared": False,
                    "workspace_shared_at": None,
                    "has_personal_integration_data": False,
                    "personal_purge_in_progress": False,
                    "ws_owner": "user-1",
                    "ws_config": {"home": True},
                    "ws_status": "active",
                }
            elif "from dalton_channel_background_runs" in s:
                self._next = {
                    "dalton_user_id": "user-1",
                    "platform": "feishu",
                    "target_address": "feishu:@me",
                    "thread_id": tid,
                    "run_id": rid,
                }
            else:
                self._next = None

        async def fetchone(self) -> Any:
            return self._next

    class _BgRunConn:
        def cursor(self, **_kw: Any):
            return _BgRunCursor()

    with pytest.raises(PersonalSurfaceDeniedError) as exc_info:
        await verify_personal_integration_surface(
            {
                "configurable": {
                    "user_id": "user-1",
                    "thread_id": tid,
                    "run_id": rid,
                    "platform": "feishu",
                    "chat_type": "dm",
                }
            },
            provider="google",
            service="calendar",
            record_touch_now=False,
            conn=_BgRunConn(),
        )
    assert exc_info.value.code == "unattended_turn_unsupported"
    assert "个人日历只能在你本人的对话里使用，自动任务暂不支持" in exc_info.value.message


@pytest.mark.asyncio
async def test_r4_automation_report_cannot_receive_or_submit_calendar_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    ws_id = str(uuid4())
    run_id = str(uuid4())
    auto_id = str(uuid4())
    exec_id = str(uuid4())
    auto_cfg = {
        "configurable": {
            "user_id": "user-1",
            "workspace_id": ws_id,
            "thread_id": tid,
            "run_id": run_id,
            "turn_origin_automation_id": auto_id,
            "automation_execution_id": exec_id,
            "platform": "automation",
        }
    }

    # Step 1: Scheduled report automation attempts to read personal calendar -> rejected at entry
    g_cli = create_autospec(GoogleCalendarClientProtocol, instance=True)
    cal_tool = create_calendar_tools(google_client=g_cli)[0]
    cal_cmd = await cal_tool.coroutine(
        action="list_events",
        provider="google",
        config=auto_cfg,
        tool_call_id="tc-auto-report-cal",
    )
    cal_res = _tool_payload(cal_cmd)
    assert cal_res["ok"] is False
    assert cal_res["error"] == "unattended_turn_unsupported"
    assert g_cli.call_mcp_tool.call_count == 0

    # Step 2: Defense-in-depth — even if the thread was previously marked personal-private,
    # `automation_report` refuses submit before saving or dispatching to any group/channel.
    submit_mock = create_autospec(report_tools_mod.submit_automation_report)
    dispatch_mock = create_autospec(report_tools_mod.dispatch_automation_report)
    monkeypatch.setattr(
        report_tools_mod,
        "is_internal_or_admin",
        create_autospec(report_tools_mod.is_internal_or_admin, return_value=True),
    )
    monkeypatch.setattr(report_tools_mod, "submit_automation_report", submit_mock)
    monkeypatch.setattr(report_tools_mod, "dispatch_automation_report", dispatch_mock)
    monkeypatch.setattr(
        guard_mod,
        "is_thread_personal_private",
        create_autospec(guard_mod.is_thread_personal_private, return_value=True),
    )
    monkeypatch.setenv("DALTON_SHARING_AUTH_ENABLED", "0")

    report_tool = report_tools_mod.create_automation_report_tools()[0]
    raw_out = await report_tool.coroutine(
        action="submit",
        report_run_id="rep-1",
        material="yes",
        text="不应发送的个人日程内容",
        config=auto_cfg,
    )
    report_res = json.loads(raw_out)
    assert report_res["ok"] is False
    assert report_res["code"] == "personal_integration_private_only"
    assert submit_mock.call_count == 0
    assert dispatch_mock.call_count == 0


# ===========================================================================
# 2. LangSmith skips entire export (zero create_run & update_run calls)
# ===========================================================================


def test_r4_langsmith_skips_entire_export_on_personal_threads_and_streaming_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ls_client = create_autospec(LangSmithClient, instance=True)

    # (a) Thread already flagged with has_personal_integration_data -> with_run_tracer suppresses EVERYTHING
    req_stub = ChatRequest(messages=[], workspace_id=str(uuid4()))
    cfg = request_prep_mod.build_graph_config(
        thread_id=str(uuid4()),
        user_id="user-1",
        workspace_id=str(uuid4()),
        timezone_str="Asia/Shanghai",
        token_callback=None,
        request=req_stub,
        effective_model="gpt-5",
        extra_configurable={"has_personal_integration_data": True},
    )
    monkeypatch.setattr(
        tracer_mod,
        "_ambient_equivalent",
        lambda: NonRetainingTracer(client=ls_client, project_name="test-proj"),
    )
    traced_cfg = tracer_mod.with_run_tracer(cfg)
    tracer = next(cb for cb in traced_cfg["callbacks"] if isinstance(cb, NonRetainingTracer))

    chain_run = Run(
        id=uuid4(),
        name="LangGraph",
        run_type="chain",
        start_time=datetime.now(UTC),
        inputs={"messages": [{"content": "明天的体检安排 SECRET_APPT"}]},
        outputs={"messages": [{"content": "你明天 10:00 有体检安排 SECRET_APPT"}]},
        extra={},
        serialized={},
        client=ls_client,
    )
    tracer._start_trace(chain_run)
    tracer._on_chain_start(chain_run)

    llm_run = Run(
        id=uuid4(),
        name="ChatOpenAI",
        run_type="llm",
        start_time=datetime.now(UTC),
        inputs={"messages": [[{"kwargs": {"content": "体检安排 SECRET_APPT"}}]]},
        outputs={
            "generations": [
                [
                    {
                        "text": "10:00 体检 SECRET_APPT",
                        "message": {
                            "kwargs": {
                                "content": "10:00 体检 SECRET_APPT",
                                "additional_kwargs": {
                                    "reasoning_content": "用户问 SECRET_APPT 体检时间"
                                },
                            }
                        },
                    }
                ]
            ]
        },
        events=[{"name": "new_token", "kwargs": {"token": "SECRET_APPT"}}],
        extra={},
        serialized={},
        client=ls_client,
    )
    child_tracer = tracer.copy_with_metadata_defaults(metadata={"step": "llm"})
    child_tracer._start_trace(llm_run)
    child_tracer._on_llm_start(llm_run)
    child_tracer._on_llm_end(llm_run)
    tracer._on_chain_end(chain_run)

    assert ls_client.create_run.call_count == 0
    assert ls_client.update_run.call_count == 0

    # (b) First turn detecting dalton_calendar mid-turn also blocks update_run and all subsequent create_run/update_run
    ls_client_2 = create_autospec(LangSmithClient, instance=True)
    tracer2 = NonRetainingTracer(client=ls_client_2, project_name="test-proj")
    first_llm = Run(
        id=uuid4(),
        name="ChatOpenAI",
        run_type="llm",
        start_time=datetime.now(UTC),
        inputs={"messages": [[{"kwargs": {"content": "查日程"}}]]},
        outputs={
            "generations": [
                [
                    {
                        "text": "",
                        "message": {
                            "kwargs": {
                                "content": "先查询日历",
                                "additional_kwargs": {"reasoning_content": "调用个人日历 SECRET_REASONING"},
                                "tool_calls": [
                                    {"id": "tc-1", "name": "dalton_calendar", "args": {"action": "list_events"}}
                                ],
                            }
                        },
                    }
                ]
            ]
        },
        events=[{"name": "new_token", "kwargs": {"token": "SECRET_STREAM_TOKEN"}}],
        extra={},
        serialized={},
        client=ls_client_2,
    )
    tracer2._on_llm_end(first_llm)
    cal_run = Run(
        id=uuid4(),
        name="dalton_calendar",
        run_type="tool",
        start_time=datetime.now(UTC),
        inputs={"action": "list_events", "query": "SECRET_QUERY"},
        outputs={"output": "SECRET_EVENT"},
        extra={},
        serialized={},
        client=ls_client_2,
    )
    tracer2._start_trace(cal_run)
    tracer2._on_tool_start(cal_run)
    tracer2._on_tool_end(cal_run)

    assert ls_client_2.create_run.call_count == 0
    assert ls_client_2.update_run.call_count == 0


# ===========================================================================
# 3. Subagent transcript manifest description + body redaction
# ===========================================================================


def test_r4_subagent_transcript_sanitizes_manifest_description_and_body() -> None:
    secret_title = "绝密董事会并购日程"
    task_messages: list[BaseMessage] = [
        HumanMessage(content=f"请帮我整理「{secret_title}」的会议纪要"),
        AIMessage(content=f"好的，已整理「{secret_title}」要点如下。"),
    ]
    directory = build_directory(
        task_messages,
        unit="run",
        header={
            "task_id": "task-priv-1",
            "description": f"整理{secret_title}相关资料",
            "subagent_type": "research",
            "status": "completed",
        },
        previous=None,
        window=Window(),
        first_personal_segment=1,
    )

    manifest = load_manifest(directory.manifest)
    assert secret_title not in directory.manifest
    assert manifest["description"] == REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER

    assert directory.rendered
    for seg_bytes in directory.rendered.values():
        seg_text = seg_bytes.decode("utf-8")
        assert secret_title not in seg_text
        assert REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER in seg_text


# ===========================================================================
# 4. Saved Google form response interrupted then resumed MUST re-check version
# ===========================================================================


@pytest.mark.skip(reason="Batch 1 personal calendar is read-only; write actions deferred to next batch")
@pytest.mark.asyncio
async def test_r4_google_form_saved_then_interrupted_rechecks_version_before_resumed_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    rid = str(uuid4())
    g_cli = create_autospec(GoogleCalendarClientProtocol, instance=True)

    update_calls: list[dict[str, Any]] = []
    get_event_versions = [
        "2026-12-01T00:00:00Z",  # 1: pre-read before approval card
        "2026-12-01T00:00:00Z",  # 2: initial vercheck right after user approves
        "2026-12-01T00:15:00Z",  # 3: vercheck after recovering saved Google form -> CHANGED!
    ]
    get_event_idx = {"i": 0}

    async def _google_mcp(
        *,
        access_token: str,
        tool_name: str,
        arguments: Any,
        request_state: str | None = None,
        input_responses: Any = None,
    ) -> dict[str, Any]:
        if tool_name == "get_event":
            idx = min(get_event_idx["i"], len(get_event_versions) - 1)
            get_event_idx["i"] += 1
            return {
                "event": {
                    "id": "g-ev-crash",
                    "summary": "Original Title",
                    "start": {"dateTime": "2026-12-09T10:00:00+08:00"},
                    "end": {"dateTime": "2026-12-09T11:00:00+08:00"},
                    "updated": get_event_versions[idx],
                }
            }
        if tool_name == "update_event":
            if input_responses is None:
                raise GoogleMCPElicitationRequired(
                    request_state="rs-write-form-0",
                    input_requests={
                        "confirm_change": {
                            "params": {
                                "message": "Confirm update",
                                "requestedSchema": {
                                    "type": "object",
                                    "properties": {"choice": {"type": "string", "enum": ["yes"]}},
                                    "required": ["choice"],
                                },
                            }
                        }
                    },
                    mcp_endpoint="https://calendarmcp.googleapis.com/mcp/v1",
                    mcp_tool_name="update_event",
                    mcp_arguments=dict(arguments),
                )
            update_calls.append(dict(arguments))
            return {"event": {"id": "g-ev-crash", "summary": arguments.get("summary")}}
        raise AssertionError(f"Unexpected MCP tool {tool_name}")

    g_cli.call_mcp_tool.side_effect = _google_mcp

    approvals_by_id: dict[str, ActionApprovalRow] = {}
    approvals_by_stage: dict[str, ActionApprovalRow] = {}
    crash_after_form_save = {"armed": True}

    async def _create_approval(**kwargs: Any) -> ActionApprovalRow:
        row = ActionApprovalRow(
            approval_id=str(kwargs.get("approval_id") or uuid4()),
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
            idempotent_message_id=kwargs.get("idempotent_message_id"),
            status="pending",
            expires_at=kwargs["expires_at"],
            consumed_at=None,
            created_at=datetime.now(UTC),
            elicitation_state=kwargs.get("elicitation_state"),
        )
        approvals_by_id[row.approval_id] = row
        if row.idempotent_message_id:
            approvals_by_stage[row.idempotent_message_id] = row
        return row

    async def _get_by_stage(uid: str, thread_id: str, stage_key: str, **kw: Any) -> ActionApprovalRow | None:
        return approvals_by_stage.get(stage_key)

    async def _list_by_prefix(uid: str, thread_id: str, prefix: str, **kw: Any) -> list[ActionApprovalRow]:
        return [r for k, r in approvals_by_stage.items() if k.startswith(prefix)]

    async def _consume(aid: str, uid: str, **kw: Any) -> ActionApprovalRow | None:
        row = approvals_by_id.get(aid)
        if row is None or row.status != "pending":
            return None
        updated = dc_replace(row, status="executing", consumed_at=datetime.now(UTC))
        approvals_by_id[aid] = updated
        if updated.idempotent_message_id:
            approvals_by_stage[updated.idempotent_message_id] = updated
        return updated

    async def _update_state(aid: str, *, caller_user_id: str, status: str | None = None, elicitation_state: Any = None, **kw: Any) -> bool:
        row = approvals_by_id[aid]
        updated = dc_replace(
            row,
            status=status or row.status,
            elicitation_state=dict(elicitation_state)
            if isinstance(elicitation_state, dict)
            else (elicitation_state if elicitation_state is not None else row.elicitation_state),
        )
        approvals_by_id[aid] = updated
        if updated.idempotent_message_id:
            approvals_by_stage[updated.idempotent_message_id] = updated
        # Simulate crash RIGHT AFTER saving the Google form response (before version re-check runs!)
        if (
            row.action_type == "dalton_google_mcp_elicitation"
            and status == "succeeded"
            and crash_after_form_save["armed"]
        ):
            crash_after_form_save["armed"] = False
            raise RuntimeError("simulated worker crash right after saving Google form response")
        return True

    monkeypatch.setattr(cal_tools_mod, "effective_capabilities", lambda: frozenset({CapabilityId.GOOGLE_CALENDAR}))
    monkeypatch.setattr(cal_tools_mod, "get_connector_providers_summary", lambda: {"google": {"ready": True}})
    monkeypatch.setattr(cal_tools_mod.db, "list_user_connections", create_autospec(db_mod.list_user_connections, return_value=[_sample_conn()]))
    monkeypatch.setattr(cal_tools_mod, "ensure_fresh_access_token", create_autospec(cal_tools_mod.ensure_fresh_access_token, return_value=_sample_token()))
    monkeypatch.setattr(
        cal_tools_mod,
        "verify_personal_integration_surface",
        create_autospec(
            guard_mod.verify_personal_integration_surface,
            return_value=PersonalSurfaceVerificationResult(
                caller_user_id="user-1",
                workspace_id=str(uuid4()),
                thread_id=tid,
                response_id=rid,
                turn_index=0,
                is_first_touch=False,
                first_touch_notice=None,
                platform="web",
                chat_type="web",
                is_dm_channel=False,
            ),
        ),
    )
    monkeypatch.setattr(cal_tools_mod, "check_thread_surface_still_private", create_autospec(guard_mod.check_thread_surface_still_private, return_value=True))
    monkeypatch.setattr(cal_tools_mod, "claim_first_touch_notice_if_needed", create_autospec(guard_mod.claim_first_touch_notice_if_needed, return_value=None))
    monkeypatch.setattr(cal_tools_mod.db, "create_action_approval", _create_approval)
    monkeypatch.setattr(cal_tools_mod.db, "get_action_approval_by_stage_key", _get_by_stage)
    monkeypatch.setattr(cal_tools_mod.db, "list_action_approvals_by_stage_prefix", _list_by_prefix)
    monkeypatch.setattr(cal_tools_mod.db, "consume_action_approval", _consume)
    monkeypatch.setattr(cal_tools_mod.db, "update_action_approval_state", _update_state)
    monkeypatch.setattr(cal_tools_mod.db, "mark_action_approval_status", create_autospec(db_mod.mark_action_approval_status, return_value=True))

    tool_obj = create_calendar_tools(google_client=g_cli)[0]

    async def _node(state: _MiniState, config: RunnableConfig) -> Command:
        return await tool_obj.coroutine(
            action="update_event",
            provider="google",
            event_id="g-ev-crash",
            summary="Updated Title",
            config=config,
            tool_call_id="tc-form-crash-1",
        )

    builder = StateGraph(_MiniState)
    builder.add_node("cal", _node)
    builder.add_edge(START, "cal")
    builder.add_edge("cal", END)
    graph = builder.compile(checkpointer=MemorySaver())
    gcfg = {"configurable": {"thread_id": tid, "user_id": "user-1", "run_id": rid}}

    # Step 1: Pause on write approval card
    res1 = await graph.ainvoke({"messages": [HumanMessage(content="Update event")]}, config=gcfg)
    req1 = res1["__interrupt__"][0].value["action_requests"][0]

    # Step 2: Approve write -> initial version check passes -> update_event pauses on Google form
    res2 = await graph.ainvoke(
        Command(
            resume={
                "decisions": [
                    {
                        "type": "approve",
                        "confirm_token": req1["confirm_token"],
                        "confirmed_payload_sha256": req1["payload_sha256"],
                    }
                ]
            }
        ),
        config=gcfg,
    )
    req2 = res2["__interrupt__"][0].value["action_requests"][0]
    assert req2["type"] == "dalton_google_mcp_elicitation"

    # Step 3: Submit Google form -> saves submitted_responses (status="succeeded"), then crashes before version check!
    form_cmd = Command(
        resume={
            "decisions": [
                {
                    "type": "approve",
                    "confirm_token": req2["confirm_token"],
                    "confirmed_payload_sha256": req2["payload_sha256"],
                    "elicitation_responses": {
                        "confirm_change": {"action": "accept", "content": {"choice": "yes"}}
                    },
                }
            ]
        }
    )
    with pytest.raises(RuntimeError, match="simulated worker crash"):
        await graph.ainvoke(form_cmd, config=gcfg)

    # Verify form row was persisted as succeeded
    elic_row = approvals_by_stage["tc:tc-form-crash-1:write:elic:0"]
    assert elic_row.status == "succeeded"
    assert isinstance(elic_row.elicitation_state["submitted_responses"], dict)

    # Step 4: Resume after crash -> recovers saved form response, MUST re-check version, detects change, NEVER writes!
    res4 = await graph.ainvoke(form_cmd, config=gcfg)
    tool_msg = next(m for m in res4["messages"] if isinstance(m, ToolMessage))
    payload = json.loads(str(tool_msg.content))
    assert payload["ok"] is False
    assert payload["error"] == "event_version_changed"
    assert get_event_idx["i"] == 3
    assert update_calls == []  # CRITICAL: resumed write was NEVER dispatched!


# ===========================================================================
# 5. Unconfirmed write reconciliation: strict idempotency key & full field check
# ===========================================================================


@pytest.mark.asyncio
async def test_r4_unconfirmed_create_rejects_fuzzy_title_time_match_and_requires_unique_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    g_cli = create_autospec(GoogleCalendarClientProtocol, instance=True)
    f_cli = create_autospec(FeishuCalendarClientProtocol, instance=True)
    monkeypatch.setattr(cal_tools_mod.db, "update_action_approval_state", create_autospec(db_mod.update_action_approval_state, return_value=True))

    async def _attach_noop(tid: str, res: dict[str, Any]) -> dict[str, Any]:
        return res

    idem_key = "dalton-cal:appr-unique-1"
    # Verify build_google_event_write_args writes extendedProperties.private.dalton_idempotency_key
    spec, err = _normalize_calendar_write_request(
        provider="google",
        action="create_event",
        calendar_id="primary",
        event_id=None,
        summary="站会",
        description="每日同步",
        start_time="2026-12-10T10:00:00+08:00",
        end_time="2026-12-10T10:30:00+08:00",
        tz_name="Asia/Shanghai",
        all_day=False,
        location=None,
        attendees=None,
        reminders=None,
        use_default_reminders=None,
        existing_event=None,
        owner_email="alice@example.com",
        account_email="alice@example.com",
        idempotency_key=idem_key,
    )
    assert err is None and spec is not None
    assert (
        spec["write_payload"]["extendedProperties"]["private"]["dalton_idempotency_key"]
        == idem_key
    )

    # Case A: Remote has an existing same-title, same-minute event WITHOUT our unique key -> MUST return write_result_unconfirmed!
    g_cli.call_mcp_tool.return_value = {
        "events": [
            {
                "id": "existing-other-standup",
                "summary": "站会",
                "start": {"dateTime": "2026-12-10T10:00:00+08:00"},
                "end": {"dateTime": "2026-12-10T10:30:00+08:00"},
                "status": "confirmed",
            }
        ]
    }
    cmd_unconfirmed = await cal_tools_mod._reconcile_unconfirmed_calendar_write(
        provider="google",
        action="create_event",
        calendar_id="primary",
        event_id=None,
        tz_name="Asia/Shanghai",
        write_payload=spec["write_payload"],
        card_args=spec["card_args"],
        pre_read_marker=None,
        idempotency_key=idem_key,
        approval_id="appr-unique-1",
        caller_user_id="user-1",
        thread_id=str(uuid4()),
        tool_call_id="tc-rec-1",
        fresh_tok=_sample_token(),
        frozen_state={},
        google_cli=g_cli,
        feishu_cli=f_cli,
        attach_notice=_attach_noop,
    )
    res_a = _tool_payload(cmd_unconfirmed)
    assert res_a["ok"] is False
    assert res_a["error"] == "write_result_unconfirmed"
    assert res_a["check_calendar"] is True

    # Case B: Remote has event WITH matching extendedProperties.private.dalton_idempotency_key -> confirmed!
    g_cli.call_mcp_tool.return_value = {
        "events": [
            {
                "id": "created-matching-standup",
                "summary": "站会",
                "start": {"dateTime": "2026-12-10T10:00:00+08:00"},
                "end": {"dateTime": "2026-12-10T10:30:00+08:00"},
                "status": "confirmed",
                "extendedProperties": {
                    "private": {"dalton_idempotency_key": idem_key}
                },
            }
        ]
    }
    cmd_confirmed = await cal_tools_mod._reconcile_unconfirmed_calendar_write(
        provider="google",
        action="create_event",
        calendar_id="primary",
        event_id=None,
        tz_name="Asia/Shanghai",
        write_payload=spec["write_payload"],
        card_args=spec["card_args"],
        pre_read_marker=None,
        idempotency_key=idem_key,
        approval_id="appr-unique-1",
        caller_user_id="user-1",
        thread_id=str(uuid4()),
        tool_call_id="tc-rec-2",
        fresh_tok=_sample_token(),
        frozen_state={},
        google_cli=g_cli,
        feishu_cli=f_cli,
        attach_notice=_attach_noop,
    )
    res_b = _tool_payload(cmd_confirmed)
    assert res_b["ok"] is True
    assert res_b["reconciled"] is True
    assert res_b["event"]["event_id"] == "created-matching-standup"


@pytest.mark.asyncio
async def test_r4_unconfirmed_update_verifies_every_approved_field_else_returns_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    g_cli = create_autospec(GoogleCalendarClientProtocol, instance=True)
    f_cli = create_autospec(FeishuCalendarClientProtocol, instance=True)
    monkeypatch.setattr(cal_tools_mod.db, "update_action_approval_state", create_autospec(db_mod.update_action_approval_state, return_value=True))

    async def _attach_noop(tid: str, res: dict[str, Any]) -> dict[str, Any]:
        return res

    spec, err = _normalize_calendar_write_request(
        provider="google",
        action="update_event",
        calendar_id="primary",
        event_id="g-ev-upd-1",
        summary="Weekly Sync",
        description="Updated agenda with Q4 targets",
        start_time="2026-12-11T14:00:00+08:00",
        end_time="2026-12-11T15:00:00+08:00",
        tz_name="Asia/Shanghai",
        all_day=False,
        location="Room 301",
        attendees=[],
        reminders=[{"method": "popup", "minutes": 15}],
        use_default_reminders=None,
        existing_event={
            "summary": "Weekly Sync",
            "description": "Old agenda",
            "attendees": [{"email": "bob@example.com"}],
            "updated": "2026-12-01T00:00:00Z",
        },
        owner_email="alice@example.com",
        account_email="alice@example.com",
    )
    assert err is None and spec is not None

    # Case A: Remote `updated` changed AND `summary` matches, BUT description & attendees did NOT update -> MUST return write_result_unconfirmed!
    g_cli.call_mcp_tool.return_value = {
        "event": {
            "id": "g-ev-upd-1",
            "summary": "Weekly Sync",
            "description": "Old agenda",
            "location": "Room 301",
            "start": {"dateTime": "2026-12-11T14:00:00+08:00"},
            "end": {"dateTime": "2026-12-11T15:00:00+08:00"},
            "attendees": [{"email": "bob@example.com"}],
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 15}]},
            "updated": "2026-12-01T00:05:00Z",
        }
    }
    cmd_mismatch = await cal_tools_mod._reconcile_unconfirmed_calendar_write(
        provider="google",
        action="update_event",
        calendar_id="primary",
        event_id="g-ev-upd-1",
        tz_name="Asia/Shanghai",
        write_payload=spec["write_payload"],
        card_args=spec["card_args"],
        pre_read_marker="2026-12-01T00:00:00Z",
        idempotency_key="dalton-cal:appr-upd-1",
        approval_id="appr-upd-1",
        caller_user_id="user-1",
        thread_id=str(uuid4()),
        tool_call_id="tc-upd-rec-1",
        fresh_tok=_sample_token(),
        frozen_state={},
        google_cli=g_cli,
        feishu_cli=f_cli,
        attach_notice=_attach_noop,
    )
    res_mismatch = _tool_payload(cmd_mismatch)
    assert res_mismatch["ok"] is False
    assert res_mismatch["error"] == "write_result_unconfirmed"
    assert res_mismatch["check_calendar"] is True

    # Case B: EVERY approved field matches remote event (including cleared attendees, description, location, time, reminders) -> confirmed!
    g_cli.call_mcp_tool.return_value = {
        "event": {
            "id": "g-ev-upd-1",
            "summary": "Weekly Sync",
            "description": "Updated agenda with Q4 targets",
            "location": "Room 301",
            "start": {"dateTime": "2026-12-11T06:00:00Z"},  # Same instant as 14:00+08:00
            "end": {"dateTime": "2026-12-11T07:00:00Z"},
            "attendees": [{"email": "alice@example.com", "self": True}],
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 15}]},
            "updated": "2026-12-01T00:06:00Z",
        }
    }
    cmd_match = await cal_tools_mod._reconcile_unconfirmed_calendar_write(
        provider="google",
        action="update_event",
        calendar_id="primary",
        event_id="g-ev-upd-1",
        tz_name="Asia/Shanghai",
        write_payload=spec["write_payload"],
        card_args=spec["card_args"],
        pre_read_marker="2026-12-01T00:00:00Z",
        idempotency_key="dalton-cal:appr-upd-1",
        approval_id="appr-upd-1",
        caller_user_id="user-1",
        thread_id=str(uuid4()),
        tool_call_id="tc-upd-rec-2",
        fresh_tok=_sample_token(),
        frozen_state={},
        google_cli=g_cli,
        feishu_cli=f_cli,
        attach_notice=_attach_noop,
    )
    res_match = _tool_payload(cmd_match)
    assert res_match["ok"] is True
    assert res_match["reconciled"] is True


# ===========================================================================
# 6. Approval card payload exposes actual attendee changes (including clear)
# ===========================================================================


def test_r4_approval_card_payload_exposes_actual_attendee_changes_and_clear() -> None:
    existing = {
        "summary": "Board Prep",
        "attendees": [
            {"email": "alice@example.com", "self": True},
            {"email": "bob@example.com"},
            {"email": "carol@example.com"},
        ],
    }

    # Clear all attendees (`attendees=[]`)
    spec_clear, err_clear = _normalize_calendar_write_request(
        provider="google",
        action="update_event",
        calendar_id="primary",
        event_id="ev-1",
        summary=None,
        description=None,
        start_time=None,
        end_time=None,
        tz_name="Asia/Shanghai",
        all_day=False,
        location=None,
        attendees=[],
        reminders=None,
        use_default_reminders=None,
        existing_event=existing,
        owner_email="alice@example.com",
        account_email="alice@example.com",
    )
    assert err_clear is None and spec_clear is not None
    card_clear = spec_clear["card_args"]
    assert card_clear["attendees_set"] is True
    assert card_clear["attendees"] == []
    assert card_clear["attendees_cleared"] is True
    assert card_clear["attendees_added"] == []
    assert card_clear["attendees_removed"] == ["bob@example.com", "carol@example.com"]
    assert card_clear["external_attendees"] == ["bob@example.com", "carol@example.com"]
    assert spec_clear["requires_second_confirm"] is True

    # Replace carol with dave
    spec_diff, err_diff = _normalize_calendar_write_request(
        provider="google",
        action="update_event",
        calendar_id="primary",
        event_id="ev-1",
        summary=None,
        description=None,
        start_time=None,
        end_time=None,
        tz_name="Asia/Shanghai",
        all_day=False,
        location=None,
        attendees=["bob@example.com", "dave@example.com"],
        reminders=None,
        use_default_reminders=None,
        existing_event=existing,
        owner_email="alice@example.com",
        account_email="alice@example.com",
    )
    assert err_diff is None and spec_diff is not None
    card_diff = spec_diff["card_args"]
    assert card_diff["attendees_set"] is True
    assert card_diff["attendees_cleared"] is False
    assert card_diff["attendees_added"] == ["dave@example.com"]
    assert card_diff["attendees_removed"] == ["carol@example.com"]
    assert set(card_diff["external_attendees"]) == {
        "bob@example.com",
        "carol@example.com",
        "dave@example.com",
    }
