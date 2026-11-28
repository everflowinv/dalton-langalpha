"""Round 3 unit tests for Personal Calendar Batch 1 Step 3.

Covers all Codex P0/P1/P2 and Grok Medium/Low items decided by lead:
1. P0: Unattended automation & channel egress guard (backend + gateway):
   - Private thread group/channel delivery denied with `personal_integration_private_only`,
     redirected to verified 1:1 DM, and group receives only sanitized redirect notice.
   - `automation_delivery.start_run`/`finish_run` propagate `has_personal_integration_data`,
     `automation_settlement` suppresses external webhooks, and `notify/dispatch.py`
     redirects group notifications to DM + sanitized notice.
2. P1 / Grok Medium: Unconditional refusal of `delegate_to_analyst` on private threads.
3. P1: Touch & disconnect share the same `lifecycle_lock_key` + `touch_user_lock_key`
   in the same transaction (with concurrent "token acquired then another window disconnects" test).
4. P1 / Grok Medium: LangSmith redacts first LLM call emitting personal tool call,
   all subsequent normal tools, and pre-arms new turns from DB; Transcript carries
   multi-turn taint + redacts first AI prose before personal tool call.
5. P1: Write state split (`write_dispatched_unconfirmed` / `send_ambiguous` reconciles
   by idempotency key without duplicate write; `awaiting_google_form` re-checks event
   version including when `get_event` asks for a Google form) — real LangGraph StateGraph tests.
6. P1: Approval resume re-checks saved `provider` capability even for `provider="auto"`.
7. P1 / Grok Low: `check_availability` treats `needs_reauth` as `status="unknown"`;
   `list_events` paginates and marks `truncated=True`; docstring documents both.
8. P2: Approval payload carries delete original start/end time and explicit clear/toggle flags.
9. P2: First-touch privacy notice persisted once on owning turn, emitted live, replayed
   only on owning turn, and still recorded when user cancels write approval.
10. P2 / Grok Low: `dalton_calendar` reuses backend `ready` summary for admin-setup guidance.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace as dc_replace
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
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
import pytest

from ptc_agent.agent.transcript.render import segment_shape
from ptc_agent.agent.transcript.store import build_directory
from ptc_agent.agent.middleware.compaction.window import Window
from src.dalton.capabilities import CapabilityId
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database import user_integrations as db_mod
from src.dalton.database.user_integrations import (
    ActionApprovalRow,
    IntegrationRow,
    IntegrationStatus,
    lifecycle_lock_key,
    touch_user_lock_key,
)
from src.dalton.integrations.feishu_calendar import FeishuCalendarClientProtocol
from src.dalton.integrations.google_calendar import (
    GoogleCalendarClientProtocol,
    GoogleMCPElicitationRequired,
)
from src.dalton.notify import dispatch as notify_dispatch_mod
from src.dalton.services import personal_integration_approval as approval_mod
from src.dalton.services import personal_integration_guard as guard_mod
from src.dalton.services.personal_integration_approval import (
    canonical_payload_sha256,
    verify_personal_integration_interrupt_resume,
)
from src.dalton.services.personal_integration_guard import (
    FIRST_TOUCH_NOTICE_CALENDAR,
    PERSONAL_INTEGRATION_GROUP_REDIRECT_NOTICE,
    PersonalSurfaceDeniedError,
    PersonalSurfaceVerificationResult,
    check_personal_thread_dispatch_allowed,
    verify_personal_integration_surface,
)
from src.dalton.services.personal_integration_trace import (
    REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER,
    read_thread_first_personal_segment,
)
from src.dalton.services.user_integrations_oauth import FreshAccessToken
from src.dalton.tools import calendar_tools as cal_tools_mod
from src.dalton.tools.calendar_tools import (
    _normalize_calendar_write_request,
    create_calendar_tools,
)
from src.observability import langsmith_tracer as tracer_mod
from src.observability.langsmith_tracer import NonRetainingTracer
from src.server.handlers.chat import request_prep as request_prep_mod
from src.server.models.chat import HITLDecision, HITLResponse
from src.server.services import automation_delivery as auto_delivery_mod
from src.server.services import automation_settlement as auto_settlement_mod
from src.server.services.history.reader import TurnAnchor
from src.server.services.history.replay import facts as replay_facts_mod
from src.server.services.history.replay import keys as replay_keys_mod
from src.server.services.history.replay import run_lane as replay_run_lane_mod
from src.server.services.history.replay import turn as replay_turn_mod
from src.server.services.history.slices import TurnSlice
from src.tools.secretary import dispatch as dispatch_mod
from src.tools.secretary._commands import hitl_confirm as real_hitl_confirm


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
    status: IntegrationStatus = IntegrationStatus.CONNECTED,
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
        status=status,
        refresh_inflight_gen=None,
        refresh_started_at=None,
        expires_at=now + timedelta(hours=1),
        last_refresh_at=now,
        needs_reauth_at=now if status == IntegrationStatus.NEEDS_REAUTH else None,
        last_error_code="invalid_grant" if status == IntegrationStatus.NEEDS_REAUTH else None,
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


class _ScriptedCursor:
    def __init__(self, rows: list[Any], *, rowcount: int = 1) -> None:
        self._rows = list(rows)
        self.executed: list[tuple[str, Any]] = []
        self.rowcount = rowcount

    async def __aenter__(self) -> _ScriptedCursor:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    async def fetchone(self) -> Any:
        return self._rows.pop(0) if self._rows else None

    async def fetchall(self) -> list[Any]:
        out = list(self._rows)
        self._rows.clear()
        return out


class _ScriptedConn:
    def __init__(self, rows: list[Any], *, rowcount: int = 1) -> None:
        self.cur = _ScriptedCursor(rows, rowcount=rowcount)

    def cursor(self, **_kw: Any) -> _ScriptedCursor:
        return self.cur

    @asynccontextmanager
    async def transaction(self):
        yield self


# ===========================================================================
# 1. P0: Backend channel gateway, automation delivery, webhook & notify guard
# ===========================================================================


@pytest.mark.asyncio
async def test_p0_backend_channel_gateway_personal_private_automation_fallback_and_redirect_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    exec_id = str(uuid4())

    monkeypatch.setattr(cg_db, "require_channel_platform_capability", lambda plat: None)
    monkeypatch.setattr(
        cg_db,
        "_verify_group_target_deliverable",
        create_autospec(cg_db._verify_group_target_deliverable, return_value=(True, None, {})),
    )

    auto_row_group = {
        "execution_id": exec_id,
        "user_id": "user-1",
        "thread_id": tid,
        "targets": [
            {"entry": "feishu:oc_group_1", "address": "feishu:oc_group_1", "ok": True},
        ],
        "status": "finishing",
    }

    # Case 1-3: SQL-routed cursor for authorize_and_claim_delivery (zero row-order fragility)
    class _GatewayClaimCursor:
        def __init__(self, *, is_private: bool, owner_uid: str = "user-1") -> None:
            self.is_private = is_private
            self.owner_uid = owner_uid
            self._next: Any = None
            self.executed: list[tuple[str, Any]] = []
            self.rowcount = 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            self.executed.append((sql, params))
            s = " ".join(sql.split()).lower()
            if "from dalton_channel_deliveries" in s:
                self._next = None
            elif "from dalton_channel_automation_runs" in s:
                self._next = auto_row_group
            elif "from dalton_channel_identities" in s:
                self._next = {"platform_user_id": "ou_alice_dm"}
            elif "from conversation_threads" in s:
                self._next = {
                    "has_personal_integration_data": self.is_private,
                    "owner_user_id": self.owner_uid,
                }
            else:
                self._next = None

        async def fetchone(self) -> Any:
            return self._next

    class _GatewayClaimConn:
        def __init__(self, *, is_private: bool, owner_uid: str = "user-1") -> None:
            self.cur = _GatewayClaimCursor(is_private=is_private, owner_uid=owner_uid)

        def cursor(self, **_kw: Any):
            return self.cur

        @asynccontextmanager
        async def transaction(self):
            yield self

    # Case 1: Normal content targeting feishu group on private thread -> DENIED
    res_deny = await cg_db.authorize_and_claim_delivery(
        delivery_key=f"auto:{exec_id}:feishu:oc_group_1",
        kind="automation",
        user_id="user-1",
        thread_id=tid,
        automation_execution_id=exec_id,
        platform="feishu",
        target="feishu:oc_group_1",
        conn=_GatewayClaimConn(is_private=True),
    )
    assert res_deny["claim_status"] == "denied"
    assert res_deny["code"] == "personal_integration_private_only"
    assert res_deny["has_personal_integration_data"] is True
    assert res_deny["redirect_dm_address"] == "feishu:@me"

    # Case 2: Sanitized `:personal_redirect_notice` to group -> CLAIMED with forced_text
    res_notice = await cg_db.authorize_and_claim_delivery(
        delivery_key=f"auto:{exec_id}:feishu:oc_group_1:personal_redirect_notice",
        kind="automation",
        user_id="user-1",
        thread_id=tid,
        automation_execution_id=exec_id,
        platform="feishu",
        target="feishu:oc_group_1",
        conn=_GatewayClaimConn(is_private=True),
    )
    assert res_notice["claim_status"] == "claimed"
    assert res_notice["personal_private_redirect_notice"] is True
    assert res_notice["forced_text"] == PERSONAL_INTEGRATION_GROUP_REDIRECT_NOTICE

    # Case 3: Automation DM redirect (`feishu:@me`) even when configured target was group -> CLAIMED!
    res_dm = await cg_db.authorize_and_claim_delivery(
        delivery_key=f"auto:{exec_id}:feishu:@me:personal_dm",
        kind="automation",
        user_id="user-1",
        thread_id=tid,
        automation_execution_id=exec_id,
        platform="feishu",
        target="feishu:@me",
        conn=_GatewayClaimConn(is_private=True),
    )
    assert res_dm["claim_status"] == "claimed"
    assert res_dm["target_address"] == "feishu:@me"

    # 1B. automation_delivery.finish_run forwards has_personal_integration_data=True
    posted_payloads: list[dict[str, Any]] = []

    async def _fake_gw_req(method: str, path: str, *, user_id: str, timeout: Any = None, body: Any = None, **kw: Any):
        posted_payloads.append({"method": method, "path": path, "body": dict(body or {})})
        return SimpleNamespace(status=200, data={"targets": []})

    monkeypatch.setattr(
        auto_delivery_mod.messaging,
        "gateway_request",
        create_autospec(auto_delivery_mod.messaging.gateway_request, side_effect=_fake_gw_req),
    )
    monkeypatch.setattr(
        guard_mod,
        "is_thread_personal_private",
        create_autospec(guard_mod.is_thread_personal_private, return_value=True),
    )

    await auto_delivery_mod.finish_run(
        {"automation_id": str(uuid4()), "user_id": "user-1", "name": "Daily"},
        exec_id,
        "completed",
        targets=[],
        final_text="私密日程摘要",
        thread_id=tid,
    )
    assert len(posted_payloads) == 1
    assert posted_payloads[0]["body"]["has_personal_integration_data"] is True

    # 1C. automation_settlement suppresses external webhook firing for private threads
    webhook_fired: list[Any] = []

    class _FakeWebhook:
        async def fire_event(self, *args: Any, **kwargs: Any) -> None:
            webhook_fired.append((args, kwargs))

    monkeypatch.setattr(auto_settlement_mod, "WebhookClient", _FakeWebhook)
    monkeypatch.setattr(
        "src.dalton.database.automation_recycle.automation_is_soft_deleted",
        create_autospec(
            __import__("src.dalton.database.automation_recycle", fromlist=["automation_is_soft_deleted"]).automation_is_soft_deleted,
            return_value=False,
        ),
    )
    monkeypatch.setattr(
        notify_dispatch_mod,
        "on_automation_settled",
        create_autospec(notify_dispatch_mod.on_automation_settled, return_value=True),
    )
    await auto_settlement_mod._after_settling(
        auto_settlement_mod.Outcome.COMPLETED,
        {"automation_id": str(uuid4()), "user_id": "user-1", "name": "Daily", "trigger_type": "cron"},
        exec_id,
        {"settled_from": "running"},
        tid,
        None,
        None,
        None,
        None,
        "Private calendar summary",
        False,
    )
    assert webhook_fired == []

    # 1D. dalton/notify/dispatch.py redirects group targets to owner DM and sends sanitized notice to group
    # Restore real on_automation_settled for 1D
    monkeypatch.undo()
    monkeypatch.setenv("DALTON_ENABLED", "1")
    monkeypatch.setenv("DALTON_PROFILE", "everflow")
    monkeypatch.setattr(
        guard_mod,
        "is_thread_personal_private",
        create_autospec(guard_mod.is_thread_personal_private, return_value=True),
    )
    monkeypatch.setattr(
        guard_mod,
        "get_thread_creator_user_id",
        create_autospec(guard_mod.get_thread_creator_user_id, return_value="user-1"),
    )
    monkeypatch.setattr(
        guard_mod,
        "verify_personal_dm_target_bound",
        create_autospec(guard_mod.verify_personal_dm_target_bound, return_value=True),
    )
    monkeypatch.setattr(
        guard_mod,
        "list_bound_personal_dm_targets",
        create_autospec(guard_mod.list_bound_personal_dm_targets, return_value=[]),
    )
    monkeypatch.setattr(
        notify_dispatch_mod,
        "_resolve_unit_managed_notify_gate",
        create_autospec(
            notify_dispatch_mod._resolve_unit_managed_notify_gate,
            return_value=(True, None, None, True),
        ),
    )
    monkeypatch.setattr(
        notify_dispatch_mod,
        "evaluate_user_notify_eligibility",
        create_autospec(
            notify_dispatch_mod.evaluate_user_notify_eligibility,
            return_value=(True, "ok", ["feishu:chat:oc_group_1"]),
        ),
    )
    posted_notifications: list[dict[str, Any]] = []

    async def _fake_post_notify(
        targets_arg: Any,
        text: str,
        card: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        ts = targets_arg if isinstance(targets_arg, list) else [targets_arg]
        for t in ts:
            posted_notifications.append({"target": t, "text": text, "card": card})
        return {"ok": True, "results": [{"target": t, "ok": True} for t in ts]}

    async def _always_true(*_a: Any, **_kw: Any) -> bool:
        return True

    async def _noop(*_a: Any, **_kw: Any) -> None:
        return None

    async def _routes(_uid: str | None) -> dict[str, list[str]]:
        return {"custom": ["feishu:chat:oc_group_1"], "general": ["feishu:chat:oc_group_1"]}

    async def _targets(_uid: str | None) -> list[str]:
        return ["feishu:user:ou_alice"]

    async def _empty_overrides(_uid: str | None) -> dict[str, bool]:
        return {}

    async def _no_verdict(*_a: Any, **_kw: Any) -> None:
        return None

    await notify_dispatch_mod.on_automation_settled(
        automation={"automation_id": str(uuid4()), "user_id": "user-1", "name": "Calendar Watch", "delivery_config": {}},
        execution_id=exec_id,
        thread_id=tid,
        final_text="【私人日程】11:00 体检",
        resolve_enabled_fn=_always_true,
        resolve_overrides_fn=_empty_overrides,
        resolve_targets_fn=_targets,
        resolve_routes_fn=_routes,
        get_verdict_fn=_no_verdict,
        claim_target_fn=_always_true,
        mark_target_fn=_noop,
        post_notify_fn=_fake_post_notify,
        frontend_url_fn=lambda: "https://app.example.com",
    )
    dm_posted = [e for e in posted_notifications if e["target"] == "feishu:user:ou_alice"]
    grp_posted = [e for e in posted_notifications if e["target"] == "feishu:chat:oc_group_1"]
    assert len(dm_posted) == 1
    assert "体检" in dm_posted[0]["text"]
    assert len(grp_posted) == 1
    assert grp_posted[0]["text"] == PERSONAL_INTEGRATION_GROUP_REDIRECT_NOTICE
    assert isinstance(grp_posted[0]["card"], dict)
    assert "体检" not in json.dumps(grp_posted[0]["card"], ensure_ascii=False)
    assert "体检" not in grp_posted[0]["text"]


# ===========================================================================
# 2. P1 / Grok Medium: Unconditional refusal of delegate_to_analyst
# ===========================================================================


@pytest.mark.asyncio
async def test_p1_delegate_to_analyst_refuses_all_personal_private_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    ws_own_hidden = str(uuid4())
    ws_colleague = str(uuid4())

    monkeypatch.setattr(
        guard_mod,
        "is_thread_personal_private",
        create_autospec(guard_mod.is_thread_personal_private, return_value=True),
    )

    for target_ws in (ws_own_hidden, ws_colleague):
        ok, err = await check_personal_thread_dispatch_allowed(
            source_thread_id=tid,
            caller_user_id="user-1",
            target_workspace_id=target_ws,
            conn=_ScriptedConn([]),
        )
        assert ok is False
        assert err is not None
        assert "不支持向其他工作区或线程转派任务" in err

    # End-to-end through dispatch.py refuses BEFORE hitl_confirm
    hitl_mock = create_autospec(real_hitl_confirm)
    monkeypatch.setattr(dispatch_mod, "hitl_confirm", hitl_mock)
    monkeypatch.setattr(
        dispatch_mod,
        "_owned_workspace",
        create_autospec(
            dispatch_mod._owned_workspace,
            return_value={"workspace_id": ws_own_hidden, "user_id": "user-1", "name": "Private WS"},
        ),
    )
    monkeypatch.setattr("src.config.settings.background_dispatch_requires_token", lambda: False)
    cmd = await dispatch_mod.dispatch(
        question="Analyze my private calendar meetings",
        config={"configurable": {"user_id": "user-1", "thread_id": tid, "workspace_id": str(uuid4())}},
        workspace_id=ws_own_hidden,
        thread_id=None,
        report_back=True,
        tool_call_id="tc-disp-priv",
    )
    msg_payload = json.loads(str(cmd.update["messages"][0].content))
    assert "不支持向其他工作区或线程转派任务" in msg_payload["error"]
    hitl_mock.assert_not_called()


# ===========================================================================
# 3. P1: Touch & disconnect share lifecycle_lock_key + touch_user_lock_key in same tx
# ===========================================================================


@pytest.mark.asyncio
async def test_p1_touch_and_disconnect_serialize_under_same_lifecycle_lock_and_tx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    ws_id = str(uuid4())

    # Shared in-memory advisory lock registry simulating Postgres pg_advisory_xact_lock
    locks: dict[int, asyncio.Lock] = {}
    lock_events: list[str] = []
    # Shared simulated DB row for dalton_user_integrations
    db_state: dict[str, Any] = {
        "status": "connected",
        "auth_epoch": 7,
        "provider_account_id": "acct-google-1",
        "touches_inserted": 0,
        "thread_flagged": False,
    }
    disconnect_entered = asyncio.Event()
    disconnect_can_commit = asyncio.Event()

    def _get_lock(key: int) -> asyncio.Lock:
        locks.setdefault(key, asyncio.Lock())
        return locks[key]

    class _LockingCursor:
        def __init__(self, actor: str, held: list[asyncio.Lock]) -> None:
            self.actor = actor
            self.held = held
            self._next_row: Any = None
            self.executed: list[tuple[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            self.executed.append((sql, params))
            norm_sql = " ".join(sql.split()).lower()
            if "pg_advisory_xact_lock" in norm_sql:
                key = int(params[0])
                lk = _get_lock(key)
                lock_events.append(f"{self.actor}:wait:{key}")
                await lk.acquire()
                self.held.append(lk)
                lock_events.append(f"{self.actor}:acquired:{key}")
                self._next_row = (None,)
            elif "from conversation_threads" in norm_sql and "for update" in norm_sql:
                self._next_row = {
                    "workspace_id": ws_id,
                    "thread_owner": "user-1",
                    "ws_owner": "user-1",
                    "thread_external_id": None,
                    "has_personal_integration_data": False,
                    "personal_purge_in_progress": False,
                    "is_shared": False,
                    "workspace_shared_at": None,
                    "ws_config": {},
                    "ws_status": "running",
                }
            elif "insert into dalton_thread_integration_touches" in norm_sql:
                db_state["touches_inserted"] += 1
                self._next_row = None
            elif "set has_personal_integration_data = true" in norm_sql:
                db_state["thread_flagged"] = True
                self._next_row = None
            else:
                self._next_row = None

        async def fetchone(self) -> Any:
            return self._next_row

    class _LockingConn:
        def __init__(self, actor: str) -> None:
            self.actor = actor
            self.held: list[asyncio.Lock] = []
            self.cursors: list[_LockingCursor] = []

        def cursor(self, **_kw: Any) -> _LockingCursor:
            c = _LockingCursor(self.actor, self.held)
            self.cursors.append(c)
            return c

        @asynccontextmanager
        async def transaction(self):
            try:
                yield self
            finally:
                while self.held:
                    self.held.pop().release()

    monkeypatch.setattr(guard_mod, "is_dalton_sharing_auth_enabled", lambda: False)
    monkeypatch.setattr(
        guard_mod.db,
        "get_connection",
        create_autospec(
            db_mod.get_connection,
            side_effect=lambda uid, prov, svc, **kw: _sample_conn(
                provider=prov,
                service=svc,
                status=IntegrationStatus(db_state["status"]),
                auth_epoch=db_state["auth_epoch"],
                account_id=db_state["provider_account_id"],
            ),
        ),
    )
    monkeypatch.setattr(
        guard_mod.db,
        "has_running_local_purge_for_service",
        create_autospec(db_mod.has_running_local_purge_for_service, return_value=False),
    )

    expected_life_key = lifecycle_lock_key("user-1", "google")
    expected_touch_key = touch_user_lock_key("user-1")

    async def _window_b_disconnect() -> None:
        conn_b = _LockingConn("disconnect")
        async with conn_b.transaction():
            async with conn_b.cursor() as cur:
                await cur.execute("SELECT pg_advisory_xact_lock(%s)", (expected_life_key,))
                await cur.execute("SELECT pg_advisory_xact_lock(%s)", (expected_touch_key,))
                disconnect_entered.set()
                await disconnect_can_commit.wait()
                # Window B marks the connection disconnect_pending before committing
                db_state["status"] = "disconnect_pending"
                db_state["auth_epoch"] = 8

    async def _window_a_touch_after_token() -> None:
        # Simulate token already acquired at epoch 7 before disconnect began;
        # wait until Window B holds the lifecycle lock, then attempt touch verification.
        await disconnect_entered.wait()
        conn_a = _LockingConn("touch")
        async with conn_a.transaction():
            # Release Window B slightly after Window A queues on the same lifecycle lock
            async def _release_b() -> None:
                await asyncio.sleep(0.02)
                disconnect_can_commit.set()

            releaser = asyncio.create_task(_release_b())
            try:
                await verify_personal_integration_surface(
                    {"configurable": {"user_id": "user-1", "thread_id": tid, "workspace_id": ws_id}},
                    provider="google",
                    service="calendar",
                    auth_epoch=7,
                    provider_account_id="acct-google-1",
                    expected_auth_epoch=7,
                    expected_provider_account_id="acct-google-1",
                    record_touch_now=True,
                    conn=conn_a,
                )
            finally:
                await releaser

    # Window A MUST fail with PersonalSurfaceDeniedError after serializing behind Window B
    b_task = asyncio.create_task(_window_b_disconnect())
    with pytest.raises(PersonalSurfaceDeniedError) as exc_info:
        await _window_a_touch_after_token()
    await b_task

    assert exc_info.value.is_purge_wait is True
    assert db_state["touches_inserted"] == 0
    assert db_state["thread_flagged"] is False
    # Verify exact lock ordering: lifecycle_lock_key acquired before touch_user_lock_key
    touch_waits = [ev for ev in lock_events if ev.startswith("touch:wait:")]
    assert touch_waits[0] == f"touch:wait:{expected_life_key}"


# ===========================================================================
# 4. P1 / Grok Medium: LangSmith & Multi-Turn Transcript Redaction
# ===========================================================================


@pytest.mark.asyncio
async def test_p1_langsmith_redacts_first_personal_llm_call_subsequent_tools_and_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_creates: list[dict[str, Any]] = []
    sent_updates: list[dict[str, Any]] = []

    class _FakeLangSmithClient:
        def create_run(self, **kwargs: Any) -> None:
            sent_creates.append(dict(kwargs))

        def update_run(self, run_id: Any, **kwargs: Any) -> None:
            sent_updates.append({"run_id": run_id, **kwargs})

    fake_ls_client = _FakeLangSmithClient()
    tracer = NonRetainingTracer(client=fake_ls_client, project_name="test-proj")

    # (a) First LLM call emits dalton_calendar tool call with private summary -> redacted in _on_llm_end BEFORE super()!
    llm_run_id = uuid4()
    llm_run = Run(
        id=llm_run_id,
        name="ChatOpenAI",
        run_type="llm",
        start_time=datetime.now(UTC),
        inputs={"messages": [[{"kwargs": {"content": "帮我把明天的体检日程改到下午三点"}}]]},
        outputs={
            "generations": [
                [
                    {
                        "text": "",
                        "message": {
                            "kwargs": {
                                "content": "好的，我先查询体检日程。",
                                "tool_calls": [
                                    {
                                        "id": "tc-1",
                                        "name": "dalton_calendar",
                                        "args": {
                                            "action": "list_events",
                                            "provider": "google",
                                            "query": "体检预约 SECRET_QUERY",
                                        },
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
        client=fake_ls_client,
    )
    tracer._on_llm_end(llm_run)
    assert tracer._personal_private[0] is True
    assert llm_run.extra.get("__disabled") is True
    assert sent_creates == []
    assert sent_updates == []

    # (b) Subsequent normal tool (bash) and errored tool must NEVER export create_run or update_run!
    bash_run_id = uuid4()
    bash_run = Run(
        id=bash_run_id,
        name="bash",
        run_type="tool",
        start_time=datetime.now(UTC),
        inputs={"command": "echo 'SECRET_CALENDAR_MEETING_3PM'"},
        extra={},
        serialized={},
        client=fake_ls_client,
    )
    tracer._start_trace(bash_run)
    tracer._on_tool_start(bash_run)
    bash_run.outputs = {"output": "SECRET_CALENDAR_MEETING_3PM"}
    tracer._on_tool_end(bash_run)

    err_run_id = uuid4()
    bash_err_run = Run(
        id=err_run_id,
        name="execute_code",
        run_type="tool",
        start_time=datetime.now(UTC),
        inputs={"code": "print('SECRET_ATTENDEE_EMAIL')"},
        extra={},
        serialized={},
        client=fake_ls_client,
    )
    tracer.run_map[str(err_run_id)] = bash_err_run
    settled_err_run = tracer._errored_tool_run(
        ValueError("failed parsing SECRET_ATTENDEE_EMAIL"), err_run_id
    )
    tracer._on_tool_error(settled_err_run)
    assert settled_err_run.extra.get("__disabled") is True
    assert sent_creates == []
    assert sent_updates == []

    # (c) New turn pre-seeds has_personal_integration_data=True via build_graph_config -> with_run_tracer
    from src.server.models.chat import ChatRequest

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
    assert cfg["metadata"]["has_personal_integration_data"] is True

    monkeypatch.setattr(
        tracer_mod,
        "_ambient_equivalent",
        lambda: NonRetainingTracer(client=_FakeLangSmithClient()),
    )
    traced_cfg = tracer_mod.with_run_tracer(cfg)
    callbacks = traced_cfg.get("callbacks") or []
    nt = next(cb for cb in callbacks if isinstance(cb, NonRetainingTracer))
    assert nt._personal_private[0] is True


@pytest.mark.asyncio
async def test_p1_transcript_multiturn_redaction_and_first_assistant_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn1: list[BaseMessage] = [
        HumanMessage(content="看看我明天有什么安排"),
        AIMessage(
            content="我先帮你在个人日历里查一下「明天体检安排」",
            tool_calls=[
                {
                    "id": "tc-cal-1",
                    "name": "dalton_calendar",
                    "args": {"action": "list_events", "query": "体检安排"},
                }
            ],
        ),
        ToolMessage(
            content=json.dumps({"events": [{"summary": "09:00 体检预约"}]}),
            tool_call_id="tc-cal-1",
            name="dalton_calendar",
        ),
        AIMessage(content="你明天上午 09:00 有体检预约。"),
    ]
    turn2: list[BaseMessage] = [
        HumanMessage(content="把刚才说的那个体检预约总结成一句话"),
        AIMessage(content="明天上午 09:00 体检预约，请空腹前往。"),
    ]

    directory = build_directory(turn1 + turn2, window=Window(0, ""))
    manifest = json.loads(directory.manifest)
    segs = manifest["segments"]
    assert len(segs) == 2
    assert segs[0].get("personal_tainted") is True
    assert segs[1].get("personal_tainted") is True

    seg1_text = directory.rendered[segs[0]["file"]].decode("utf-8")
    seg2_text = directory.rendered[segs[1]["file"]].decode("utf-8")

    # Turn 1 AI prose BEFORE first personal tool call is redacted, along with tool args & outputs
    assert "明天体检安排" not in seg1_text
    assert "体检预约" not in seg1_text
    assert REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER in seg1_text

    # Turn 2 NEVER called dalton_calendar, yet its human/AI content is ALSO redacted!
    assert "体检预约" not in seg2_text
    assert "空腹" not in seg2_text
    assert REDACTED_PERSONAL_INTEGRATION_PLACEHOLDER in seg2_text

    # segment_shape includes taint state so cached clean segments re-render when tainted
    s_clean = segment_shape(2, turn2, unit="turn", previous="", tool_names={}, personal_tainted=False)
    s_taint = segment_shape(2, turn2, unit="turn", previous="", tool_names={}, personal_tainted=True)
    assert s_clean != s_taint

    # DB-backed read_thread_first_personal_segment maps MIN(turn_index)=1 -> 1-based segment 2 (or 1 for task)
    tid = str(uuid4())
    seg_main = await read_thread_first_personal_segment(
        tid,
        is_task=False,
        conn=_ScriptedConn([(True,), (1,)]),
    )
    assert seg_main == 2
    seg_task = await read_thread_first_personal_segment(
        tid,
        is_task=True,
        conn=_ScriptedConn([(True,)]),
    )
    assert seg_task == 1


# ===========================================================================
# 5. P1: Write state split (real LangGraph StateGraph + MemorySaver tests)
# ===========================================================================


class _MiniState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


@pytest.mark.skip(reason="Batch 1 personal calendar is read-only; write actions deferred to next batch")
@pytest.mark.asyncio
async def test_p1_write_state_split_dispatched_unconfirmed_reconciles_without_duplicate_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    rid = str(uuid4())
    f_cli = create_autospec(FeishuCalendarClientProtocol, instance=True)

    created_calls: list[dict[str, Any]] = []

    async def _create_once(**kwargs: Any) -> dict[str, Any]:
        created_calls.append(dict(kwargs))
        body = kwargs.get("event_body") or {}
        return {
            "provider": "feishu",
            "calendar_id": "primary",
            "event_id": "feishu-ev-once",
            "summary": body.get("summary") or "Board Dinner",
            "start": {"dateTime": "2026-12-08T19:00:00+08:00"},
            "end": {"dateTime": "2026-12-08T21:00:00+08:00"},
            "status": "confirmed",
        }

    async def _list_reconcile(**kwargs: Any) -> dict[str, Any]:
        return {
            "items": [
                {
                    "provider": "feishu",
                    "calendar_id": "primary",
                    "event_id": "feishu-ev-once",
                    "summary": "Board Dinner",
                    "start": {"dateTime": "2026-12-08T19:00:00+08:00"},
                    "end": {"dateTime": "2026-12-08T21:00:00+08:00"},
                    "status": "confirmed",
                    "idempotency_key": created_calls[0].get("idempotency_key"),
                }
            ],
            "has_more": False,
            "page_token": None,
        }

    f_cli.create_event.side_effect = _create_once
    f_cli.list_events_page.side_effect = _list_reconcile

    approvals_by_id: dict[str, ActionApprovalRow] = {}
    approvals_by_stage: dict[str, ActionApprovalRow] = {}
    succeeded_update_attempts = {"count": 0}

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
        if status == "succeeded":
            succeeded_update_attempts["count"] += 1
            if succeeded_update_attempts["count"] == 1:
                # Crash right AFTER remote create_event succeeded!
                raise RuntimeError("simulated DB commit crash after remote write")
        row = approvals_by_id[aid]
        updated = dc_replace(
            row,
            status=status or row.status,
            elicitation_state=dict(elicitation_state) if isinstance(elicitation_state, dict) else (elicitation_state if elicitation_state is not None else row.elicitation_state),
        )
        approvals_by_id[aid] = updated
        if updated.idempotent_message_id:
            approvals_by_stage[updated.idempotent_message_id] = updated
        return True

    monkeypatch.setattr(cal_tools_mod, "effective_capabilities", lambda: frozenset({CapabilityId.FEISHU_CALENDAR}))
    monkeypatch.setattr(cal_tools_mod, "get_connector_providers_summary", lambda: {"feishu": {"ready": True}})
    monkeypatch.setattr(cal_tools_mod.db, "list_user_connections", create_autospec(db_mod.list_user_connections, return_value=[_sample_conn(provider="feishu", account_id="ou_1")]))
    monkeypatch.setattr(cal_tools_mod, "ensure_fresh_access_token", create_autospec(cal_tools_mod.ensure_fresh_access_token, return_value=_sample_token(provider="feishu", account_id="ou_1")))
    monkeypatch.setattr(
        cal_tools_mod,
        "verify_personal_integration_surface",
        create_autospec(
            guard_mod.verify_personal_integration_surface,
            return_value=PersonalSurfaceVerificationResult(
                caller_user_id="user-1", workspace_id=str(uuid4()), thread_id=tid, response_id=rid,
                turn_index=0, is_first_touch=False, first_touch_notice=None,
                platform="web", chat_type="web", is_dm_channel=False,
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

    tool_obj = create_calendar_tools(feishu_client=f_cli)[0]

    async def _node(state: _MiniState, config: RunnableConfig) -> Command:
        return await tool_obj.coroutine(
            action="create_event",
            provider="feishu",
            summary="Board Dinner",
            start_time="2026-12-08T19:00:00+08:00",
            end_time="2026-12-08T21:00:00+08:00",
            timezone="Asia/Shanghai",
            config=config,
            tool_call_id="tc-split-1",
        )

    builder = StateGraph(_MiniState)
    builder.add_node("cal", _node)
    builder.add_edge(START, "cal")
    builder.add_edge("cal", END)
    graph = builder.compile(checkpointer=MemorySaver())
    gcfg = {"configurable": {"thread_id": tid, "user_id": "user-1", "run_id": rid}}

    # Step 1: First invoke hits HITL interrupt
    res1 = await graph.ainvoke({"messages": [HumanMessage(content="Create Board Dinner")]}, config=gcfg)
    assert "__interrupt__" in res1
    intr_val = res1["__interrupt__"][0].value
    req = intr_val["action_requests"][0]

    # Step 2: Approve -> persists write_dispatched_unconfirmed (`send_ambiguous`), calls create_event ONCE, then crashes on local DB succeeded write!
    approve_cmd = Command(
        resume={
            "decisions": [
                {
                    "type": "approve",
                    "confirm_token": req["confirm_token"],
                    "confirmed_payload_sha256": req["payload_sha256"],
                }
            ]
        }
    )
    with pytest.raises(RuntimeError, match="simulated DB commit crash"):
        await graph.ainvoke(approve_cmd, config=gcfg)

    assert len(created_calls) == 1
    saved_row = approvals_by_id[req["approval_id"]]
    assert saved_row.status == "send_ambiguous"
    assert saved_row.elicitation_state["write_phase"] == "write_dispatched_unconfirmed"

    # Step 3: Retry resume -> reconciles via remote list_events WITHOUT calling create_event a second time!
    res3 = await graph.ainvoke(approve_cmd, config=gcfg)
    tool_msg = next(m for m in res3["messages"] if isinstance(m, ToolMessage))
    payload = json.loads(str(tool_msg.content))
    assert payload["ok"] is True
    assert payload["reconciled"] is True
    assert payload["event"]["event_id"] == "feishu-ev-once"
    assert len(created_calls) == 1  # CRITICAL: never duplicated!


@pytest.mark.skip(reason="Batch 1 personal calendar is read-only; write actions deferred to next batch")
@pytest.mark.asyncio
async def test_p1_write_state_split_awaiting_google_form_rechecks_version_including_form_on_get_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    rid = str(uuid4())
    g_cli = create_autospec(GoogleCalendarClientProtocol, instance=True)

    update_calls: list[dict[str, Any]] = []
    get_event_calls = {"count": 0}

    async def _google_mcp(
        *,
        access_token: str,
        tool_name: str,
        arguments: Any,
        request_state: str | None = None,
        input_responses: Any = None,
    ) -> dict[str, Any]:
        if tool_name == "get_event":
            get_event_calls["count"] += 1
            if get_event_calls["count"] == 1:
                # Pre-read before approval card
                return {
                    "event": {
                        "id": "g-ev-form",
                        "summary": "Original Title",
                        "start": {"dateTime": "2026-12-09T10:00:00+08:00"},
                        "end": {"dateTime": "2026-12-09T11:00:00+08:00"},
                        "updated": "2026-12-01T00:00:00Z",
                    }
                }
            if get_event_calls["count"] == 2:
                # Version re-check after Google form submission: get_event ITSELF asks for a Google form!
                raise GoogleMCPElicitationRequired(
                    request_state="rs-get-vercheck",
                    input_requests={"confirm_read": {"type": "string", "title": "Confirm read"}},
                    mcp_endpoint="https://mcp.google.com/calendar",
                    mcp_tool_name=tool_name,
                    mcp_arguments=arguments,
                )
            # After form submitted for version re-check, return DRIFTED updated timestamp!
            assert request_state == "rs-get-vercheck"
            return {
                "event": {
                    "id": "g-ev-form",
                    "summary": "Edited Externally",
                    "start": {"dateTime": "2026-12-09T10:00:00+08:00"},
                    "end": {"dateTime": "2026-12-09T11:00:00+08:00"},
                    "updated": "2026-12-01T00:05:00Z",
                }
            }
        if tool_name == "update_event":
            update_calls.append({"arguments": arguments, "request_state": request_state})
            if request_state is None:
                # First write attempt asks for Google form -> transitions to awaiting_google_form!
                raise GoogleMCPElicitationRequired(
                    request_state="rs-write-form-1",
                    input_requests={"confirm_write": {"type": "string", "title": "Allow update"}},
                    mcp_endpoint="https://mcp.google.com/calendar",
                    mcp_tool_name=tool_name,
                    mcp_arguments=arguments,
                )
            return {"event": {"id": "g-ev-form", "summary": "Should Not Happen"}}
        raise AssertionError(f"Unexpected MCP tool {tool_name}")

    g_cli.call_mcp_tool.side_effect = _google_mcp

    approvals_by_id: dict[str, ActionApprovalRow] = {}
    approvals_by_stage: dict[str, ActionApprovalRow] = {}

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
            elicitation_state=dict(elicitation_state) if isinstance(elicitation_state, dict) else (elicitation_state if elicitation_state is not None else row.elicitation_state),
        )
        approvals_by_id[aid] = updated
        if updated.idempotent_message_id:
            approvals_by_stage[updated.idempotent_message_id] = updated
        return True

    async def _mark_status(aid: str, status: str, *, caller_user_id: str | None = None, **kw: Any) -> bool:
        row = approvals_by_id[aid]
        updated = dc_replace(row, status=status)
        approvals_by_id[aid] = updated
        if updated.idempotent_message_id:
            approvals_by_stage[updated.idempotent_message_id] = updated
        return True

    monkeypatch.setattr(cal_tools_mod, "effective_capabilities", lambda: frozenset({CapabilityId.GOOGLE_CALENDAR}))
    monkeypatch.setattr(cal_tools_mod, "get_connector_providers_summary", lambda: {"google": {"ready": True}})
    monkeypatch.setattr(cal_tools_mod.db, "list_user_connections", create_autospec(db_mod.list_user_connections, return_value=[_sample_conn(provider="google")]))
    monkeypatch.setattr(cal_tools_mod, "ensure_fresh_access_token", create_autospec(cal_tools_mod.ensure_fresh_access_token, return_value=_sample_token(provider="google")))
    monkeypatch.setattr(
        cal_tools_mod,
        "verify_personal_integration_surface",
        create_autospec(
            guard_mod.verify_personal_integration_surface,
            return_value=PersonalSurfaceVerificationResult(
                caller_user_id="user-1", workspace_id=str(uuid4()), thread_id=tid, response_id=rid,
                turn_index=0, is_first_touch=False, first_touch_notice=None,
                platform="web", chat_type="web", is_dm_channel=False,
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
    monkeypatch.setattr(cal_tools_mod.db, "mark_action_approval_status", _mark_status)

    tool_obj = create_calendar_tools(google_client=g_cli)[0]

    async def _node(state: _MiniState, config: RunnableConfig) -> Command:
        return await tool_obj.coroutine(
            action="update_event",
            provider="google",
            event_id="g-ev-form",
            summary="New Title",
            config=config,
            tool_call_id="tc-form-ver",
        )

    builder = StateGraph(_MiniState)
    builder.add_node("cal", _node)
    builder.add_edge(START, "cal")
    builder.add_edge("cal", END)
    graph = builder.compile(checkpointer=MemorySaver())
    gcfg = {"configurable": {"thread_id": tid, "user_id": "user-1", "run_id": rid}}

    # 1. Pre-read + write approval card
    r1 = await graph.ainvoke({"messages": [HumanMessage(content="Rename event")]}, config=gcfg)
    req1 = r1["__interrupt__"][0].value["action_requests"][0]

    # 2. Approve write -> initial version check (get_event #2 asks for Google form!) -> pauses for vercheck form!
    r2 = await graph.ainvoke(
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
    assert "__interrupt__" in r2
    vercheck_form_req = r2["__interrupt__"][0].value["action_requests"][0]
    assert vercheck_form_req["type"] == "dalton_google_mcp_elicitation"
    assert vercheck_form_req["args"]["tool"] == "get_event"

    # 3. User submits vercheck Google form -> get_event #3 returns drifted `updated` timestamp -> aborts with event_version_changed!
    r3 = await graph.ainvoke(
        Command(
            resume={
                "decisions": [
                    {
                        "type": "approve",
                        "elicitation_responses": {
                            "confirm_read": {"action": "accept", "content": {"confirm_read": "YES"}}
                        },
                    }
                ]
            }
        ),
        config=gcfg,
    )
    tool_msg = next(m for m in r3["messages"] if isinstance(m, ToolMessage))
    payload = json.loads(str(tool_msg.content))
    assert payload["ok"] is False
    assert payload["error"] == "event_version_changed"
    # update_event was NEVER called because version check caught the external change even after Google form!
    assert len(update_calls) == 0


# ===========================================================================
# 6. P1: Approval resume re-checks saved provider capability (including provider="auto")
# ===========================================================================


@pytest.mark.skip(reason="Batch 1 personal calendar is read-only; write actions deferred to next batch")
@pytest.mark.asyncio
async def test_p1_approval_resume_rechecks_saved_provider_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    aid = str(uuid4())
    payload_sha = canonical_payload_sha256({"action": "create_event", "summary": "Sync"})
    google_approval = ActionApprovalRow(
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
        idempotent_message_id="tc:tc-auto-cap:write",
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        consumed_at=None,
        created_at=datetime.now(UTC),
        elicitation_state={
            "provider": "google",
            "calendar_id": "primary",
            "action": "create_event",
            "tz_name": "Asia/Shanghai",
            "write_payload": {"summary": "Sync", "start_time": "2026-12-10T10:00:00+08:00", "end_time": "2026-12-10T11:00:00+08:00"},
            "confirm_token": "tok-cap-1",
            "risk_level": "L1",
            "requires_second_confirm": False,
            "description": "新建日程",
            "card_args": {},
        },
    )

    # Google Calendar disabled while Feishu Calendar remains enabled!
    feishu_only = frozenset({CapabilityId.FEISHU_CALENDAR})
    monkeypatch.setattr("src.dalton.capabilities.effective_capabilities", lambda: feishu_only)
    monkeypatch.setattr(approval_mod.db, "get_action_approval", create_autospec(db_mod.get_action_approval, return_value=google_approval))

    # 6A. HTTP resume endpoint returns False (stamps scope_changed -> tool refuses cleanly)
    ok_resume = await verify_personal_integration_interrupt_resume(
        thread_id=tid,
        action_req={
            "type": "dalton_calendar_create_event",
            "approval_id": aid,
            "caller_user_id": "user-1",
            "confirm_token": "tok-cap-1",
        },
        answer=HITLResponse(decisions=[HITLDecision(type="approve", confirm_token="tok-cap-1")]),
        principal=SimpleNamespace(user_id="user-1", auth="session"),
    )
    assert ok_resume is False

    # 6B. Tool resume with provider="auto" ALSO refuses saved Google approval BEFORE consuming or calling Google!
    g_cli = create_autospec(GoogleCalendarClientProtocol, instance=True)
    consume_mock = create_autospec(db_mod.consume_action_approval, return_value=google_approval)
    mark_mock = create_autospec(db_mod.mark_action_approval_status, return_value=True)
    monkeypatch.setattr(cal_tools_mod, "effective_capabilities", lambda: feishu_only)
    monkeypatch.setattr(cal_tools_mod.db, "get_action_approval_by_stage_key", create_autospec(db_mod.get_action_approval_by_stage_key, return_value=google_approval))
    monkeypatch.setattr(cal_tools_mod.db, "list_action_approvals_by_stage_prefix", create_autospec(db_mod.list_action_approvals_by_stage_prefix, return_value=[]))
    monkeypatch.setattr(cal_tools_mod.db, "consume_action_approval", consume_mock)
    monkeypatch.setattr(cal_tools_mod.db, "mark_action_approval_status", mark_mock)
    monkeypatch.setattr(
        cal_tools_mod,
        "hitl_confirm",
        create_autospec(
            real_hitl_confirm,
            return_value=(True, {"decisions": [{"type": "approve", "confirm_token": "tok-cap-1"}]}),
        ),
    )

    tool_obj = create_calendar_tools(google_client=g_cli)[0]
    cmd = await tool_obj.coroutine(
        action="create_event",
        provider="auto",
        summary="Sync",
        start_time="2026-12-10T10:00:00+08:00",
        end_time="2026-12-10T11:00:00+08:00",
        config={"configurable": {"user_id": "user-1", "thread_id": tid}},
        tool_call_id="tc-auto-cap",
    )
    res = _tool_payload(cmd)
    assert res["ok"] is False
    assert res["error"] == "provider_capability_disabled"
    assert res["provider"] == "google"
    consume_mock.assert_not_awaited()
    g_cli.call_mcp_tool.assert_not_awaited()


# ===========================================================================
# 7. P1 / Grok Low: check_availability needs_reauth -> unknown; list_events pagination & truncation; docstring
# ===========================================================================


@pytest.mark.asyncio
async def test_p1_check_availability_needs_reauth_returns_unknown_and_list_events_paginates_and_truncates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    g_cli = create_autospec(GoogleCalendarClientProtocol, instance=True)
    f_cli = create_autospec(FeishuCalendarClientProtocol, instance=True)

    monkeypatch.setattr(
        cal_tools_mod,
        "effective_capabilities",
        lambda: frozenset({CapabilityId.GOOGLE_CALENDAR, CapabilityId.FEISHU_CALENDAR}),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "get_connector_providers_summary",
        lambda: {"google": {"ready": True}, "feishu": {"ready": True}},
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "ensure_fresh_access_token",
        create_autospec(
            cal_tools_mod.ensure_fresh_access_token,
            side_effect=lambda uid, p, svc, **kw: _sample_token(provider=p, account_id=f"acct-{p}"),
        ),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "verify_personal_integration_surface",
        create_autospec(
            guard_mod.verify_personal_integration_surface,
            return_value=PersonalSurfaceVerificationResult(
                caller_user_id="user-1", workspace_id=str(uuid4()), thread_id=tid, response_id=None,
                turn_index=0, is_first_touch=False, first_touch_notice=None,
                platform="web", chat_type="web", is_dm_channel=False,
            ),
        ),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "claim_first_touch_notice_if_needed",
        create_autospec(guard_mod.claim_first_touch_notice_if_needed, return_value=None),
    )
    monkeypatch.setattr(
        cal_tools_mod.db,
        "get_action_approval_by_stage_key",
        create_autospec(db_mod.get_action_approval_by_stage_key, return_value=None),
    )

    orig_list_user_connections = db_mod.list_user_connections

    # 7A. check_availability with Google connected + Feishu needs_reauth -> status="unknown", free=[]
    monkeypatch.setattr(
        cal_tools_mod.db,
        "list_user_connections",
        create_autospec(
            orig_list_user_connections,
            return_value=[
                _sample_conn(provider="google", status=IntegrationStatus.CONNECTED),
                _sample_conn(provider="feishu", status=IntegrationStatus.NEEDS_REAUTH, account_id="ou_f"),
            ],
        ),
    )
    g_cli.call_mcp_tool.return_value = {"events": []}

    tool_obj = create_calendar_tools(google_client=g_cli, feishu_client=f_cli)[0]
    cfg = {"configurable": {"user_id": "user-1", "thread_id": tid}}
    avail_cmd = await tool_obj.coroutine(
        action="check_availability",
        provider="auto",
        time_min="2026-12-11T09:00:00+08:00",
        time_max="2026-12-11T18:00:00+08:00",
        config=cfg,
        tool_call_id="tc-avail-reauth",
    )
    avail_res = _tool_payload(avail_cmd)
    assert avail_res["ok"] is True
    assert avail_res["availability"]["status"] == "unknown"
    assert avail_res["availability"]["coverage_complete"] is False
    assert avail_res["availability"]["free"] == []
    assert avail_res["availability"]["incomplete_providers"]["feishu"] == "needs_reauth"
    assert avail_res["availability"]["reauth_providers"] == ["feishu"]
    assert "重新授权" in avail_res["availability"]["unknown_reason"]

    # 7B. list_events paginates across multiple pages AND marks truncated=True when exceeding page cap
    monkeypatch.setattr(
        cal_tools_mod.db,
        "list_user_connections",
        create_autospec(
            orig_list_user_connections,
            return_value=[
                _sample_conn(provider="google", status=IntegrationStatus.CONNECTED),
                _sample_conn(provider="feishu", status=IntegrationStatus.CONNECTED, account_id="ou_f"),
            ],
        ),
    )

    async def _google_paged(*, access_token: str, tool_name: str, arguments: Any, **kw: Any) -> dict[str, Any]:
        tok = arguments.get("pageToken")
        page_num = int(tok.split("-")[1]) if tok else 1
        return {
            "events": [
                {
                    "id": f"g-ev-p{page_num}",
                    "summary": f"Google Page {page_num}",
                    "start": {"dateTime": "2026-12-12T09:00:00+08:00"},
                    "end": {"dateTime": "2026-12-12T09:30:00+08:00"},
                }
            ],
            "nextPageToken": f"pg-{page_num + 1}",  # never ends -> hits 20-page cap!
        }

    feishu_pages = {
        None: {
            "items": [
                {
                    "provider": "feishu",
                    "provider_label": "[飞书日历]",
                    "calendar_id": "primary",
                    "event_id": "f-p1",
                    "summary": "Feishu Page 1",
                    "start": {"dateTime": "2026-12-12T10:00:00+08:00"},
                    "end": {"dateTime": "2026-12-12T10:30:00+08:00"},
                }
            ],
            "page_token": "ftok-2",
            "has_more": True,
        },
        "ftok-2": {
            "items": [
                {
                    "provider": "feishu",
                    "provider_label": "[飞书日历]",
                    "calendar_id": "primary",
                    "event_id": "f-p2",
                    "summary": "Feishu Page 2",
                    "start": {"dateTime": "2026-12-12T11:00:00+08:00"},
                    "end": {"dateTime": "2026-12-12T11:30:00+08:00"},
                }
            ],
            "page_token": None,
            "has_more": False,
        },
    }

    async def _feishu_paged(*, page_token: str | None = None, **kw: Any):
        return feishu_pages[page_token]

    g_cli.call_mcp_tool.side_effect = _google_paged
    f_cli.list_events_page.side_effect = _feishu_paged

    list_cmd = await tool_obj.coroutine(
        action="list_events",
        provider="auto",
        time_min="2026-12-12T00:00:00+08:00",
        time_max="2026-12-12T23:59:59+08:00",
        config=cfg,
        tool_call_id="tc-list-paged",
    )
    list_res = _tool_payload(list_cmd)
    assert list_res["ok"] is True
    assert list_res["truncated"] is True
    assert "google" in list_res["truncated_providers"]
    assert "未列全" in list_res["truncation_notice"]
    event_ids = {ev["event_id"] for ev in list_res["events"]}
    assert "f-p1" in event_ids and "f-p2" in event_ids
    assert "g-ev-p1" in event_ids and "g-ev-p20" in event_ids

    # 7C. Docstring documents unknown availability and truncated list_events
    desc = str(tool_obj.description or "")
    assert "unknown" in desc
    assert "无法确认空闲" in desc
    assert "truncated" in desc


# ===========================================================================
# 8. P2: Approval payload delete start/end time & explicit clear/toggle fields
# ===========================================================================


def test_p2_approval_payload_delete_times_and_explicit_clear_fields() -> None:
    existing = {
        "event_id": "ev-9",
        "summary": "Client Kickoff",
        "start": {"dateTime": "2026-12-15T14:00:00+08:00"},
        "end": {"dateTime": "2026-12-15T15:00:00+08:00"},
        "location": "Room 302",
        "description": "Original agenda",
    }

    # delete_event carries original start_time / end_time
    del_norm, del_err = _normalize_calendar_write_request(
        action="delete_event",
        provider="google",
        calendar_id="primary",
        event_id="ev-9",
        summary=None,
        start_time=None,
        end_time=None,
        all_day=False,
        tz_name="Asia/Shanghai",
        description=None,
        location=None,
        attendees=None,
        reminders=None,
        use_default_reminders=None,
        owner_email="alice@example.com",
        account_email="alice@example.com",
        existing_event=existing,
    )
    assert del_err is None and del_norm is not None
    del_card = del_norm["card_args"]
    assert del_card["start_time"] == "2026-12-15T14:00:00+08:00"
    assert del_card["end_time"] == "2026-12-15T15:00:00+08:00"
    assert del_card["before"]["start"]["dateTime"] == "2026-12-15T14:00:00+08:00"
    assert del_card["before"]["end"]["dateTime"] == "2026-12-15T15:00:00+08:00"

    # update_event clearing description/location, emptying reminders, toggling use_default_reminders
    upd_norm, upd_err = _normalize_calendar_write_request(
        action="update_event",
        provider="google",
        calendar_id="primary",
        event_id="ev-9",
        summary=None,
        start_time=None,
        end_time=None,
        all_day=False,
        tz_name="Asia/Shanghai",
        description="",
        location="",
        attendees=None,
        reminders=[],
        use_default_reminders=False,
        owner_email="alice@example.com",
        account_email="alice@example.com",
        existing_event=existing,
    )
    assert upd_err is None and upd_norm is not None
    upd_payload = upd_norm["write_payload"]
    upd_card = upd_norm["card_args"]
    assert upd_payload["description"] == ""
    assert upd_payload["overrideReminders"] == []
    assert upd_payload["useDefaultReminders"] is False
    assert upd_card["description"] == ""
    assert upd_card["description_set"] is True
    assert upd_card["location"] == ""
    assert upd_card["location_set"] is True
    assert upd_card["reminders"] == []
    assert upd_card["reminders_set"] is True
    assert upd_card["use_default_reminders"] is False


# ===========================================================================
# 9. P2: First-touch privacy notice persisted on turn, emitted live, replayed on owning turn
# ===========================================================================


@pytest.mark.asyncio
async def test_p2_first_touch_notice_persisted_on_turn_replayed_and_visible_on_cancelled_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    ws_id = str(uuid4())
    live_events: list[dict[str, Any]] = []

    monkeypatch.setattr(guard_mod, "is_dalton_sharing_auth_enabled", lambda: False)
    monkeypatch.setattr(guard_mod, "is_home_row", lambda ws: True)
    monkeypatch.setattr(
        guard_mod,
        "_emit_live_first_touch_notice",
        lambda ev: live_events.append(dict(ev)),
    )
    monkeypatch.setattr(
        guard_mod.db,
        "get_connection",
        create_autospec(db_mod.get_connection, return_value=_sample_conn(provider="google", auth_epoch=7)),
    )
    monkeypatch.setattr(
        guard_mod.db,
        "has_running_local_purge_for_service",
        create_autospec(db_mod.has_running_local_purge_for_service, return_value=False),
    )
    monkeypatch.setattr(
        guard_mod.db,
        "record_touch",
        create_autospec(db_mod.record_touch, return_value=True),
    )

    resp_uuid = str(uuid4())
    conn = _ScriptedConn(
        [
            # conversation_threads FOR UPDATE: has_personal_integration_data=False (first touch!)
            {
                "workspace_id": ws_id,
                "thread_owner": "user-1",
                "ws_owner": "user-1",
                "thread_external_id": None,
                "has_personal_integration_data": False,
                "personal_purge_in_progress": False,
                "is_shared": False,
                "workspace_shared_at": None,
                "ws_config": {},
                "ws_status": "running",
            },
            # _resolve_turn_index_tx -> 1
            {"turn_index": 1},
        ]
    )

    surf = await verify_personal_integration_surface(
        {"configurable": {"user_id": "user-1", "thread_id": tid, "workspace_id": ws_id, "run_id": resp_uuid}},
        provider="google",
        service="calendar",
        auth_epoch=7,
        provider_account_id="acct-google-1",
        expected_auth_epoch=7,
        expected_provider_account_id="acct-google-1",
        record_touch_now=True,
        conn=conn,
    )
    assert surf.is_first_touch is True
    assert surf.first_touch_notice == FIRST_TOUCH_NOTICE_CALENDAR
    assert len(live_events) == 1
    assert live_events[0]["turn_index"] == 1
    assert live_events[0]["message"] == FIRST_TOUCH_NOTICE_CALENDAR
    # Verify metadata.personal_first_touch_event SQL ran inside the same transaction
    assert any("personal_first_touch_event" in sql for sql, _ in conn.cur.executed)

    # Replay projection emits personal_integration_notice ONLY on owning turn_index=1 (even if turn had no assistant text / cancelled approval)
    ft_event = dict(live_events[0])
    resp0 = {"conversation_response_id": "r0", "turn_index": 0, "status": "completed"}
    resp1 = {"conversation_response_id": "r1", "turn_index": 1, "status": "completed"}
    inputs = replay_turn_mod.Inputs(
        thread_id=tid,
        turn_indexes=[0, 1],
        queries_by_turn={},
        responses_by_turn={0: resp0, 1: resp1},
        usage_by_response={},
        provenance_by_response={},
        run_facts=replay_facts_mod.RunFacts.of([]),
        open_runs={},
        personal_first_touch_event=ft_event,
    )
    anchor0 = TurnAnchor(turn_ordinal=0, input_checkpoint_id="cp0", tail_checkpoint_id="cp0", turn_index=0)
    anchor1 = TurnAnchor(turn_ordinal=1, input_checkpoint_id="cp1", tail_checkpoint_id="cp1", turn_index=1)
    turn0 = TurnSlice(anchor=anchor0, messages=[HumanMessage(content="hi")], new_ui_records=[])
    turn1 = TurnSlice(anchor=anchor1, messages=[HumanMessage(content="update meeting")], new_ui_records=[])

    lane = create_autospec(replay_run_lane_mod.RunLane, instance=True)
    lane.project.return_value = ([], True, {})
    items0, _ = replay_turn_mod.project_turn(inputs, 0, turn0, resp0, lane)
    items1, _ = replay_turn_mod.project_turn(inputs, 1, turn1, resp1, lane)

    notices0 = [it for it in items0 if it.get("event") == "personal_integration_notice"]
    notices1 = [it for it in items1 if it.get("event") == "personal_integration_notice"]
    assert notices0 == []
    assert len(notices1) == 1
    assert notices1[0]["data"]["message"] == FIRST_TOUCH_NOTICE_CALENDAR

    # lines_key changes when personal_first_touch_event lands on turn 1
    inputs_without = replay_turn_mod.Inputs(
        thread_id=tid,
        turn_indexes=[0, 1],
        queries_by_turn={},
        responses_by_turn={0: resp0, 1: resp1},
        usage_by_response={},
        provenance_by_response={},
        run_facts=replay_facts_mod.RunFacts.of([]),
        open_runs={},
        personal_first_touch_event=None,
    )
    k_without = replay_keys_mod.lines_key(inputs_without, 1, anchor1, resp1)
    k_with = replay_keys_mod.lines_key(inputs, 1, anchor1, resp1)
    assert k_without != k_with


# ===========================================================================
# 10. P2 / Grok Low: dalton_calendar reuses backend ready summary
# ===========================================================================


@pytest.mark.asyncio
async def test_p2_provider_not_ready_returns_admin_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cal_tools_mod,
        "effective_capabilities",
        lambda: frozenset({CapabilityId.GOOGLE_CALENDAR}),
    )
    monkeypatch.setattr(
        cal_tools_mod,
        "get_connector_providers_summary",
        lambda: {"google": {"ready": False, "credentials_ready": False}},
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
        tool_call_id="tc-not-ready",
    )
    res = _tool_payload(cmd)
    assert res["ok"] is False
    assert res["error"] == "provider_not_ready"
    assert res["ready"] is False
    assert "管理员" in res["message"]
    assert "/settings?tab=integrations" not in res["message"]
