"""Tests for Item 5: quota-class (429) and general automation failure notification receipts."""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest

from src.dalton.notify import dispatch as notify_dispatch
from src.dalton.services.execution_delivery_status import summarize_execution_delivery
from src.server.services import automation_settlement as settlement_mod
from src.server.services.automation_outcomes import Outcome


@pytest.mark.asyncio
async def test_failure_receipt_unconfigured_for_quota_429_limit() -> None:
    """When a 429 quota-limited automation has no webhook and no channel targets configured,
    a 'unconfigured' (未配置) receipt is saved and visible in delivery summary."""
    recorded: list[tuple[str, list[dict]]] = []

    async def _record(execution_id: str, entries: list[dict]) -> None:
        recorded.append((execution_id, entries))

    record_spy = create_autospec(_record, side_effect=_record)
    resolve_enabled = create_autospec(
        notify_dispatch.resolve_dalton_notify_enabled, return_value=True
    )
    resolve_overrides = create_autospec(
        notify_dispatch.resolve_dalton_notify_overrides, return_value={}
    )
    resolve_targets = create_autospec(
        notify_dispatch.resolve_dalton_notify_targets, return_value=[]
    )
    resolve_routes = create_autospec(
        notify_dispatch.resolve_dalton_notify_routes, return_value={}
    )
    gate_spy = create_autospec(
        notify_dispatch._resolve_unit_managed_notify_gate,
        return_value=(True, None, None, False),
    )

    receipt = await notify_dispatch.on_automation_failed(
        automation={"automation_id": "auto-429-1", "user_id": "u-1", "name": "Hynix Alert"},
        execution_id="exec-429-unconf",
        outcome=Outcome.LIMITED.value,
        failure_reason="usage_limit",
        error="HTTP 429: Claude Opus quota exhausted",
        deleted=False,
        repeated_refusal=False,
        webhook_result=None,
        held_pending=False,
        resolve_enabled_fn=resolve_enabled,
        resolve_overrides_fn=resolve_overrides,
        resolve_targets_fn=resolve_targets,
        resolve_routes_fn=resolve_routes,
        resolve_unit_gate_fn=gate_spy,
        record_delivery_fn=record_spy,
    )

    assert receipt is not None
    assert receipt["receipt_status"] == "unconfigured"
    assert receipt["receipt_label"] == "未配置"
    assert receipt["failure_reason"] == "usage_limit"
    record_spy.assert_awaited_once()
    saved_entries = recorded[0][1]
    summary = summarize_execution_delivery(delivery_result=saved_entries)
    assert summary is not None
    assert summary["status"] == "unconfigured"
    assert summary["failure_notification_receipt"]["status"] == "unconfigured"
    assert summary["failure_notification_receipt"]["label_zh"] == "未配置"


@pytest.mark.asyncio
async def test_failure_receipt_suppressed_on_repeated_refusal_or_disabled_gate() -> None:
    """Repeated refusal (or soft-deleted / disabled notify) saves a 'suppressed' (已抑制) receipt."""
    recorded: list[tuple[str, list[dict]]] = []

    async def _record(execution_id: str, entries: list[dict]) -> None:
        recorded.append((execution_id, entries))

    record_spy = create_autospec(_record, side_effect=_record)

    receipt = await notify_dispatch.on_automation_failed(
        automation={"automation_id": "auto-429-2", "user_id": "u-1", "name": "Tencent Alert"},
        execution_id="exec-429-supp",
        outcome=Outcome.LIMITED.value,
        failure_reason="usage_limit",
        error="HTTP 429: rate limit",
        deleted=False,
        repeated_refusal=True,
        webhook_result=None,
        held_pending=False,
        record_delivery_fn=record_spy,
    )

    assert receipt is not None
    assert receipt["receipt_status"] == "suppressed"
    assert receipt["receipt_label"] == "已抑制"
    assert receipt["reason"] == "repeated_refusal"
    record_spy.assert_awaited_once()
    summary = summarize_execution_delivery(delivery_result=recorded[0][1])
    assert summary is not None
    assert summary["status"] == "suppressed"
    assert summary["failure_notification_receipt"]["status"] == "suppressed"
    assert summary["failure_notification_receipt"]["label_zh"] == "已抑制"


@pytest.mark.asyncio
async def test_failure_receipt_pending_when_configured_dispatch_is_queued_or_retryable() -> None:
    """Configured failure notification that is queued/retryable saves a 'pending' (待发) receipt."""
    recorded: list[tuple[str, list[dict]]] = []

    async def _record(execution_id: str, entries: list[dict]) -> None:
        recorded.append((execution_id, entries))

    record_spy = create_autospec(_record, side_effect=_record)
    resolve_enabled = create_autospec(
        notify_dispatch.resolve_dalton_notify_enabled, return_value=True
    )
    resolve_overrides = create_autospec(
        notify_dispatch.resolve_dalton_notify_overrides, return_value={}
    )
    resolve_targets = create_autospec(
        notify_dispatch.resolve_dalton_notify_targets, return_value=["feishu:ou_test123"]
    )
    resolve_routes = create_autospec(
        notify_dispatch.resolve_dalton_notify_routes, return_value={}
    )
    gate_spy = create_autospec(
        notify_dispatch._resolve_unit_managed_notify_gate,
        return_value=(True, None, None, False),
    )
    post_spy = create_autospec(
        notify_dispatch.post_bridge_notify,
        return_value={"ok": False, "error_code": "http_503", "error": "gateway busy"},
    )

    receipt = await notify_dispatch.on_automation_failed(
        automation={
            "automation_id": "auto-429-3",
            "user_id": "u-1",
            "name": "dalton-meta-move-up",
            "trigger_type": "price",
        },
        execution_id="exec-429-pend",
        outcome=Outcome.PLAN_LIMITED.value,
        failure_reason="claude_plan",
        error="HTTP 429: Claude plan limit",
        deleted=False,
        repeated_refusal=False,
        webhook_result=None,
        held_pending=False,
        resolve_enabled_fn=resolve_enabled,
        resolve_overrides_fn=resolve_overrides,
        resolve_targets_fn=resolve_targets,
        resolve_routes_fn=resolve_routes,
        resolve_unit_gate_fn=gate_spy,
        post_notify_fn=post_spy,
        record_delivery_fn=record_spy,
    )

    assert receipt is not None
    assert receipt["receipt_status"] == "pending"
    assert receipt["receipt_label"] == "待发"
    record_spy.assert_awaited_once()
    summary = summarize_execution_delivery(delivery_result=recorded[0][1])
    assert summary is not None
    assert summary["status"] == "pending"
    assert summary["failure_notification_receipt"]["status"] == "pending"
    assert summary["failure_notification_receipt"]["label_zh"] == "待发"


