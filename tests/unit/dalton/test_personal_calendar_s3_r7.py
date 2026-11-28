"""Round 7 unit tests for Personal Calendar Batch 1 Step 3 (final 4 review fixes).

Covers:
1. Notification retry shares the personal-DM redirect rule with initial settlement and
   re-verifies the thread creator's current channel binding before every send attempt.
2. Google Calendar OAuth scope merging excludes legacy calendar write scopes when
   active same-client sibling connections (e.g. Gmail) still carry them, while existing
   write-granted tokens remain readable.
3. LangSmith tracing mutual exclusion applies strictly to ``calendar`` readiness and
   authorization; ``mail`` connectors (Gmail & Feishu Mail) remain ready for connect
   and re-authorization even when LangSmith tracing is enabled.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json
from typing import Any
from unittest.mock import create_autospec
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src.dalton import capabilities as cap_mod
from src.dalton.app import integrations_router as integ_router_mod
from src.dalton.database import automation_notifications as auto_notify_db
from src.dalton.database import user_integrations as db_mod
from src.dalton.database.user_integrations import IntegrationRow, IntegrationStatus
from src.dalton.deployment_credentials.snapshot import CredentialGroupResult
from src.dalton.integrations.constants import (
    GOOGLE_SCOPE_CALENDAR_EVENTS,
    GOOGLE_SCOPE_CALENDAR_EVENTS_RO,
    GOOGLE_SCOPE_CALENDAR_FULL,
    GOOGLE_SCOPE_CALENDAR_LIST_RO,
    GOOGLE_SCOPE_GMAIL_READONLY,
    scope_satisfied,
)
from src.dalton.llm.model_prefs import (
    resolve_dalton_notify_enabled,
    resolve_dalton_notify_overrides,
    resolve_dalton_notify_routes,
    resolve_dalton_notify_targets,
)
from src.dalton.notify import dispatch as notify_dispatch_mod
from src.dalton.services import personal_integration_guard as guard_mod
from src.dalton.services import user_integrations_oauth as oauth_mod
from src.dalton.services.user_integrations_oauth import _check_required_scopes
from src.dalton.tools import calendar_tools as cal_tools_mod


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DALTON_ENABLED", "1")
    monkeypatch.setenv("DALTON_PROFILE", "everflow")
    for var in (
        "LANGSMITH_TRACING",
        "LANGSMITH_TRACING_V2",
        "LANGCHAIN_TRACING_V2",
        "LANGCHAIN_TRACING",
    ):
        monkeypatch.delenv(var, raising=False)


class _RetryFakeCursor:
    def __init__(
        self,
        *,
        candidate_rows: list[dict[str, Any]],
        threads: dict[str, dict[str, Any]],
        identities: set[tuple[str, str, str]],
    ) -> None:
        self._candidate_rows = candidate_rows
        self._threads = threads
        self._identities = identities
        self._last_rows: list[Any] = []
        self.rowcount = 0

    async def __aenter__(self) -> _RetryFakeCursor:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    async def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        low = " ".join(sql.lower().split())
        self._last_rows = []
        self.rowcount = 0
        if "from dalton_notify_deliveries d" in low and "left join automations a" in low:
            self._last_rows = list(self._candidate_rows)
            self.rowcount = len(self._last_rows)
        elif low.startswith("update dalton_notify_deliveries") and "returning attempt_count" in low:
            exec_id = str(params[0]) if params else ""
            target = str(params[2]) if params and len(params) > 2 else ""
            for r in self._candidate_rows:
                if r["execution_id"] == exec_id and r["target"] == target:
                    r["status"] = "pending"
                    r["attempt_count"] = int(r.get("attempt_count") or 0) + 1
                    self._last_rows = [
                        {
                            "attempt_count": r["attempt_count"],
                            "payload_json": dict(r["payload_json"]),
                        }
                    ]
                    self.rowcount = 1
                    break
        elif "from conversation_threads" in low:
            tid = str(params[0]) if params else ""
            row = self._threads.get(tid)
            if row is not None:
                self._last_rows = [dict(row)]
                self.rowcount = 1
        elif "from dalton_channel_identities" in low:
            if params and len(params) == 3:
                uid, plat, puid = str(params[0]), str(params[1]), str(params[2])
                if (uid, plat, puid) in self._identities:
                    self._last_rows = [(1,)]
                    self.rowcount = 1
        elif "from dalton_channel_deliveries" in low:
            self._last_rows = []

    async def fetchone(self) -> Any:
        return self._last_rows[0] if self._last_rows else None

    async def fetchall(self) -> list[Any]:
        return list(self._last_rows)


class _RetryFakeConn:
    def __init__(self, cur: _RetryFakeCursor) -> None:
        self._cur = cur

    def cursor(self, **_kw: Any) -> _RetryFakeCursor:
        return self._cur


def _make_integration_row(
    *,
    provider: str = "google",
    service: str = "mail",
    oauth_client_id: str = "123456-abc.apps.googleusercontent.com",
    granted_scopes: str = "",
    status: IntegrationStatus = IntegrationStatus.CONNECTED,
) -> IntegrationRow:
    now = datetime.now(UTC)
    return IntegrationRow(
        user_id="user-1",
        provider=provider,
        service=service,
        oauth_client_id=oauth_client_id,
        provider_account_id="sub-alice-1",
        provider_union_id=None,
        provider_email="alice@example.com",
        granted_scopes=granted_scopes,
        auth_epoch=1,
        authorized_at=now,
        token_generation=1,
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


# ===========================================================================
# 2. Notification retry shares personal-DM redirect & verifies current binding
# ===========================================================================


@pytest.mark.asyncio
async def test_r7_notify_retry_shares_personal_dm_redirect_and_verifies_current_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = str(uuid4())
    auto_id = str(uuid4())
    exec_id = str(uuid4())
    delivery_id = str(uuid4())
    group_target = "feishu:chat:oc_group_only"
    alice_dm = "feishu:user:ou_alice_bound"

    def _make_failed_dm_row() -> dict[str, Any]:
        return {
            "delivery_id": delivery_id,
            "execution_id": exec_id,
            "automation_id": auto_id,
            "user_id": "user-1",
            "channel": "feishu",
            "target": alice_dm,
            "status": "failed",
            "attempt_count": 1,
            "error_code": "http_502",
            "updated_at": datetime.now(UTC) - timedelta(minutes=2),
            "automation_name": "Group Only Calendar Brief",
            "automation_metadata": {},
            "automation_workspace_id": None,
            "automation_user_id": "user-1",
            "payload_json": {
                "source": "automation_completion",
                "transport": "gateway",
                "thread_id": tid,
                "personal_private_dm_redirect": True,
                "text": "【私人日程】14:00 牙医复诊",
                "card": {
                    "schema": "2.0",
                    "body": {"elements": [{"tag": "markdown", "content": "14:00 牙医复诊"}]},
                },
            },
        }

    routes = {f"automation:{auto_id}": [group_target]}
    enabled_fn = create_autospec(resolve_dalton_notify_enabled, return_value=True)
    overrides_fn = create_autospec(resolve_dalton_notify_overrides, return_value={})
    targets_fn = create_autospec(resolve_dalton_notify_targets, return_value=[alice_dm])
    routes_fn = create_autospec(resolve_dalton_notify_routes, return_value=routes)
    admin_fn = create_autospec(notify_dispatch_mod._default_notify_admin, return_value=True)

    monkeypatch.setattr(
        notify_dispatch_mod,
        "_retry_parent_is_deleted",
        create_autospec(notify_dispatch_mod._retry_parent_is_deleted, return_value=False),
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
    monkeypatch.setattr(
        auto_notify_db,
        "reconcile_uncertain_automation_notify_deliveries",
        create_autospec(
            auto_notify_db.reconcile_uncertain_automation_notify_deliveries,
            return_value=0,
        ),
    )

    async def _noop_reconcile(**_kw: Any) -> int:
        return 0

    # --- Case A: Bound DM on a group-only automation retries and succeeds (NOT skipped as target_removed) ---
    row_a = _make_failed_dm_row()
    cur_a = _RetryFakeCursor(
        candidate_rows=[row_a],
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
    async def _fake_conn_a(conn: Any = None):
        yield conn if conn is not None else _RetryFakeConn(cur_a)

    monkeypatch.setattr(notify_dispatch_mod, "get_db_connection", _fake_conn_a)
    monkeypatch.setattr(guard_mod, "get_db_connection", _fake_conn_a)

    plans_a: list[dict[str, Any]] = []

    async def _gw_send_a(
        did: str,
        *,
        target: str,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        _ = timeout_s
        plan = await auto_notify_db.decide_automation_notification_plan(
            cur_a,
            row_a,
            delivery_id=did,
            resolve_enabled_fn=enabled_fn,
            resolve_overrides_fn=overrides_fn,
            resolve_targets_fn=targets_fn,
            resolve_routes_fn=routes_fn,
            is_admin_fn=admin_fn,
        )
        plans_a.append(plan)
        assert plan["authorized"] is True
        return {"ok": True, "results": [{"target": target, "ok": True}]}

    orig_post_gw = notify_dispatch_mod.post_automation_gateway_notify
    gw_spy_a = create_autospec(
        orig_post_gw,
        side_effect=_gw_send_a,
    )
    monkeypatch.setattr(
        notify_dispatch_mod, "post_automation_gateway_notify", gw_spy_a
    )
    mark_spy_a = create_autospec(notify_dispatch_mod.mark_target_delivery, return_value=True)

    sent_a = await notify_dispatch_mod.retry_failed_notify_deliveries(
        reconcile_data_tasks_fn=_noop_reconcile,
        mark_delivery_fn=mark_spy_a,
        resolve_enabled_fn=enabled_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        resolve_routes_fn=routes_fn,
        is_admin_fn=admin_fn,
    )
    assert sent_a == 1
    assert gw_spy_a.await_count == 1
    assert len(plans_a) == 1
    assert plans_a[0]["requested_target"] == alice_dm
    assert any(
        call.kwargs.get("status") == "sent" for call in mark_spy_a.await_args_list
    )

    # --- Case B: Creator unbinds DM before retry -> skipped with personal_dm_recipient_unverified, never sent ---
    row_b = _make_failed_dm_row()
    cur_b = _RetryFakeCursor(
        candidate_rows=[row_b],
        threads={
            tid: {
                "has_personal_integration_data": True,
                "user_id": "user-1",
                "thread_user_id": "user-1",
            }
        },
        identities=set(),  # unbound!
    )

    @asynccontextmanager
    async def _fake_conn_b(conn: Any = None):
        yield conn if conn is not None else _RetryFakeConn(cur_b)

    monkeypatch.setattr(notify_dispatch_mod, "get_db_connection", _fake_conn_b)
    monkeypatch.setattr(guard_mod, "get_db_connection", _fake_conn_b)

    gw_spy_b = create_autospec(orig_post_gw)
    monkeypatch.setattr(
        notify_dispatch_mod, "post_automation_gateway_notify", gw_spy_b
    )
    mark_spy_b = create_autospec(notify_dispatch_mod.mark_target_delivery, return_value=True)

    sent_b = await notify_dispatch_mod.retry_failed_notify_deliveries(
        reconcile_data_tasks_fn=_noop_reconcile,
        mark_delivery_fn=mark_spy_b,
        resolve_enabled_fn=enabled_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        resolve_routes_fn=routes_fn,
        is_admin_fn=admin_fn,
    )
    assert sent_b == 0
    assert gw_spy_b.await_count == 0
    assert any(
        call.kwargs.get("status") == "skipped"
        and call.kwargs.get("reason") == "personal_dm_recipient_unverified"
        for call in mark_spy_b.await_args_list
    )

    # --- Case C: User removes the group target from the route -> skipped as target_removed ---
    row_c = _make_failed_dm_row()
    cur_c = _RetryFakeCursor(
        candidate_rows=[row_c],
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
    async def _fake_conn_c(conn: Any = None):
        yield conn if conn is not None else _RetryFakeConn(cur_c)

    monkeypatch.setattr(notify_dispatch_mod, "get_db_connection", _fake_conn_c)
    monkeypatch.setattr(guard_mod, "get_db_connection", _fake_conn_c)

    gw_spy_c = create_autospec(orig_post_gw)
    monkeypatch.setattr(
        notify_dispatch_mod, "post_automation_gateway_notify", gw_spy_c
    )
    mark_spy_c = create_autospec(notify_dispatch_mod.mark_target_delivery, return_value=True)
    empty_routes_fn = create_autospec(
        resolve_dalton_notify_routes,
        return_value={f"automation:{auto_id}": []},
    )

    sent_c = await notify_dispatch_mod.retry_failed_notify_deliveries(
        reconcile_data_tasks_fn=_noop_reconcile,
        mark_delivery_fn=mark_spy_c,
        resolve_enabled_fn=enabled_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        resolve_routes_fn=empty_routes_fn,
        is_admin_fn=admin_fn,
    )
    assert sent_c == 0
    assert gw_spy_c.await_count == 0
    assert any(
        call.kwargs.get("status") == "skipped"
        and call.kwargs.get("reason") == "target_removed"
        for call in mark_spy_c.await_args_list
    )


# ===========================================================================
# 3. Calendar OAuth excludes legacy write scopes when merging same-client rows
# ===========================================================================


@pytest.mark.asyncio
async def test_r7_google_calendar_oauth_excludes_legacy_write_scopes_when_merging_same_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_id = "123456-abc.apps.googleusercontent.com"
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
                "DALTON_GOOGLE_OAUTH_CLIENT_ID": client_id,
                "DALTON_GOOGLE_OAUTH_CLIENT_SECRET": "g-secret",
            },
        ),
    )

    # Existing active same-client Gmail row still carrying legacy calendar write scopes.
    existing_gmail = _make_integration_row(
        provider="google",
        service="mail",
        oauth_client_id=client_id,
        granted_scopes=(
            f"openid email {GOOGLE_SCOPE_GMAIL_READONLY} "
            f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS} "
            f"{GOOGLE_SCOPE_CALENDAR_FULL}"
        ),
        status=IntegrationStatus.CONNECTED,
    )
    monkeypatch.setattr(
        oauth_mod.db,
        "list_user_connections",
        create_autospec(db_mod.list_user_connections, return_value=[existing_gmail]),
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

    started = await oauth_mod.start_oauth_authorization(
        user_id="user-1",
        provider="google",
        service="calendar",
        auth_mode="local",
    )
    query = parse_qs(urlsplit(started.authorize_url).query)
    requested_scopes = set(query["scope"][0].split())

    # Strictly read-only calendar scopes + preserved Gmail scope; zero calendar write scopes.
    assert GOOGLE_SCOPE_CALENDAR_LIST_RO in requested_scopes
    assert GOOGLE_SCOPE_CALENDAR_EVENTS_RO in requested_scopes
    assert GOOGLE_SCOPE_GMAIL_READONLY in requested_scopes
    assert GOOGLE_SCOPE_CALENDAR_EVENTS not in requested_scopes
    assert GOOGLE_SCOPE_CALENDAR_FULL not in requested_scopes

    # Existing write-granted tokens still satisfy read-only calendar checks.
    assert scope_satisfied(GOOGLE_SCOPE_CALENDAR_EVENTS_RO, [GOOGLE_SCOPE_CALENDAR_EVENTS])
    _check_required_scopes(
        f"{GOOGLE_SCOPE_CALENDAR_LIST_RO} {GOOGLE_SCOPE_CALENDAR_EVENTS}",
        cal_tools_mod._required_scopes_for("google", "list_events"),
    )


# ===========================================================================
# 4. LangSmith tracing restriction applies strictly to calendar, never mail
# ===========================================================================


@pytest.mark.asyncio
async def test_r7_langsmith_tracing_only_blocks_calendar_leaving_mail_connect_and_reauth_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_id = "123456-abc.apps.googleusercontent.com"
    monkeypatch.setenv("DALTON_PROFILE", "core")
    monkeypatch.setenv("DALTON_CAP_GOOGLE_CALENDAR", "1")
    monkeypatch.setenv("DALTON_CAP_FEISHU_CALENDAR", "1")
    monkeypatch.setenv("DALTON_CAP_GOOGLE_GMAIL", "1")
    monkeypatch.setenv("DALTON_CAP_FEISHU_MAIL", "1")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("PUBLIC_APP_URL", "https://dalton.example.com")
    monkeypatch.setattr(
        "src.server.database.encryption.encryption_configured", lambda: True
    )
    monkeypatch.setattr(oauth_mod, "encryption_configured", lambda: True)

    cred_stub = lambda group: CredentialGroupResult(
        group=group,
        ready=True,
        source="env",
        revision=1,
        values={
            "DALTON_GOOGLE_OAUTH_CLIENT_ID": client_id,
            "DALTON_GOOGLE_OAUTH_CLIENT_SECRET": "g-secret",
            "DALTON_FEISHU_APP_ID": "cli_test",
            "DALTON_FEISHU_APP_SECRET": "f-secret",
            "DALTON_FEISHU_ENCRYPT_KEY": "enc-k",
            "DALTON_FEISHU_VERIFICATION_TOKEN": "ver-t",
        },
    )
    monkeypatch.setattr(
        "src.dalton.deployment_credentials.snapshot.get_credential_group", cred_stub
    )
    monkeypatch.setattr(oauth_mod, "get_credential_group", cred_stub)

    # Provider summary keeps provider ready=True and gates only services.calendar.
    summary = cap_mod.get_connector_providers_summary()
    for prov in ("google", "feishu"):
        assert summary[prov]["ready"] is True
        assert summary[prov]["langsmith_tracing_enabled"] is True
        assert summary[prov]["services"]["calendar"]["ready"] is False
        assert summary[prov]["services"]["calendar"]["langsmith_tracing_enabled"] is True
        assert summary[prov]["services"]["mail"]["ready"] is True
        assert summary[prov]["services"]["mail"]["langsmith_tracing_enabled"] is False
        assert cap_mod.is_connector_service_ready(summary[prov], "calendar") is False
        assert cap_mod.is_connector_service_ready(summary[prov], "mail") is True

    # Simulate existing needs_reauth mail rows so we test BOTH connect and reauth for mail.
    existing_rows = [
        _make_integration_row(
            provider="google",
            service="mail",
            oauth_client_id=client_id,
            granted_scopes=f"openid email {GOOGLE_SCOPE_GMAIL_READONLY}",
            status=IntegrationStatus.NEEDS_REAUTH,
        ),
        _make_integration_row(
            provider="feishu",
            service="mail",
            oauth_client_id="cli_test",
            granted_scopes="offline_access mail:user_mailbox.message:readonly",
            status=IntegrationStatus.NEEDS_REAUTH,
        ),
    ]

    async def _list_conns(user_id: str, provider: str | None = None):
        _ = user_id
        if provider is None:
            return list(existing_rows)
        return [r for r in existing_rows if r.provider == provider]

    monkeypatch.setattr(
        db_mod,
        "list_user_connections",
        create_autospec(db_mod.list_user_connections, side_effect=_list_conns),
    )
    monkeypatch.setattr(
        db_mod,
        "list_user_purge_ledgers",
        create_autospec(db_mod.list_user_purge_ledgers, return_value=[]),
    )
    monkeypatch.setattr(
        oauth_mod.db,
        "insert_oauth_state",
        create_autospec(db_mod.insert_oauth_state, return_value=None),
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

    # 1. GET /api/v1/dalton/integrations reports per-service readiness accurately.
    list_resp = client.get("/api/v1/dalton/integrations")
    assert list_resp.status_code == 200
    by_pair = {
        (c["provider"], c["service"]): c for c in list_resp.json()["connectors"]
    }
    for prov in ("google", "feishu"):
        assert by_pair[(prov, "calendar")]["ready"] is False
        assert by_pair[(prov, "calendar")]["langsmith_tracing_enabled"] is True
        assert by_pair[(prov, "mail")]["ready"] is True
        assert by_pair[(prov, "mail")]["langsmith_tracing_enabled"] is False

    # 2. Calendar authorize is blocked with 503 under LangSmith tracing.
    for prov in ("google", "feishu"):
        cal_resp = client.post(f"/api/v1/dalton/integrations/{prov}/calendar/authorize")
        assert cal_resp.status_code == 503
        assert "当前部署开启了调用追踪，个人日历不可用" in json.dumps(
            cal_resp.json(), ensure_ascii=False
        )

    # 3. Gmail & Feishu Mail authorize (reauth on needs_reauth AND fresh connect) succeed with 200!
    for prov in ("google", "feishu"):
        mail_reauth_resp = client.post(
            f"/api/v1/dalton/integrations/{prov}/mail/authorize"
        )
        assert mail_reauth_resp.status_code == 200
        assert mail_reauth_resp.json()["authorize_url"].startswith("https://")
        assert mail_reauth_resp.json()["intent"] == "reauth"

    existing_rows.clear()
    for prov in ("google", "feishu"):
        mail_connect_resp = client.post(
            f"/api/v1/dalton/integrations/{prov}/mail/authorize"
        )
        assert mail_connect_resp.status_code == 200
        assert mail_connect_resp.json()["authorize_url"].startswith("https://")
        assert mail_connect_resp.json()["intent"] == "connect"
