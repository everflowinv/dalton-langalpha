"""Round 6 unit tests for Personal Calendar Batch 1 Step 3 (narrowing & review fixes).

Covers all backend lead decisions:
1. LangSmith mutual exclusion: when LangSmith tracing is active, personal calendar readiness
   is unavailable (`langsmith_tracing_enabled=True`, `ready=False`), OAuth authorize returns 503,
   `build_dalton_tools` withholds `dalton_calendar` (covering PTC, Claude Code, and subagents),
   and direct tool entry refuses cleanly.
2. Normal `/retry` inherits `unwatched=True` from the retried attempt through the real retry
   endpoint -> `carry.carried_unwatched` -> `build_turn_context` -> `build_graph_config` ->
   `dalton_calendar` entry refusal.
3. Completion notifications on personal-private threads:
   - Private body is sent ONLY to a DM verified against the thread creator's CURRENT channel
     binding; unbound / other user's DM targets or unresolvable thread creator fail closed in
     BOTH `on_automation_settled` and `decide_automation_notification_plan`.
   - Group-only automations authorize DM redirect to the thread creator's bound DM through the
     real `evaluate_user_notify_eligibility` + real `decide_automation_notification_plan`
     without `target_removed`, and send only sanitized Card 2.0 notice to the group after DM
     succeeds (using `create_autospec` only for external transport).
4. Read-only OAuth scopes & compatibility: Google and Feishu authorize strictly read-only
   calendar scopes while existing connections with write scopes remain fully usable.
"""

from __future__ import annotations

from collections import namedtuple
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import ToolMessage
from langgraph.types import Command
import pytest

from src.dalton import capabilities as cap_mod
from src.dalton.app import integrations_router as integ_router_mod
from src.dalton.database import automation_notifications as auto_notify_db
from src.dalton.database import user_integrations as db_mod
from src.dalton.database.user_integrations import IntegrationRow, IntegrationStatus
from src.dalton.deployment_credentials.snapshot import CredentialGroupResult
from src.dalton.integrations.constants import (
    FEISHU_CALENDAR_SCOPES,
    GOOGLE_CALENDAR_MIN_SCOPES,
    GOOGLE_SCOPE_CALENDAR_EVENTS,
    GOOGLE_SCOPE_CALENDAR_EVENTS_RO,
    GOOGLE_SCOPE_CALENDAR_LIST_RO,
    scope_satisfied,
)
from src.dalton.integrations.feishu_calendar import FeishuCalendarClientProtocol
from src.dalton.integrations.google_calendar import GoogleCalendarClientProtocol
from src.dalton.llm.model_prefs import (
    resolve_dalton_notify_enabled,
    resolve_dalton_notify_overrides,
    resolve_dalton_notify_routes,
    resolve_dalton_notify_targets,
)
from src.dalton.notify import dispatch as notify_dispatch_mod
from src.dalton.services import personal_integration_guard as guard_mod
from src.dalton.services import user_integrations_oauth as oauth_mod
from src.dalton.services.personal_integration_guard import (
    PERSONAL_INTEGRATION_GROUP_REDIRECT_NOTICE,
)
from src.dalton.services.user_integrations_oauth import (
    FreshAccessToken,
    IntegrationCredentialsNotReadyError,
    MissingRequiredScopesError,
    _check_required_scopes,
    _missing_min_scopes,
)
from src.dalton.tools import build_dalton_tools
from src.dalton.tools import calendar_tools as cal_tools_mod
from src.dalton.tools.calendar_tools import (
    UNATTENDED_CALENDAR_REFUSAL_MESSAGE,
    create_calendar_tools,
)
from src.server.app.threads import messaging as messaging_mod
from src.server.database.runs import lifecycle as tl_db
from src.server.handlers.chat import request_prep as request_prep_mod
from src.server.models.chat import ChatRequest
from src.server.services.report_back.handoff import carry


@pytest.fixture(autouse=True)
def _enable_dalton_clean_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DALTON_ENABLED", "1")
    monkeypatch.setenv("DALTON_PROFILE", "everflow")
    for var in (
        "LANGSMITH_TRACING",
        "LANGSMITH_TRACING_V2",
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_TRACING",
    ):
        monkeypatch.delenv(var, raising=False)


def _tool_payload(cmd: Command) -> dict[str, Any]:
    msgs = cmd.update["messages"]
    assert len(msgs) == 1
    assert isinstance(msgs[0], ToolMessage)
    return json.loads(str(msgs[0].content))