@pytest.mark.asyncio
async def test_failure_receipt_delivered_when_configured_channel_succeeds() -> None:
    """Configured failure notification sent cleanly saves a 'delivered' (已送达) receipt."""
    recorded: list[tuple[str, list[dict]]] = []

    async def _record(execution_id: str, entries: list[dict]) -> None:
        recorded.append((execution_id, entries))

    record_spy = create_autospec(_record, side_effect=_record)
    resolve_enabled = create_autospec(
        notify_dispatch.resolve_dalton_notify_enabled, return_value=True
    )
    resolve_overrides = create_autospec(
        notify_dispatch.resolve_dalton_notify_overrides, return_value={}
    )
    resolve_targets = create_autospec(
        notify_dispatch.resolve_dalton_notify_targets, return_value=["feishu:ou_test123"]
    )
    resolve_routes = create_autospec(
        notify_dispatch.resolve_dalton_notify_routes, return_value={}
    )
    gate_spy = create_autospec(
        notify_dispatch._resolve_unit_managed_notify_gate,
        return_value=(True, None, None, False),
    )
    post_spy = create_autospec(
        notify_dispatch.post_bridge_notify,
        return_value={"ok": True, "message_id": "om_fail_1"},
    )

    receipt = await notify_dispatch.on_automation_failed(
        automation={
            "automation_id": "auto-429-4",
            "user_id": "u-1",
            "name": "dalton-catl-move-down",
            "trigger_type": "price",
        },
        execution_id="exec-429-deliv",
        outcome=Outcome.SUBSCRIPTION_LIMITED.value,
        failure_reason="plan_limit",
        error="HTTP 429: subscription limit",
        deleted=False,
        repeated_refusal=False,
        webhook_result=None,
        held_pending=False,
        resolve_enabled_fn=resolve_enabled,
        resolve_overrides_fn=resolve_overrides,
        resolve_targets_fn=resolve_targets,
        resolve_routes_fn=resolve_routes,
        resolve_unit_gate_fn=gate_spy,
        post_notify_fn=post_spy,
        record_delivery_fn=record_spy,
    )

    assert receipt is not None
    assert receipt["receipt_status"] == "delivered"
    assert receipt["receipt_label"] == "已送达"
    post_spy.assert_awaited_once()
    record_spy.assert_awaited_once()
    summary = summarize_execution_delivery(delivery_result=recorded[0][1])
    assert summary is not None
    assert summary["status"] == "delivered"
    assert summary["failure_notification_receipt"]["status"] == "delivered"
    assert summary["failure_notification_receipt"]["label_zh"] == "已送达"


@pytest.mark.asyncio
async def test_after_settling_calls_on_automation_failed_for_quota_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_after_settling invokes on_automation_failed on Outcome.LIMITED even when WebhookClient returns None."""
    from src.dalton.database import automation_recycle

    soft_del_spy = create_autospec(
        automation_recycle.automation_is_soft_deleted, return_value=False
    )
    monkeypatch.setattr(automation_recycle, "automation_is_soft_deleted", soft_del_spy)

    webhook_cls = create_autospec(settlement_mod.WebhookClient)
    webhook_inst = webhook_cls.return_value
    webhook_inst.fire_event.return_value = None
    monkeypatch.setattr(settlement_mod, "WebhookClient", webhook_cls)

    failed_spy = create_autospec(
        notify_dispatch.on_automation_failed,
        return_value={"receipt_status": "delivered", "receipt_label": "已送达"},
    )
    monkeypatch.setattr(notify_dispatch, "on_automation_failed", failed_spy)
    metric_spy = create_autospec(settlement_mod.safe_add)
    monkeypatch.setattr(settlement_mod, "safe_add", metric_spy)

    await settlement_mod._after_settling(
        outcome=Outcome.LIMITED,
        automation={"automation_id": "auto-lim-1", "user_id": "u-1", "trigger_type": "price"},
        execution_id="exec-lim-1",
        row={"settled_from": "running", "previous_failure_reason": None},
        thread_id="t-1",
        run_id="r-1",
        workspace_id="ws-1",
        error="429 quota exceeded",
        delivery=None,
        final_text=None,
        finish_left=False,
    )

    failed_spy.assert_awaited_once()
    kwargs = failed_spy.await_args.kwargs
    assert kwargs["execution_id"] == "exec-lim-1"
    assert kwargs["outcome"] == Outcome.LIMITED.value
    assert kwargs["failure_reason"] == "usage_limit"
    assert kwargs["repeated_refusal"] is False
