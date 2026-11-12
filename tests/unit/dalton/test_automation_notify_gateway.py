"""Generic automation notifications on the channel gateway.

Covers the plan, the delivery_id transport, the default-off settlement switch,
uncertain rows that must not be reclaimed, and test pushes that stay out of retry.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, Self
from unittest.mock import create_autospec
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from src.dalton.app.automation_notifications_router import (
    AutomationNotificationPlanRequest,
)
from src.dalton.database import automation_notifications as automation_notify_db
from src.dalton.database.automation_notifications import (
    authorize_and_claim_automation_notify_delivery,
    decide_automation_notification_plan,
    reconcile_uncertain_automation_notify_deliveries,
)
from src.dalton.database.verdicts import get_verdict_for_execution
from src.dalton.llm.model_prefs import (
    resolve_dalton_notify_enabled,
    resolve_dalton_notify_overrides,
    resolve_dalton_notify_routes,
    resolve_dalton_notify_target_labels,
    resolve_dalton_notify_targets,
)
from src.dalton.notify import dispatch as notify_dispatch
from src.dalton.notify.channel_transport import (
    UNCERTAIN_NOTIFY_ERROR_CODES,
    post_automation_gateway_notify,
)
from src.dalton.notify.dispatch import (
    ClaimAttempt,
    claim_target_delivery,
    mark_target_delivery,
    on_automation_settled,
    post_bridge_notify,
    send_dalton_notify_test,
)
from src.dalton.settings import get_dalton_frontend_url

FEISHU_TARGET = "feishu:user:ou_analyst_a"
DISCORD_TARGET = "discord:user:123456789012345678"
CARD = {
    "schema": "2.0",
    "header": {"title": {"tag": "plain_text", "content": "完成"}, "template": "blue"},
    "body": {"elements": [{"tag": "markdown", "content": "已完成"}]},
}
DELIVERY_ID = "12345678-1234-4234-8234-1234567890ab"


class _PlanCursor:
    async def execute(self, *_args: Any, **_kwargs: Any) -> None:
        return

    async def fetchone(self) -> None:
        return


def _completion_row(
    target: str,
    payload: dict[str, Any],
    *,
    status: str = "pending",
) -> dict[str, Any]:
    return {
        "user_id": "user-1",
        "channel": target.split(":", 1)[0],
        "target": target,
        "status": status,
        "payload_json": payload,
    }


async def _plan(
    row: Mapping[str, Any] | None,
    *,
    enabled: bool = True,
    targets: list[str] | None = None,
    admin: bool = False,
) -> dict[str, Any]:
    enabled_fn = create_autospec(resolve_dalton_notify_enabled, return_value=enabled)
    overrides_fn = create_autospec(resolve_dalton_notify_overrides, return_value={})
    targets_fn = create_autospec(
        resolve_dalton_notify_targets,
        return_value=list(targets if targets is not None else [FEISHU_TARGET]),
    )
    routes_fn = create_autospec(resolve_dalton_notify_routes, return_value={})
    admin_fn = create_autospec(notify_dispatch._default_notify_admin, return_value=admin)
    return await decide_automation_notification_plan(
        _PlanCursor(),
        row,
        delivery_id=DELIVERY_ID,
        resolve_enabled_fn=enabled_fn,
        resolve_overrides_fn=overrides_fn,
        resolve_targets_fn=targets_fn,
        resolve_routes_fn=routes_fn,
        is_admin_fn=admin_fn,
    )


def test_plan_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        AutomationNotificationPlanRequest.model_validate(
            {"delivery_id": DELIVERY_ID, "card": CARD}
        )
    parsed = AutomationNotificationPlanRequest.model_validate(
        {"delivery_id": DELIVERY_ID}
    )
    assert str(parsed.delivery_id) == DELIVERY_ID


@pytest.mark.asyncio
async def test_feishu_plan_is_the_stored_card_and_discord_plan_is_the_stored_text() -> None:
    feishu = await _plan(
        _completion_row(
            FEISHU_TARGET,
            {
                "text": "不发给飞书",
                "card": CARD,
                "automation_name": "dalton-googl-earn-2026q1-t0",
                "category": "earnings_t0",
                "source": "automation_completion",
                "transport": "gateway",
            },
        )
    )
    assert feishu["authorized"] is True
    assert feishu["items"] == [
        {
            "item_index": 0,
            "delivery_key": f"automation_notify:{DELIVERY_ID}:item:0",
            "kind": "feishu_card",
            "card": CARD,
            "images": [],
            "existing_status": None,
        }
    ]

    discord = await _plan(
        _completion_row(
            DISCORD_TARGET,
            {
                "text": "Discord 正文",
                "automation_name": "dalton-googl-earn-2026q1-t0",
                "category": "earnings_t0",
                "source": "automation_completion",
                "transport": "gateway",
            },
        ),
        targets=[DISCORD_TARGET],
    )
    assert discord["authorized"] is True
    assert discord["canonical_target_address"] == "discord:@me"
    assert discord["recipient_id"] == "123456789012345678"
    assert discord["items"][0]["kind"] == "text"
    assert discord["items"][0]["text"] == "Discord 正文"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "code"),
    [
        ({"feishu_cards": [{"card": CARD}]}, "feishu_cards_not_supported"),
        ({"images": [{"image_key": "img"}]}, "images_not_supported"),
        (
            {"card": {"schema": "2.0", "body": {"tag": "__img__"}}},
            "image_placeholder_not_supported",
        ),
    ],
)
async def test_media_payloads_are_rejected(extra: dict[str, Any], code: str) -> None:
    payload = {
        "text": "正文",
        "card": CARD,
        "automation_name": "dalton-googl-earn-2026q1-t0",
        "category": "earnings_t0",
        "source": "automation_completion",
        "transport": "gateway",
    }
    payload.update(extra)
    plan = await _plan(_completion_row(FEISHU_TARGET, payload))
    assert plan["authorized"] is False
    assert plan["code"] == code


@pytest.mark.asyncio
async def test_closed_switch_removed_target_and_eve_chat_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = {
        "text": "正文",
        "card": CARD,
        "automation_name": "dalton-googl-earn-2026q1-t0",
        "category": "earnings_t0",
        "source": "automation_completion",
        "transport": "gateway",
    }
    closed = await _plan(_completion_row(FEISHU_TARGET, base), enabled=False)
    assert closed["status"] == "skipped"
    assert closed["code"] == "toggle_off"

    removed = await _plan(
        _completion_row(FEISHU_TARGET, base),
        targets=["feishu:user:ou_other"],
    )
    assert removed["status"] == "skipped"
    assert removed["code"] == "target_removed"

    monkeypatch.setenv("DALTON_EVE_TRACKING_CHAT_ID", "oc_eve_unit_test")
    eve = await _plan(
        _completion_row(
            "feishu:chat:oc_eve_unit_test",
            {
                "text": "测试",
                "card": CARD,
                "source": "notify_test",
                "transport": "gateway",
            },
        ),
        targets=[],
        admin=True,
    )
    assert eve["status"] == "skipped"
    assert eve["code"] == "eve_tracking_mirror_pending"

    weixin = await _plan(
        _completion_row("weixin:user:abc", base),
        targets=["weixin:user:abc"],
    )
    assert weixin["code"] == "unsupported_platform"


def _transport_client(post_result: Any) -> Any:
    client = create_autospec(httpx.AsyncClient, instance=True)
    client.post.return_value = post_result
    return client


@pytest.mark.asyncio
async def test_automation_gateway_posts_only_delivery_id_and_classifies_outcomes() -> None:
    invalid = await post_automation_gateway_notify(
        "not-a-uuid",
        target=FEISHU_TARGET,
        gateway_url="http://127.0.0.1:9",
        service_token="tok",
    )
    assert invalid["error_code"] == "invalid_request"
    assert invalid["uncertain"] is False

    connect_client = _transport_client(None)
    connect_client.post.side_effect = httpx.ConnectError("down")
    connect = await post_automation_gateway_notify(
        DELIVERY_ID,
        target=FEISHU_TARGET,
        gateway_url="http://127.0.0.1:9",
        service_token="tok",
        http_client=connect_client,
    )
    assert connect["status"] == "failed"
    assert connect["uncertain"] is False
    assert connect["error_code"] == "ConnectError"
    assert connect["results"][0]["target"] == FEISHU_TARGET
    body = connect_client.post.await_args.kwargs["json"]
    assert body == {"delivery_id": DELIVERY_ID}

    timeout_client = _transport_client(None)
    timeout_client.post.side_effect = httpx.ReadTimeout("late")
    timed = await post_automation_gateway_notify(
        DELIVERY_ID,
        target=FEISHU_TARGET,
        gateway_url="http://127.0.0.1:9",
        service_token="tok",
        http_client=timeout_client,
    )
    assert timed["uncertain"] is True
    assert timed["error_code"] == "delivery_uncertain"

    for code in ("in_flight", "unknown", "partial"):
        response = create_autospec(httpx.Response, instance=True)
        response.status_code = 200
        response.json.return_value = {"status": code, "code": code}
        client = _transport_client(response)
        result = await post_automation_gateway_notify(
            DELIVERY_ID,
            target=FEISHU_TARGET,
            gateway_url="http://127.0.0.1:9",
            service_token="tok",
            http_client=client,
        )
        assert result["uncertain"] is True
        assert result["error_code"] == code
        assert result["results"][0]["target"] == FEISHU_TARGET


class _RetryCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self._returned_scan = False
        self.executed: list[tuple[str, Any]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return False

    async def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((" ".join(str(sql).split()), params))

    async def fetchall(self) -> list[dict[str, Any]]:
        if self._returned_scan:
            return []
        self._returned_scan = True
        return list(self.rows)

    async def fetchone(self) -> None:
        return


class _RetryConnection:
    def __init__(self, cursor: _RetryCursor) -> None:
        self._cursor = cursor

    def cursor(self, **_kwargs: Any) -> _RetryCursor:
        return self._cursor


@pytest.mark.asyncio
async def test_uncertain_pending_and_notify_test_rows_are_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _RetryCursor(
        [
            {
                "delivery_id": DELIVERY_ID,
                "execution_id": "exec-uncertain",
                "automation_id": "",
                "user_id": "user-1",
                "channel": "feishu",
                "target": FEISHU_TARGET,
                "status": "pending",
                "attempt_count": 1,
                "error_code": "delivery_uncertain",
                "updated_at": None,
                "payload_json": {
                    "source": "automation_completion",
                    "transport": "gateway",
                    "text": "正文",
                    "card": CARD,
                },
                "automation_name": "dalton-googl-earn-2026q1-t0",
                "automation_metadata": {},
            },
            {
                "delivery_id": str(uuid4()),
                "execution_id": "test:ignored",
                "automation_id": "",
                "user_id": "user-1",
                "channel": "feishu",
                "target": FEISHU_TARGET,
                "status": "failed",
                "attempt_count": 1,
                "error_code": "ConnectError",
                "updated_at": None,
                "payload_json": {"source": "notify_test", "transport": "gateway"},
                "automation_name": "",
                "automation_metadata": {},
            },
        ]
    )

    @asynccontextmanager
    async def _connection(_conn: Any = None):
        yield _RetryConnection(cursor)

    monkeypatch.setattr(
        notify_dispatch,
        "get_db_connection",
        create_autospec(notify_dispatch.get_db_connection, side_effect=_connection),
    )
    gateway = create_autospec(post_automation_gateway_notify)
    monkeypatch.setattr(notify_dispatch, "post_automation_gateway_notify", gateway)
    bridge = create_autospec(post_bridge_notify)
    sent = await notify_dispatch.retry_failed_notify_deliveries(
        post_notify_fn=bridge,
        max_attempts=4,
        lookback_hours=24,
        stale_pending_minutes=10,
        limit=5,
    )
    assert sent == 0
    gateway.assert_not_called()
    bridge.assert_not_called()
    select_sql, select_params = cursor.executed[0]
    assert "SELECT d.execution_id" in select_sql
    assert "d.delivery_id" in select_sql
    assert "<> 'notify_test'" in select_sql
    assert "COALESCE(d.automation_id, '') LIKE 'data:%%'" in select_sql
    assert "->> 'transport', '') = 'gateway'" in select_sql
    assert "COALESCE(d.error_code, '') <> ALL(%s)" in select_sql
    assert select_sql.count("%s") == 6
    assert select_params[1] == sorted(UNCERTAIN_NOTIFY_ERROR_CODES)


@pytest.mark.asyncio
async def test_claim_returns_delivery_id_without_reclaiming_uncertain_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[str] = []

    class _Cursor:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> bool | None:
            return False

        async def execute(self, sql: str, _params: Any = None) -> None:
            executed.append(" ".join(sql.split()))

        async def fetchone(self) -> tuple[str, int]:
            return (DELIVERY_ID, 1)

    class _Connection:
        def cursor(self, **_kwargs: Any) -> _Cursor:
            return _Cursor()

    @asynccontextmanager
    async def _connection(_conn: Any = None):
        yield _Connection()

    monkeypatch.setattr(
        notify_dispatch,
        "get_db_connection",
        create_autospec(notify_dispatch.get_db_connection, side_effect=_connection),
    )
    claimed = await notify_dispatch.claim_target_delivery_with_attempt(
        "exec-1",
        "feishu",
        FEISHU_TARGET,
        user_id="user-1",
    )
    assert claimed == (True, 1)
    assert len(claimed) == 2
    assert claimed.delivery_id == DELIVERY_ID
    assert "RETURNING delivery_id, attempt_count" in executed[0]
    assert "COALESCE(dalton_notify_deliveries.automation_id, '') LIKE 'data:%%'" in executed[0]
    assert "->> 'transport', '') = 'gateway'" in executed[0]
    assert "COALESCE(dalton_notify_deliveries.error_code, '') <> ALL(%s)" in executed[0]
    preserved_at = executed[0].index("payload_json ->> 'transport', '' ) <> ''")
    replaced_at = executed[0].index("ELSE EXCLUDED.payload_json")
    assert preserved_at > replaced_at


def _settlement_kwargs(post: Any, claim: Any, mark: Any) -> dict[str, Any]:
    return {
        "automation": {
            "name": "dalton-googl-earn-2026q1-t0",
            "user_id": "user-1",
            "automation_id": str(uuid4()),
        },
        "execution_id": "exec-settle",
        "thread_id": "th-1",
        "final_text": "营收超预期。云业务利润率改善。跟踪下季度资本开支。",
        "resolve_enabled_fn": create_autospec(
            resolve_dalton_notify_enabled, return_value=True
        ),
        "resolve_overrides_fn": create_autospec(
            resolve_dalton_notify_overrides, return_value={}
        ),
        "resolve_targets_fn": create_autospec(
            resolve_dalton_notify_targets, return_value=[FEISHU_TARGET]
        ),
        "resolve_routes_fn": create_autospec(
            resolve_dalton_notify_routes, return_value={}
        ),
        "is_admin_fn": create_autospec(
            notify_dispatch._default_notify_admin, return_value=False
        ),
        "get_verdict_fn": create_autospec(get_verdict_for_execution, return_value=None),
        "claim_target_fn": claim,
        "mark_target_fn": mark,
        "post_notify_fn": post,
        "frontend_url_fn": create_autospec(
            get_dalton_frontend_url, return_value="https://dalton.example.com"
        ),
    }


@pytest.mark.asyncio
async def test_settlement_flag_off_uses_bridge_and_flag_on_posts_delivery_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        notify_dispatch, "notify_via_gateway_enabled", lambda: False
    )
    bridge = create_autospec(
        post_bridge_notify,
        return_value={
            "ok": True,
            "results": [{"target": FEISHU_TARGET, "ok": True}],
        },
    )
    claim = create_autospec(claim_target_delivery, return_value=True)
    mark = create_autospec(mark_target_delivery, return_value=True)
    gateway = create_autospec(post_automation_gateway_notify)
    monkeypatch.setattr(notify_dispatch, "post_automation_gateway_notify", gateway)
    pushed = await on_automation_settled(**_settlement_kwargs(bridge, claim, mark))
    assert pushed is True
    bridge.assert_awaited()
    gateway.assert_not_called()
    payload = claim.await_args.kwargs["payload"]
    assert payload["source"] == "automation_completion"
    assert payload["transport"] == "bridge"

    monkeypatch.setattr(notify_dispatch, "notify_via_gateway_enabled", lambda: True)
    marked_bridge = create_autospec(
        post_bridge_notify,
        return_value={"ok": True, "results": [{"target": FEISHU_TARGET, "ok": True}]},
    )
    marked_bridge.__dalton_bridge_notify__ = True
    claim_with_id = create_autospec(
        notify_dispatch.claim_target_delivery_with_attempt,
        return_value=ClaimAttempt(True, 1, DELIVERY_ID),
    )
    monkeypatch.setattr(
        notify_dispatch,
        "claim_target_delivery_with_attempt",
        claim_with_id,
    )
    gateway_on = create_autospec(
        post_automation_gateway_notify,
        return_value={
            "ok": True,
            "status": "sent",
            "uncertain": False,
            "results": [{"target": FEISHU_TARGET, "ok": True, "status": "sent"}],
        },
    )
    monkeypatch.setattr(notify_dispatch, "post_automation_gateway_notify", gateway_on)
    legacy_claim = create_autospec(
        claim_target_delivery, side_effect=AssertionError("bridge claim")
    )
    mark_on = create_autospec(mark_target_delivery, return_value=True)
    pushed_on = await on_automation_settled(
        **_settlement_kwargs(marked_bridge, legacy_claim, mark_on)
    )
    assert pushed_on is True
    marked_bridge.assert_not_called()
    legacy_claim.assert_not_called()
    assert gateway_on.await_args.args == (DELIVERY_ID,)
    assert gateway_on.await_args.kwargs == {"target": FEISHU_TARGET}
    stamped = claim_with_id.await_args.kwargs["payload"]
    assert stamped["transport"] == "gateway"
    assert stamped["source"] == "automation_completion"
    mark_on.assert_awaited()
    assert mark_on.await_args.kwargs["status"] == "sent"


@pytest.mark.asyncio
async def test_gateway_settlement_keeps_connect_failure_and_uncertain_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notify_dispatch, "notify_via_gateway_enabled", lambda: True)
    bridge = create_autospec(post_bridge_notify)
    bridge.__dalton_bridge_notify__ = True
    claim = create_autospec(
        notify_dispatch.claim_target_delivery_with_attempt,
        return_value=ClaimAttempt(True, 1, DELIVERY_ID),
    )
    monkeypatch.setattr(notify_dispatch, "claim_target_delivery_with_attempt", claim)
    gateway = create_autospec(
        post_automation_gateway_notify,
        return_value={
            "ok": False,
            "status": "failed",
            "uncertain": False,
            "error_code": "ConnectError",
            "results": [
                {
                    "target": FEISHU_TARGET,
                    "ok": False,
                    "status": "failed",
                    "error": "ConnectError",
                    "error_code": "ConnectError",
                }
            ],
        },
    )
    monkeypatch.setattr(notify_dispatch, "post_automation_gateway_notify", gateway)
    mark = create_autospec(mark_target_delivery, return_value=True)
    await on_automation_settled(
        **_settlement_kwargs(bridge, create_autospec(claim_target_delivery), mark)
    )
    assert mark.await_args.kwargs["status"] == "failed"
    assert mark.await_args.kwargs["error_code"] == "ConnectError"

    gateway.return_value = {
        "ok": False,
        "status": "unknown",
        "uncertain": True,
        "error_code": "in_flight",
        "results": [
            {
                "target": FEISHU_TARGET,
                "ok": False,
                "status": "unknown",
                "uncertain": True,
                "error": "in_flight",
                "error_code": "in_flight",
            }
        ],
    }
    mark_uncertain = create_autospec(mark_target_delivery, return_value=True)
    await on_automation_settled(
        **_settlement_kwargs(
            bridge, create_autospec(claim_target_delivery), mark_uncertain
        )
    )
    assert mark_uncertain.await_args.kwargs["status"] == "pending"
    assert mark_uncertain.await_args.kwargs["error_code"] == "in_flight"


@pytest.mark.asyncio
async def test_notify_test_via_gateway_keeps_the_three_field_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notify_dispatch, "notify_via_gateway_enabled", lambda: True)
    targets = create_autospec(
        resolve_dalton_notify_targets, return_value=[FEISHU_TARGET]
    )
    labels = create_autospec(resolve_dalton_notify_target_labels, return_value={})
    monkeypatch.setattr(
        "src.dalton.llm.model_prefs.resolve_dalton_notify_targets", targets
    )
    monkeypatch.setattr(
        "src.dalton.llm.model_prefs.resolve_dalton_notify_target_labels", labels
    )
    claim = create_autospec(
        notify_dispatch.claim_target_delivery_with_attempt,
        return_value=ClaimAttempt(True, 1, DELIVERY_ID),
    )
    monkeypatch.setattr(notify_dispatch, "claim_target_delivery_with_attempt", claim)
    gateway = create_autospec(
        post_automation_gateway_notify,
        return_value={
            "ok": False,
            "status": "unknown",
            "uncertain": True,
            "error_code": "delivery_uncertain",
            "results": [
                {
                    "target": FEISHU_TARGET,
                    "ok": False,
                    "uncertain": True,
                    "status": "unknown",
                    "error_code": "delivery_uncertain",
                }
            ],
        },
    )
    monkeypatch.setattr(notify_dispatch, "post_automation_gateway_notify", gateway)
    mark = create_autospec(mark_target_delivery, return_value=True)
    monkeypatch.setattr(notify_dispatch, "mark_target_delivery", mark)

    result = await send_dalton_notify_test("user-1", FEISHU_TARGET)
    assert set(result) == {"ok", "detail", "error_code"}
    assert result == {
        "ok": False,
        "detail": "结果未知",
        "error_code": "delivery_uncertain",
    }
    assert claim.await_args.args[0].startswith("test:")
    assert claim.await_args.kwargs["payload"]["source"] == "notify_test"
    assert claim.await_args.kwargs["payload"]["transport"] == "gateway"
    assert mark.await_args.kwargs["status"] == "pending"
    assert mark.await_args.kwargs["error_code"] == "delivery_uncertain"
    assert gateway.await_args.kwargs == {"target": FEISHU_TARGET}
    assert "text" not in gateway.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row_extra", "payload_extra"),
    [
        ({"automation_id": "data:softbank-nav-monitor"}, {}),
        ({}, {"route_key": "data:softbank-nav-monitor"}),
        ({}, {"content_kind": "cards"}),
        ({}, {"transport": "bridge"}),
        ({}, {"transport": ""}),
        ({}, {"source": "automation_report"}),
    ],
)
async def test_plan_rejects_rows_that_are_not_gateway_notifications(
    row_extra: dict[str, Any],
    payload_extra: dict[str, Any],
) -> None:
    payload = {
        "text": "正文",
        "card": CARD,
        "automation_name": "dalton-googl-earn-2026q1-t0",
        "category": "earnings_t0",
        "source": "automation_completion",
        "transport": "gateway",
    }
    payload.update(payload_extra)
    row = _completion_row(FEISHU_TARGET, payload)
    row.update(row_extra)
    plan = await _plan(row)
    assert plan["authorized"] is False
    assert plan["code"] == "not_gateway_notification"


@pytest.mark.asyncio
async def test_claim_refuses_a_data_task_row_before_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[str] = []

    class _Cursor:
        def __init__(self) -> None:
            self._fetched = 0

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> bool | None:
            return False

        async def execute(self, sql: str, _params: Any = None) -> None:
            executed.append(" ".join(sql.split()))

        async def fetchone(self) -> dict[str, Any] | None:
            self._fetched += 1
            if self._fetched == 1:
                return None
            if self._fetched == 2:
                return {
                    "delivery_id": DELIVERY_ID,
                    "execution_id": "exec-data",
                    "automation_id": "data:softbank-nav-monitor",
                    "user_id": "user-1",
                    "channel": "feishu",
                    "target": FEISHU_TARGET,
                    "status": "pending",
                    "attempt_count": 1,
                    "error_code": None,
                    "payload_json": {
                        "source": "automation_completion",
                        "transport": "gateway",
                        "text": "正文",
                        "card": CARD,
                        "route_key": "data:softbank-nav-monitor",
                    },
                }
            return None

    class _Connection:
        def cursor(self, **_kwargs: Any) -> _Cursor:
            return _Cursor()

        def transaction(self) -> Any:
            @asynccontextmanager
            async def _tx() -> Any:
                yield None

            return _tx()

    @asynccontextmanager
    async def _connection(_conn: Any = None) -> Any:
        yield _Connection()

    monkeypatch.setattr(
        automation_notify_db,
        "get_db_connection",
        create_autospec(
            _REAL_GET_DB_CONNECTION,
            side_effect=_connection,
        ),
    )
    result = await authorize_and_claim_automation_notify_delivery(
        delivery_key=f"automation_notify:{DELIVERY_ID}:item:0",
        run_id=DELIVERY_ID,
        target=FEISHU_TARGET,
    )
    assert result["claim_status"] == "denied"
    assert result["code"] == "not_gateway_notification"
    assert not any("INSERT INTO dalton_channel_deliveries" in sql for sql in executed)


@pytest.mark.asyncio
async def test_renamed_earnings_calendar_stays_eligible_on_the_second_check() -> None:
    plan = await _plan(
        _completion_row(
            FEISHU_TARGET,
            {
                "text": "本周财报日历",
                "card": CARD,
                "automation_name": "coverage-calendar-renamed",
                "category": "earnings_calendar",
                "source": "automation_completion",
                "transport": "gateway",
            },
        )
    )
    assert plan["authorized"] is True
    assert plan["status"] == "authorized"
    rebuilt = notify_dispatch._reconstruct_automation_from_delivery_row(
        {
            "automation_id": "auto-1",
            "automation_name": "coverage-calendar-renamed",
        },
        {
            "category": "earnings_calendar",
            "automation_name": "coverage-calendar-renamed",
        },
    )
    assert rebuilt is not None
    assert rebuilt["metadata"]["dalton_kind"] == "calendar"
    classified = notify_dispatch.classify_automation_for_notify(rebuilt)
    assert classified is not None
    assert classified.category == "earnings_calendar"


class _ReconcileState:
    def __init__(self, channel_status: str | None) -> None:
        self.channel_status = channel_status
        self.updates = 0
        self.outer: dict[str, Any] = {
            "delivery_id": DELIVERY_ID,
            "execution_id": "exec-reconcile",
            "automation_id": "auto-1",
            "user_id": "user-1",
            "channel": "feishu",
            "target": FEISHU_TARGET,
            "status": "pending",
            "attempt_count": 2,
            "error_code": "delivery_uncertain",
            "payload_json": {
                "source": "automation_completion",
                "transport": "gateway",
                "text": "正文",
                "card": CARD,
            },
        }
        self.scanned_sql: list[str] = []


class _ReconcileCursor:
    def __init__(self, state: _ReconcileState) -> None:
        self.state = state
        self.rowcount = 0
        self._rows: list[dict[str, Any]] = []
        self._one: dict[str, Any] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return False

    async def execute(self, sql: str, params: Any = None) -> None:
        norm = " ".join(sql.split())
        self.rowcount = 0
        self._rows = []
        self._one = None
        if "FROM dalton_notify_deliveries" in norm and norm.startswith("SELECT"):
            self.state.scanned_sql.append(norm)
            if self.state.outer["status"] == "pending":
                self._rows = [dict(self.state.outer)]
            return
        if "FROM dalton_channel_deliveries" in norm:
            await asyncio.sleep(0)
            if self.state.channel_status is not None:
                self._one = {"status": self.state.channel_status}
            return
        if norm.startswith("UPDATE dalton_notify_deliveries"):
            new_status, new_code, delivery_id, expected_error, expected_attempt = params
            outer = self.state.outer
            if (
                str(outer["delivery_id"]) == str(delivery_id)
                and outer["status"] == "pending"
                and outer["error_code"] == expected_error
                and outer["attempt_count"] == expected_attempt
            ):
                outer["status"] = new_status
                outer["error_code"] = new_code
                outer_attempt = outer["attempt_count"]
                self.state.updates += 1
                self.rowcount = 1
                assert outer_attempt == expected_attempt

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    async def fetchone(self) -> dict[str, Any] | None:
        return self._one


class _ReconcileConnection:
    def __init__(self, state: _ReconcileState) -> None:
        self.state = state

    def cursor(self, **_kwargs: Any) -> _ReconcileCursor:
        return _ReconcileCursor(self.state)

    def transaction(self) -> Any:
        @asynccontextmanager
        async def _tx() -> Any:
            yield None

        return _tx()


# Captured once: helpers patch this attribute repeatedly, and create_autospec over an
# already-patched attribute would spec a Mock.
_REAL_GET_DB_CONNECTION = automation_notify_db.get_db_connection


def _patch_reconcile_db(monkeypatch: pytest.MonkeyPatch, state: _ReconcileState) -> None:
    @asynccontextmanager
    async def _connection(_conn: Any = None) -> Any:
        yield _ReconcileConnection(state)

    monkeypatch.setattr(
        automation_notify_db,
        "get_db_connection",
        create_autospec(
            _REAL_GET_DB_CONNECTION,
            side_effect=_connection,
        ),
    )


@pytest.mark.asyncio
async def test_reconcile_marks_sent_and_retryable_failed_and_leaves_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_state = _ReconcileState("sent")
    _patch_reconcile_db(monkeypatch, sent_state)
    sent_count = await reconcile_uncertain_automation_notify_deliveries()
    assert sent_count == 1
    assert sent_state.outer["status"] == "sent"
    assert sent_state.outer["error_code"] is None
    assert sent_state.outer["attempt_count"] == 2
    assert sent_state.updates == 1
    scan_sql = sent_state.scanned_sql[0]
    assert "->> 'transport', '') = 'gateway'" in scan_sql
    assert "'automation_completion'" in scan_sql
    assert "'notify_test'" in scan_sql
    assert "NOT LIKE 'data:%%'" in scan_sql
    assert "content_kind" in scan_sql
    # Settled-platform filter is applied before LIMIT so unknown rows cannot starve the scan.
    assert "EXISTS ( SELECT 1 FROM dalton_channel_deliveries c" in scan_sql
    assert "c.status IN ('sent', 'failed')" in scan_sql
    assert scan_sql.index("EXISTS") < scan_sql.index("LIMIT")

    failed_state = _ReconcileState("failed")
    _patch_reconcile_db(monkeypatch, failed_state)
    failed_count = await reconcile_uncertain_automation_notify_deliveries()
    assert failed_count == 0
    assert failed_state.outer["status"] == "failed"
    assert failed_state.outer["error_code"] == "target_failed"
    assert failed_state.outer["attempt_count"] == 2
    assert failed_state.updates == 1

    unknown_state = _ReconcileState("unknown")
    _patch_reconcile_db(monkeypatch, unknown_state)
    unknown_count = await reconcile_uncertain_automation_notify_deliveries()
    assert unknown_count == 0
    assert unknown_state.outer["status"] == "pending"
    assert unknown_state.outer["error_code"] == "delivery_uncertain"
    assert unknown_state.updates == 0

    missing_state = _ReconcileState(None)
    _patch_reconcile_db(monkeypatch, missing_state)
    missing_count = await reconcile_uncertain_automation_notify_deliveries()
    assert missing_count == 0
    assert missing_state.outer["status"] == "pending"
    assert missing_state.updates == 0


@pytest.mark.asyncio
async def test_reconcile_updates_a_sent_row_once_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _ReconcileState("sent")
    _patch_reconcile_db(monkeypatch, state)
    first, second = await asyncio.gather(
        reconcile_uncertain_automation_notify_deliveries(),
        reconcile_uncertain_automation_notify_deliveries(),
    )
    assert first + second == 1
    assert state.updates == 1
    assert state.outer["status"] == "sent"
    assert state.outer["error_code"] is None
    assert state.outer["attempt_count"] == 2