def _sample_conn(
    provider: str = "google",
    *,
    granted_scopes: str = "openid https://www.googleapis.com/auth/calendar.calendarlist.readonly https://www.googleapis.com/auth/calendar.events.readonly",
) -> IntegrationRow:
    now = datetime.now(UTC)
    return IntegrationRow(
        user_id="user-1",
        provider=provider,
        service="calendar",
        oauth_client_id="client-1",
        provider_account_id="acct-1",
        provider_union_id=None,
        provider_email="alice@example.com",
        granted_scopes=granted_scopes,
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


def _sample_token(
    provider: str = "google",
    *,
    granted_scopes: tuple[str, ...] = (
        GOOGLE_SCOPE_CALENDAR_LIST_RO,
        GOOGLE_SCOPE_CALENDAR_EVENTS_RO,
    ),
) -> FreshAccessToken:
    return FreshAccessToken(
        access_token="tok-secret",
        provider=provider,
        service="calendar",
        oauth_client_id="client-1",
        provider_account_id="acct-1",
        auth_epoch=1,
        token_generation=1,
        granted_scopes=granted_scopes,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )


# ===========================================================================
# 1. LangSmith mutual exclusion: readiness unavailable, tool not loaded
# ===========================================================================


@pytest.mark.asyncio
async def test_r6_langsmith_tracing_disables_calendar_readiness_blocks_oauth_and_withholds_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Make all OAuth credentials, public URL, and encryption ready so ONLY LangSmith tracing decides readiness.
    monkeypatch.setenv("PUBLIC_APP_URL", "https://dalton.example.com")
    monkeypatch.setattr(
        "src.server.database.encryption.encryption_configured",
        lambda: True,
    )
    monkeypatch.setattr(
        "src.dalton.deployment_credentials.snapshot.get_credential_group",
        lambda group: CredentialGroupResult(
            group=group,
            ready=True,
            source="env",
            revision=1,
            values={
                "DALTON_GOOGLE_OAUTH_CLIENT_ID": "123456-abc.apps.googleusercontent.com",
                "DALTON_GOOGLE_OAUTH_CLIENT_SECRET": "g-secret",
                "DALTON_FEISHU_APP_ID": "cli_test",
                "DALTON_FEISHU_APP_SECRET": "f-secret",
                "DALTON_FEISHU_ENCRYPT_KEY": "enc-k",
                "DALTON_FEISHU_VERIFICATION_TOKEN": "ver-t",
            },
        ),
    )
    monkeypatch.setattr(
        db_mod,
        "list_user_connections",
        create_autospec(db_mod.list_user_connections, return_value=[]),
    )
    monkeypatch.setattr(
        db_mod,
        "list_user_purge_ledgers",
        create_autospec(db_mod.list_user_purge_ledgers, return_value=[]),
    )

    # Baseline: tracing OFF -> ready=True and tool IS loaded for both roles.
    assert cap_mod.is_langsmith_tracing_active() is False
    summary_off = cap_mod.get_connector_providers_summary()
    assert summary_off["google"]["ready"] is True
    assert summary_off["google"]["langsmith_tracing_enabled"] is False
    assert summary_off["feishu"]["ready"] is True
    assert summary_off["feishu"]["langsmith_tracing_enabled"] is False
    assert any(t.name == "dalton_calendar" for t in build_dalton_tools("chief_of_staff"))
    assert any(t.name == "dalton_calendar" for t in build_dalton_tools("analyst"))

    # Turn LangSmith tracing ON -> readiness reports unavailable, tool NOT loaded, OAuth blocked, tool entry refuses.
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    assert cap_mod.is_langsmith_tracing_active() is True

    summary_on = cap_mod.get_connector_providers_summary()
    assert summary_on["google"]["ready"] is True
    assert summary_on["google"]["langsmith_tracing_enabled"] is True
    assert summary_on["google"]["services"]["calendar"]["ready"] is False
    assert summary_on["google"]["services"]["mail"]["ready"] is True
    assert summary_on["feishu"]["ready"] is True
    assert summary_on["feishu"]["langsmith_tracing_enabled"] is True
    assert summary_on["feishu"]["services"]["calendar"]["ready"] is False
    assert summary_on["feishu"]["services"]["mail"]["ready"] is True

    # Neither CoS nor Analyst mounts dalton_calendar (covers PTC, Claude Code, and subagents).
    assert not any(t.name == "dalton_calendar" for t in build_dalton_tools("chief_of_staff"))
    assert not any(t.name == "dalton_calendar" for t in build_dalton_tools("analyst"))

    # Settings API reports ready=False + langsmith_tracing_enabled=True, and authorize returns 503.
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

    list_resp = client.get("/api/v1/dalton/integrations")
    assert list_resp.status_code == 200
    cal_connectors = [
        c for c in list_resp.json()["connectors"] if c["service"] == "calendar"
    ]
    assert len(cal_connectors) == 2
    for conn_info in cal_connectors:
        assert conn_info["ready"] is False
        assert conn_info["langsmith_tracing_enabled"] is True

    auth_resp = client.post("/api/v1/dalton/integrations/google/calendar/authorize")
    assert auth_resp.status_code == 503
    assert "当前部署开启了调用追踪，个人日历不可用" in json.dumps(
        auth_resp.json(), ensure_ascii=False
    )

    with pytest.raises(IntegrationCredentialsNotReadyError, match="调用追踪"):
        await oauth_mod.start_oauth_authorization(
            user_id="user-1",
            provider="google",
            service="calendar",
            auth_mode="local",
        )

    # Direct tool entry defense-in-depth refuses before DB/token/upstream calls.
    tool_obj = create_calendar_tools()[0]
    cmd = await tool_obj.coroutine(
        action="list_events",
        provider="google",
        config={"configurable": {"user_id": "user-1", "thread_id": str(uuid4())}},
        tool_call_id="tc-ls-block",
    )
    payload = _tool_payload(cmd)
    assert payload["ok"] is False
    assert payload["error"] == "langsmith_tracing_incompatible"
    assert "当前部署开启了调用追踪，个人日历不可用" in payload["message"]


# ===========================================================================
# 2. Normal `/retry` inherits `unwatched=True` and rejects personal calendar
# ===========================================================================


@pytest.mark.asyncio
async def test_r6_retry_endpoint_inherits_unwatched_and_rejects_personal_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    ws_id = str(uuid4())
    failed_run_id = str(uuid4())

    failed_agent_run = {
        "conversation_response_id": failed_run_id,
        "conversation_thread_id": tid,
        "status": "error",
        "turn_index": 0,
        "metadata": {
            "unwatched": True,
            "run_origin": "turn",
            "subagents_enabled": None,
        },
    }

    # Wire real retry_thread endpoint -> capture exact ChatRequest & retry_of_run_id forwarded to _handle_send_message
    captured: dict[str, Any] = {}

    async def _capture_send(
        request: ChatRequest,
        auth: Any,
        thread_id: str,
        raw_request: Any,
        *,
        retry_of_run_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        captured["request"] = request
        captured["retry_of_run_id"] = retry_of_run_id
        return {"ok": True}

    from src.server.handlers import checkpoint_handler as cp_handler_mod
    from src.server.services.runs import pending_input as pending_input_mod

    monkeypatch.setattr(
        messaging_mod.cli_approval,
        "admit_retry",
        create_autospec(messaging_mod.cli_approval.admit_retry, return_value=None),
    )
    monkeypatch.setattr(
        messaging_mod,
        "get_thread_auth_meta",
        create_autospec(
            messaging_mod.get_thread_auth_meta,
            return_value={"user_id": "user-1", "workspace_id": ws_id},
        ),
    )
    monkeypatch.setattr(
        messaging_mod,
        "_assert_stream_transport_ready",
        create_autospec(messaging_mod._assert_stream_transport_ready, return_value=None),
    )
    monkeypatch.setattr(
        tl_db,
        "get_latest_attempt",
        create_autospec(tl_db.get_latest_attempt, return_value=failed_agent_run),
    )
    monkeypatch.setattr(
        cp_handler_mod,
        "get_retry_checkpoint",
        create_autospec(cp_handler_mod.get_retry_checkpoint, return_value="cp-1"),
    )
    monkeypatch.setattr(
        pending_input_mod,
        "retried_wake_fields",
        create_autospec(pending_input_mod.retried_wake_fields, return_value={}),
    )
    monkeypatch.setattr(
        messaging_mod,
        "_handle_send_message",
        create_autospec(messaging_mod._handle_send_message, side_effect=_capture_send),
    )

    from src.server.dependencies.principal import Principal

    principal = Principal(user_id="user-1", auth="session")

    async def _limit(uid: str) -> Any:
        return SimpleNamespace(user_id=uid, burst_slot_id=None)

    fake_raw_req = SimpleNamespace(
        headers={},
        scope={},
        app=SimpleNamespace(state=SimpleNamespace()),
    )
    await messaging_mod.retry_thread(
        thread_id=tid,
        principal=principal,
        limit=_limit,
        body=messaging_mod.RetryRequest(workspace_id=ws_id),
        raw_request=fake_raw_req,  # type: ignore[arg-type]
    )

    assert captured["retry_of_run_id"] == failed_run_id
    retried_req: ChatRequest = captured["request"].model_copy(
        update={"retry_of_run_id": captured["retry_of_run_id"]}
    )
    # Plain user /retry request itself has no origin=agent or query_type=system
    assert request_prep_mod.is_unwatched_turn(retried_req) is False

    # Now run production ptc_run handoff: carry.prior_attempt -> carry.carried_unwatched -> build_turn_context -> build_graph_config
    monkeypatch.setattr(
        tl_db,
        "get_run",
        create_autospec(tl_db.get_run, return_value=failed_agent_run),
    )
    prior = await carry.prior_attempt(retried_req, tid)
    carried = carry.carried_unwatched(retried_req, prior)
    assert carried is True

    turn_ctx = request_prep_mod.build_turn_context(
        retried_req,
        request_prep_mod.PriorThread(),
        user_profile=None,
        carried_unwatched=carried,
    )
    assert turn_ctx.unwatched is True

    graph_config = request_prep_mod.build_graph_config(
        thread_id=tid,
        user_id="user-1",
        workspace_id=ws_id,
        timezone_str="Asia/Shanghai",
        token_callback=None,
        request=retried_req,
        effective_model="gpt-5",
        unwatched=turn_ctx.unwatched,
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
        tool_call_id="tc-retry-unwatched",
    )
    payload = _tool_payload(cmd)
    assert payload["ok"] is False
    assert payload["error"] == "unattended_turn_unsupported"
    assert payload["message"] == UNATTENDED_CALENDAR_REFUSAL_MESSAGE
    assert list_conns.call_count == 0
    assert token_fn.call_count == 0
    assert g_cli.call_mcp_tool.call_count == 0

    # Retrying a normal interactive attempt (unwatched=False) does NOT force unwatched=True.
    interactive_failed_run = {
        **failed_agent_run,
        "metadata": {"unwatched": False},
    }
    assert (
        carry.carried_unwatched(
            retried_req,
            carry.PriorAttempt(latest=interactive_failed_run, retried=interactive_failed_run),
        )
        is None
    )


# ===========================================================================
# 3. Completion notify: creator-only DM binding + gateway DM redirect authorization
# ===========================================================================


class _BindingAwareCursor:
    """SQL-routed async cursor backing conversation_threads + channel binding checks."""

    def __init__(
        self,
        *,
        threads: dict[str, dict[str, Any]],
        identities: set[tuple[str, str, str]],
        weixin_accounts: set[tuple[str, str]] | None = None,
    ) -> None:
        self.threads = threads
        self.identities = identities
        self.weixin_accounts = weixin_accounts or set()
        self._one: Any = None
        self._many: list[Any] = []

    async def __aenter__(self) -> _BindingAwareCursor:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split()).lower()
        p = tuple(params or ())
        self._one = None
        self._many = []
        if "from conversation_threads" in s:
            tid = str(p[0]) if p else ""
            self._one = self.threads.get(tid)
        elif "from dalton_channel_identities" in s:
            if "platform_user_id = %s" in s:
                uid, plat, puid = str(p[0]), str(p[1]), str(p[2])
                self._one = (1,) if (uid, plat, puid) in self.identities else None
            else:
                uid = str(p[0])
                self._many = [
                    {"platform": plat, "platform_user_id": puid}
                    for (u, plat, puid) in sorted(self.identities)
                    if u == uid
                ]
        elif "from dalton_channel_weixin_accounts" in s:
            if "ilink_user_id = %s" in s:
                uid, wx_uid = str(p[0]), str(p[1])
                self._one = (1,) if (uid, wx_uid) in self.weixin_accounts else None
            else:
                uid = str(p[0])
                self._many = [
                    {"ilink_user_id": wx_uid}
                    for (u, wx_uid) in sorted(self.weixin_accounts)
                    if u == uid
                ]
        elif "from dalton_channel_deliveries" in s:
            self._one = None

    async def fetchone(self) -> Any:
        return self._one

    async def fetchall(self) -> list[Any]:
        return list(self._many)


class _BindingAwareConn:
    def __init__(self, cursor: _BindingAwareCursor) -> None:
        self._cur = cursor

    def cursor(self, **_kw: Any) -> _BindingAwareCursor:
        return self._cur


@pytest.mark.asyncio
async def test_r6_private_completion_notify_blocks_unbound_or_other_users_dm_in_dispatch_and_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    auto_id = str(uuid4())
    exec_id = str(uuid4())
    delivery_id = str(uuid4())

    # Thread belongs to user-1 (private), who is ONLY bound to feishu:user:ou_alice_bound (NOT ou_other_person).
    db_cur = _BindingAwareCursor(
        threads={
            tid: {
                "has_personal_integration_data": True,
                "user_id": "user-1",
                "thread_user_id": "user-1",
            }
        },
        identities={("user-1", "feishu", "ou_alice_bound")},
    )

    @asynccontextmanager
    async def _fake_get_db_conn(conn: Any = None):
        yield conn if conn is not None else _BindingAwareConn(db_cur)

    monkeypatch.setattr(guard_mod, "get_db_connection", _fake_get_db_conn)
    monkeypatch.setattr(
        notify_dispatch_mod,
        "_resolve_unit_managed_notify_gate",
        create_autospec(
            notify_dispatch_mod._resolve_unit_managed_notify_gate,
            return_value=(True, None, None, True),
        ),
    )
    monkeypatch.setattr(
        auto_notify_db,
        "_resolve_unit_managed_notify_gate",
        create_autospec(
            auto_notify_db._resolve_unit_managed_notify_gate,
            return_value=(True, None, None, True),
        ),
    )
    monkeypatch.setattr(
        auto_notify_db,
        "_parent_is_deleted",
        create_autospec(auto_notify_db._parent_is_deleted, return_value=False),
    )
    monkeypatch.setattr(notify_dispatch_mod, "notify_via_gateway_enabled", lambda: False)

    # Automation configured with someone else's Feishu DM (`ou_other_person`)
    other_dm = "feishu:user:ou_other_person"
    automation = {
        "automation_id": auto_id,
        "user_id": "user-1",
        "name": "Daily Personal Brief",
        "delivery_config": {},
    }
    routes = {f"automation:{auto_id}": [other_dm]}

    enabled_fn = create_autospec(resolve_dalton_notify_enabled, return_value=True)
    overrides_fn = create_autospec(resolve_dalton_notify_overrides, return_value={})
    targets_fn = create_autospec(resolve_dalton_notify_targets, return_value=[other_dm])
    routes_fn = create_autospec(resolve_dalton_notify_routes, return_value=routes)
    admin_fn = create_autospec(notify_dispatch_mod._default_notify_admin, return_value=True)
    post_spy = create_autospec(notify_dispatch_mod.post_bridge_notify, return_value={"ok": True})
    claim_spy = create_autospec(notify_dispatch_mod.claim_target_delivery, return_value=True)
    mark_spy = create_autospec(notify_dispatch_mod.mark_target_delivery, return_value=None)

    async def _no_verdict(*_a: Any, **_kw: Any) -> None:
        return None

    # (a) Real on_automation_settled + real evaluate_user_notify_eligibility MUST NOT send to ou_other_person!
    sent = await notify_dispatch_mod.on_automation_settled(
        automation=automation,
        execution_id=exec_id,
        thread_id=tid,
        final_text="【私密日程】15:00 体检复查",
        resolve_enabled_fn=enabled_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        resolve_routes_fn=routes_fn,
        is_admin_fn=admin_fn,
        get_verdict_fn=_no_verdict,
        claim_target_fn=claim_spy,
        mark_target_fn=mark_spy,
        post_notify_fn=post_spy,
        frontend_url_fn=lambda: "https://app.example.com",
    )
    assert sent is False
    assert post_spy.call_count == 0
    assert claim_spy.call_count == 0

    # (b) Gateway authorization (real decide_automation_notification_plan) ALSO denies ou_other_person!
    card = {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": "完成"}, "template": "blue"},
        "body": {"elements": [{"tag": "markdown", "content": "【私密日程】15:00 体检复查"}]},
    }
    plan_other = await auto_notify_db.decide_automation_notification_plan(
        db_cur,
        {
            "delivery_id": delivery_id,
            "automation_id": auto_id,
            "user_id": "user-1",
            "channel": "feishu",
            "target": other_dm,
            "status": "pending",
            "payload_json": {
                "text": "【私密日程】15:00 体检复查",
                "card": card,
                "automation_name": "Daily Personal Brief",
                "category": "custom",
                "source": "automation_completion",
                "transport": "gateway",
                "thread_id": tid,
            },
        },
        delivery_id=delivery_id,
        resolve_enabled_fn=enabled_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        resolve_routes_fn=routes_fn,
        is_admin_fn=admin_fn,
    )
    assert plan_other["authorized"] is False
    assert plan_other["code"] == "personal_dm_recipient_unverified"

    # (c) When thread creator cannot be verified (missing conversation_threads row), fail closed!
    missing_tid = str(uuid4())
    plan_unverifiable = await auto_notify_db.decide_automation_notification_plan(
        db_cur,
        {
            "delivery_id": delivery_id,
            "automation_id": auto_id,
            "user_id": "user-1",
            "channel": "feishu",
            "target": "feishu:user:ou_alice_bound",
            "status": "pending",
            "payload_json": {
                "text": "【私密日程】15:00 体检复查",
                "card": card,
                "automation_name": "Daily Personal Brief",
                "category": "custom",
                "source": "automation_completion",
                "transport": "gateway",
                "thread_id": missing_tid,
                "personal_private_dm_redirect": True,
            },
        },
        delivery_id=delivery_id,
        resolve_enabled_fn=enabled_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        resolve_routes_fn=routes_fn,
        is_admin_fn=admin_fn,
    )
    assert plan_unverifiable["authorized"] is False
    assert plan_unverifiable["code"] == "personal_dm_recipient_unverified"


@pytest.mark.asyncio
async def test_r6_private_group_only_automation_authorizes_bound_dm_redirect_without_target_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    auto_id = str(uuid4())
    exec_id = str(uuid4())
    group_target = "feishu:chat:oc_room_only"
    alice_bound_dm = "feishu:user:ou_alice_bound"

    db_cur = _BindingAwareCursor(
        threads={
            tid: {
                "has_personal_integration_data": True,
                "user_id": "user-1",
                "thread_user_id": "user-1",
            }
        },
        identities={("user-1", "feishu", "ou_alice_bound")},
    )

    @asynccontextmanager
    async def _fake_get_db_conn(conn: Any = None):
        yield conn if conn is not None else _BindingAwareConn(db_cur)

    monkeypatch.setattr(guard_mod, "get_db_connection", _fake_get_db_conn)
    monkeypatch.setattr(
        notify_dispatch_mod,
        "_resolve_unit_managed_notify_gate",
        create_autospec(
            notify_dispatch_mod._resolve_unit_managed_notify_gate,
            return_value=(True, None, None, True),
        ),
    )
    monkeypatch.setattr(
        auto_notify_db,
        "_resolve_unit_managed_notify_gate",
        create_autospec(
            auto_notify_db._resolve_unit_managed_notify_gate,
            return_value=(True, None, None, True),
        ),
    )
    monkeypatch.setattr(
        auto_notify_db,
        "_parent_is_deleted",
        create_autospec(auto_notify_db._parent_is_deleted, return_value=False),
    )
    # Run the real production gateway transport path!
    monkeypatch.setattr(notify_dispatch_mod, "notify_via_gateway_enabled", lambda: True)

    automation = {
        "automation_id": auto_id,
        "user_id": "user-1",
        "name": "Group Only Calendar Brief",
        "delivery_config": {},
    }
    # Task is configured ONLY with a group target (NO DM in route!)
    routes = {f"automation:{auto_id}": [group_target]}
    enabled_fn = create_autospec(resolve_dalton_notify_enabled, return_value=True)
    overrides_fn = create_autospec(resolve_dalton_notify_overrides, return_value={})
    targets_fn = create_autospec(resolve_dalton_notify_targets, return_value=[alice_bound_dm])
    routes_fn = create_autospec(resolve_dalton_notify_routes, return_value=routes)
    admin_fn = create_autospec(notify_dispatch_mod._default_notify_admin, return_value=True)

    claimed_rows: dict[str, dict[str, Any]] = {}
    ClaimRes = namedtuple("ClaimRes", ["claimed", "delivery_id", "attempt"])

    async def _store_claim(
        execution_id: str,
        channel: str,
        target: str,
        *,
        user_id: str = "",
        automation_id: str | None = None,
        payload: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Any:
        did = str(uuid4())
        claimed_rows[did] = {
            "delivery_id": did,
            "execution_id": execution_id,
            "automation_id": automation_id,
            "user_id": user_id,
            "channel": channel,
            "target": target,
            "status": "pending",
            "payload_json": dict(payload or {}),
        }
        return ClaimRes(True, did, 1)

    authorized_plans: list[dict[str, Any]] = []

    async def _gateway_transport_via_real_plan(
        delivery_id: str,
        *,
        target: str,
    ) -> dict[str, Any]:
        # External transport boundary runs REAL backend plan authorization (decide_automation_notification_plan)!
        row = claimed_rows[delivery_id]
        plan = await auto_notify_db.decide_automation_notification_plan(
            db_cur,
            row,
            delivery_id=delivery_id,
            resolve_enabled_fn=enabled_fn,
            resolve_overrides_fn=overrides_fn,
            resolve_targets_fn=targets_fn,
            resolve_routes_fn=routes_fn,
            is_admin_fn=admin_fn,
        )
        authorized_plans.append(plan)
        if not plan.get("authorized"):
            return {
                "ok": False,
                "results": [
                    {
                        "target": target,
                        "ok": False,
                        "status": plan.get("status"),
                        "code": plan.get("code"),
                    }
                ],
            }
        return {"ok": True, "results": [{"target": target, "ok": True}]}

    monkeypatch.setattr(
        notify_dispatch_mod,
        "claim_target_delivery_with_attempt",
        create_autospec(
            notify_dispatch_mod.claim_target_delivery_with_attempt,
            side_effect=_store_claim,
        ),
    )
    post_gw_spy = create_autospec(
        notify_dispatch_mod.post_automation_gateway_notify,
        side_effect=_gateway_transport_via_real_plan,
    )
    monkeypatch.setattr(
        notify_dispatch_mod,
        "post_automation_gateway_notify",
        post_gw_spy,
    )
    mark_spy = create_autospec(notify_dispatch_mod.mark_target_delivery, return_value=None)

    async def _no_verdict(*_a: Any, **_kw: Any) -> None:
        return None

    ok = await notify_dispatch_mod.on_automation_settled(
        automation=automation,
        execution_id=exec_id,
        thread_id=tid,
        final_text="【私人日程】11:00 与投资人一对一会面",
        resolve_enabled_fn=enabled_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        resolve_routes_fn=routes_fn,
        is_admin_fn=admin_fn,
        get_verdict_fn=_no_verdict,
        mark_target_fn=mark_spy,
        frontend_url_fn=lambda: "https://app.example.com",
    )

    assert ok is True
    assert len(authorized_plans) == 2

    # 1st plan: thread creator's bound DM authorized (NOT rejected with target_removed!) with full body
    dm_plan = authorized_plans[0]
    assert dm_plan["authorized"] is True
    assert dm_plan["requested_target"] == alice_bound_dm
    assert dm_plan["recipient_id"] == "ou_alice_bound"
    assert "投资人一对一会面" in json.dumps(dm_plan["items"][0]["card"], ensure_ascii=False)

    # 2nd plan: group receives ONLY sanitized Card 2.0 redirect notice (zero private body)
    group_plan = authorized_plans[1]
    assert group_plan["authorized"] is True
    assert group_plan["requested_target"] == group_target
    group_card = group_plan["items"][0]["card"]
    assert group_card.get("schema") == "2.0"
    group_card_json = json.dumps(group_card, ensure_ascii=False)
    assert PERSONAL_INTEGRATION_GROUP_REDIRECT_NOTICE in group_card_json
    assert "投资人一对一会面" not in group_card_json

    # Toggle-off check: if user disables notifications, redirected DM is still denied with toggle_off!
    disabled_fn = create_autospec(resolve_dalton_notify_enabled, return_value=False)
    dm_delivery_id = dm_plan["delivery_id"]
    denied_off = await auto_notify_db.decide_automation_notification_plan(
        db_cur,
        claimed_rows[dm_delivery_id],
        delivery_id=dm_delivery_id,
        resolve_enabled_fn=disabled_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        resolve_routes_fn=routes_fn,
        is_admin_fn=admin_fn,
    )
    assert denied_off["authorized"] is False
    assert denied_off["code"] == "toggle_off"


# ===========================================================================
# 4. Read-only OAuth scopes & compatibility with existing write connections
# ===========================================================================


@pytest.mark.asyncio
async def test_r6_calendar_oauth_requests_readonly_scopes_and_accepts_existing_write_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Constants request strictly read-only scopes for both Google and Feishu
    assert GOOGLE_SCOPE_CALENDAR_LIST_RO in GOOGLE_CALENDAR_MIN_SCOPES
    assert GOOGLE_SCOPE_CALENDAR_EVENTS_RO in GOOGLE_CALENDAR_MIN_SCOPES
    assert GOOGLE_SCOPE_CALENDAR_EVENTS not in GOOGLE_CALENDAR_MIN_SCOPES

    assert "calendar:calendar:readonly" in FEISHU_CALENDAR_SCOPES
    assert "calendar:calendar.event:read" in FEISHU_CALENDAR_SCOPES
    for write_scope in (
        "calendar:calendar.event:create",
        "calendar:calendar.event:update",
        "calendar:calendar.event:delete",
    ):
        assert write_scope not in FEISHU_CALENDAR_SCOPES

    # 2. Scope check accepts both new read-only grants AND existing write-scope grants
    assert _missing_min_scopes(
        "google",
        "calendar",
        [GOOGLE_SCOPE_CALENDAR_LIST_RO, GOOGLE_SCOPE_CALENDAR_EVENTS_RO],
    ) == []
    assert _missing_min_scopes(
        "google",
        "calendar",
        [GOOGLE_SCOPE_CALENDAR_LIST_RO, GOOGLE_SCOPE_CALENDAR_EVENTS],
    ) == []
    assert _missing_min_scopes(
        "feishu",
        "calendar",
        ["calendar:calendar:readonly", "calendar:calendar.event:read"],
    ) == []
    assert _missing_min_scopes(
        "feishu",
        "calendar",
        [
            "offline_access",
            "calendar:calendar:readonly",
            "calendar:calendar.event:read",
            "calendar:calendar.event:create",
        ],
    ) == []
    assert scope_satisfied(GOOGLE_SCOPE_CALENDAR_EVENTS_RO, [GOOGLE_SCOPE_CALENDAR_EVENTS]) is True

    # _check_required_scopes passes for existing Google write connection when tool requests read-only scope
    _check_required_scopes(
        f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS}",
        cal_tools_mod._required_scopes_for("google", "list_events"),
    )
    # And still rejects when events scope is missing completely
    with pytest.raises(MissingRequiredScopesError):
        _check_required_scopes(
            GOOGLE_SCOPE_CALENDAR_LIST_RO,
            cal_tools_mod._required_scopes_for("google", "list_events"),
        )

    # 3. OAuth authorize URLs request strictly read-only scopes for Google and Feishu
    monkeypatch.setenv("PUBLIC_APP_URL", "https://dalton.example.com")
    monkeypatch.setattr(oauth_mod, "encryption_configured", lambda: True)
    monkeypatch.setattr(
        oauth_mod,
        "get_credential_group",
        lambda group: CredentialGroupResult(
            group=group,
            ready=True,
            source="env",
            revision=1,
            values={
                "DALTON_GOOGLE_OAUTH_CLIENT_ID": "123456-abc.apps.googleusercontent.com",
                "DALTON_GOOGLE_OAUTH_CLIENT_SECRET": "g-secret",
                "DALTON_FEISHU_APP_ID": "cli_test",
                "DALTON_FEISHU_APP_SECRET": "f-secret",
            },
        ),
    )
    monkeypatch.setattr(
        oauth_mod.db,
        "list_user_connections",
        create_autospec(db_mod.list_user_connections, return_value=[]),
    )
    monkeypatch.setattr(
        oauth_mod.db,
        "list_user_purge_ledgers",
        create_autospec(db_mod.list_user_purge_ledgers, return_value=[]),
    )
    monkeypatch.setattr(
        oauth_mod.db,
        "insert_oauth_state",
        create_autospec(db_mod.insert_oauth_state, return_value=None),
    )

    g_started = await oauth_mod.start_oauth_authorization(
        user_id="user-1",
        provider="google",
        service="calendar",
        auth_mode="local",
    )
    g_scopes = set(parse_qs(urlsplit(g_started.authorize_url).query)["scope"][0].split())
    assert GOOGLE_SCOPE_CALENDAR_EVENTS_RO in g_scopes
    assert GOOGLE_SCOPE_CALENDAR_LIST_RO in g_scopes
    assert GOOGLE_SCOPE_CALENDAR_EVENTS not in g_scopes

    f_started = await oauth_mod.start_oauth_authorization(
        user_id="user-1",
        provider="feishu",
        service="calendar",
        auth_mode="local",
    )
    f_scopes = set(parse_qs(urlsplit(f_started.authorize_url).query)["scope"][0].split())
    assert "calendar:calendar:readonly" in f_scopes
    assert "calendar:calendar.event:read" in f_scopes
    assert "calendar:calendar.event:create" not in f_scopes
    assert "calendar:calendar.event:update" not in f_scopes
    assert "calendar:calendar.event:delete" not in f_scopes

    # 4. Tool not-connected copy reflects read-only wording
    monkeypatch.setattr(
        cal_tools_mod,
        "get_connector_providers_summary",
        lambda: {
            "google": {"ready": True, "credentials_ready": True, "langsmith_tracing_enabled": False},
            "feishu": {"ready": True, "credentials_ready": True, "langsmith_tracing_enabled": False},
        },
    )
    tool_obj = create_calendar_tools()[0]
    not_conn_cmd = await tool_obj.coroutine(
        action="list_events",
        provider="auto",
        config={"configurable": {"user_id": "user-1", "thread_id": str(uuid4())}},
        tool_call_id="tc-not-conn",
    )
    not_conn_payload = _tool_payload(not_conn_cmd)
    assert not_conn_payload["error"] == "not_connected"
    assert "在私密对话中查看日程和空闲时间" in not_conn_payload["message"]
    assert "管理日程" not in not_conn_payload["message"]
