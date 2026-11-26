"""Tests for Item 5 / r2 Item 1: quota-class (429) and general automation failure notification receipts.

Covers:
- Dedicated failure_notice_status / failure_notification_receipt fields (never polluting delivery_summary.status)
- All four receipt states: unconfigured (未配置) / suppressed (已抑制) / pending (待发) / delivered (已送达)
- Production default writer path importing src.server.database.automation_executions.record_delivery
- Permanent bridge errors (capability_disabled, bridge_url_unconfigured -> unconfigured; http_400 -> terminal failed, NOT pending)
- Outbox held_pending -> pending receipt -> settled delivery rows upgrading failure_notice_status to delivered
- FastAPI list_automations, list_automation_runs, and get_automation endpoints with 429 failed runs & no webhook rows returning 200 (never 500)
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import create_autospec
from uuid import uuid4

import pytest

from src.dalton.notify import dispatch as notify_dispatch
from src.dalton.services import execution_delivery_status as eds_mod
from src.dalton.services.execution_delivery_status import summarize_execution_delivery
from src.server.app import automations as automations_app
from src.server.database import automation_executions as exec_db
from src.server.models.automation import (
    AutomationExecutionResponse,
    ExecutionDeliverySummary,
)
from src.server.services import automation_settlement as settlement_mod
from src.server.services.automation_outcomes import Outcome


@pytest.mark.asyncio
async def test_failure_receipt_unconfigured_for_quota_429_limit() -> None:
    """When a 429 quota-limited automation has no webhook and no channel targets configured,
    an 'unconfigured' (未配置) receipt is stored in failure_notice_status while delivery_summary.status stays valid."""
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
    assert receipt["receipt_label_en"] == "Not configured"
    assert receipt["failure_reason"] == "usage_limit"
    record_spy.assert_awaited_once()
    saved_entries = recorded[0][1]
    summary = summarize_execution_delivery(delivery_result=saved_entries)
    assert summary is not None
    assert summary["status"] == "skipped"
    assert summary["failure_notice_status"] == "unconfigured"
    assert summary["failure_notice_label_zh"] == "未配置"
    assert summary["failure_notice_label_en"] == "Not configured"
    assert summary["failure_notification_receipt"]["status"] == "unconfigured"
    validated = ExecutionDeliverySummary.model_validate(summary)
    assert validated.status == "skipped"
    assert validated.failure_notice_status == "unconfigured"


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
    assert summary["status"] == "skipped"
    assert summary["failure_notice_status"] == "suppressed"
    assert summary["failure_notice_label_zh"] == "已抑制"
    assert summary["failure_notice_label_en"] == "Suppressed"
    validated = ExecutionDeliverySummary.model_validate(summary)
    assert validated.failure_notice_status == "suppressed"


@pytest.mark.asyncio
async def test_failure_receipt_pending_when_configured_dispatch_is_queued_or_retryable() -> None:
    """Configured failure notification that is retryable AND enqueued for retry saves a 'pending' (待发) receipt."""
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
    enqueue_spy = create_autospec(
        notify_dispatch._enqueue_failure_notice_retries,
        return_value=True,
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
        enqueue_retry_fn=enqueue_spy,
    )

    assert receipt is not None
    assert receipt["receipt_status"] == "pending"
    assert receipt["receipt_label"] == "待发"
    enqueue_spy.assert_awaited_once()
    record_spy.assert_awaited_once()
    summary = summarize_execution_delivery(delivery_result=recorded[0][1])
    assert summary is not None
    assert summary["status"] == "retrying"
    assert summary["failure_notice_status"] == "pending"
    assert summary["failure_notice_label_zh"] == "待发"
    assert summary["failure_notice_label_en"] == "Pending"
    validated = ExecutionDeliverySummary.model_validate(summary)
    assert validated.status == "retrying"
    assert validated.failure_notice_status == "pending"

    # Once retry delivers in dalton_notify_deliveries, read path reconciles receipt to 'delivered'.
    reconciled = summarize_execution_delivery(
        notify_rows=[
            {
                "channel": "feishu",
                "target": "feishu:ou_test123",
                "status": "sent",
                "attempt_count": 2,
            }
        ],
        delivery_result=recorded[0][1],
        execution_status="failed",
    )
    assert reconciled is not None
    assert reconciled["status"] == "delivered"
    assert reconciled["failure_notice_status"] == "delivered"
    assert reconciled["failure_notice_label_zh"] == "已送达"


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
    assert summary["failure_notice_status"] == "delivered"
    assert summary["failure_notice_label_zh"] == "已送达"
    assert summary["failure_notice_label_en"] == "Delivered"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["capability_disabled", "bridge_url_unconfigured"])
async def test_failure_receipt_bridge_unconfigured_or_disabled_not_stuck_pending(
    error_code: str,
) -> None:
    """Bridge capability_disabled / bridge_url_unconfigured must map to 'unconfigured', never perpetual 'pending'."""
    recorded: list[tuple[str, list[dict]]] = []

    async def _record(execution_id: str, entries: list[dict]) -> None:
        recorded.append((execution_id, entries))

    record_spy = create_autospec(_record, side_effect=_record)
    resolve_enabled = create_autospec(notify_dispatch.resolve_dalton_notify_enabled, return_value=True)
    resolve_overrides = create_autospec(notify_dispatch.resolve_dalton_notify_overrides, return_value={})
    resolve_targets = create_autospec(
        notify_dispatch.resolve_dalton_notify_targets, return_value=["feishu:ou_test123"]
    )
    resolve_routes = create_autospec(notify_dispatch.resolve_dalton_notify_routes, return_value={})
    gate_spy = create_autospec(
        notify_dispatch._resolve_unit_managed_notify_gate,
        return_value=(True, None, None, False),
    )
    post_spy = create_autospec(
        notify_dispatch.post_bridge_notify,
        return_value={"ok": False, "error_code": error_code},
    )

    receipt = await notify_dispatch.on_automation_failed(
        automation={
            "automation_id": "auto-unconf-bridge",
            "user_id": "u-1",
            "name": "dalton-meta-move-up",
            "trigger_type": "price",
        },
        execution_id=f"exec-{error_code}",
        outcome=Outcome.LIMITED.value,
        failure_reason="usage_limit",
        error="HTTP 429",
        resolve_enabled_fn=resolve_enabled,
        resolve_overrides_fn=resolve_overrides,
        resolve_targets_fn=resolve_targets,
        resolve_routes_fn=resolve_routes,
        resolve_unit_gate_fn=gate_spy,
        post_notify_fn=post_spy,
        record_delivery_fn=record_spy,
    )

    assert receipt is not None
    assert receipt["receipt_status"] == "unconfigured"
    assert receipt["reason"] == error_code
    summary = summarize_execution_delivery(delivery_result=recorded[0][1])
    assert summary is not None
    assert summary["status"] == "skipped"
    assert summary["failure_notice_status"] == "unconfigured"


@pytest.mark.asyncio
async def test_failure_receipt_permanent_http_400_records_terminal_failure_not_pending() -> None:
    """Permanent 4xx bridge failure must record a terminal failed delivery entry, not perpetual 'pending'."""
    recorded: list[tuple[str, list[dict]]] = []

    async def _record(execution_id: str, entries: list[dict]) -> None:
        recorded.append((execution_id, entries))

    record_spy = create_autospec(_record, side_effect=_record)
    resolve_enabled = create_autospec(notify_dispatch.resolve_dalton_notify_enabled, return_value=True)
    resolve_overrides = create_autospec(notify_dispatch.resolve_dalton_notify_overrides, return_value={})
    resolve_targets = create_autospec(
        notify_dispatch.resolve_dalton_notify_targets, return_value=["feishu:ou_bad"]
    )
    resolve_routes = create_autospec(notify_dispatch.resolve_dalton_notify_routes, return_value={})
    gate_spy = create_autospec(
        notify_dispatch._resolve_unit_managed_notify_gate,
        return_value=(True, None, None, False),
    )
    post_spy = create_autospec(
        notify_dispatch.post_bridge_notify,
        return_value={"ok": False, "error_code": "http_400", "error": "bad target"},
    )
    enqueue_spy = create_autospec(
        notify_dispatch._enqueue_failure_notice_retries,
        return_value=True,
    )

    res = await notify_dispatch.on_automation_failed(
        automation={
            "automation_id": "auto-400",
            "user_id": "u-1",
            "name": "dalton-meta-move-up",
            "trigger_type": "price",
        },
        execution_id="exec-400",
        outcome=Outcome.LIMITED.value,
        failure_reason="usage_limit",
        error="HTTP 429",
        resolve_enabled_fn=resolve_enabled,
        resolve_overrides_fn=resolve_overrides,
        resolve_targets_fn=resolve_targets,
        resolve_routes_fn=resolve_routes,
        resolve_unit_gate_fn=gate_spy,
        post_notify_fn=post_spy,
        record_delivery_fn=record_spy,
        enqueue_retry_fn=enqueue_spy,
    )

    assert res is not None
    assert res["status"] == "failed"
    enqueue_spy.assert_not_awaited()
    record_spy.assert_awaited_once()
    summary = summarize_execution_delivery(delivery_result=recorded[0][1], execution_status="failed")
    assert summary is not None
    assert summary["status"] == "failed"
    assert summary.get("failure_notice_status") is None
    ExecutionDeliverySummary.model_validate(summary)


@pytest.mark.asyncio
async def test_failure_receipt_default_writer_persists_via_automation_executions_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production default writer (record_delivery_fn=None) must use src.server.database.automation_executions.record_delivery."""
    from src.dalton import capabilities as cap_mod

    monkeypatch.setattr(cap_mod, "has_capability", create_autospec(cap_mod.has_capability, return_value=True))
    record_spy = create_autospec(exec_db.record_delivery, return_value=None)
    monkeypatch.setattr(exec_db, "record_delivery", record_spy)

    receipt = await notify_dispatch.on_automation_failed(
        automation={"automation_id": "auto-prod-writer", "user_id": "u-1", "name": "Prod Writer"},
        execution_id="exec-prod-writer",
        outcome=Outcome.LIMITED.value,
        failure_reason="usage_limit",
        error="HTTP 429",
        repeated_refusal=True,
        record_delivery_fn=None,
    )

    assert receipt is not None
    assert receipt["receipt_status"] == "suppressed"
    record_spy.assert_awaited_once()
    assert record_spy.await_args.args[0] == "exec-prod-writer"
    assert record_spy.await_args.args[1][0]["receipt_status"] == "suppressed"


@pytest.mark.asyncio
async def test_failure_receipt_outbox_held_pending_persists_and_updates_when_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When settlement leaves a failed run to the hook outbox (held_pending=True),
    on_automation_failed persists a 'pending' receipt; once outbox finishes with success,
    the read path reflects 'delivered'."""
    record_spy = create_autospec(exec_db.record_delivery, return_value=None)
    monkeypatch.setattr(exec_db, "record_delivery", record_spy)

    receipt = await notify_dispatch.on_automation_failed(
        automation={"automation_id": "auto-outbox", "user_id": "u-1", "name": "Outbox Fail"},
        execution_id="exec-outbox-1",
        outcome=Outcome.FAILED.value,
        failure_reason="server_error",
        error="boom",
        held_pending=True,
        record_delivery_fn=None,
    )

    assert receipt is not None
    assert receipt["receipt_status"] == "pending"
    assert receipt["reason"] == "outbox_queued"
    record_spy.assert_awaited_once()
    pending_summary = summarize_execution_delivery(
        delivery_result=record_spy.await_args.args[1],
        execution_status="failed",
    )
    assert pending_summary is not None
    assert pending_summary["status"] == "retrying"
    assert pending_summary["failure_notice_status"] == "pending"

    # Simulate _finish_held_run overwriting delivery_result once the message lands.
    settled_summary = summarize_execution_delivery(
        delivery_result=[{"method": "slack:T/C", "success": True}],
        execution_status="failed",
    )
    assert settled_summary is not None
    assert settled_summary["status"] == "delivered"
    assert settled_summary["failure_notice_status"] == "delivered"
    assert settled_summary["failure_notice_label_zh"] == "已送达"


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("receipt_status", "expected_delivery_status", "expected_zh", "expected_en"),
    [
        ("unconfigured", "skipped", "未配置", "Not configured"),
        ("suppressed", "skipped", "已抑制", "Suppressed"),
        ("pending", "retrying", "待发", "Pending"),
        ("delivered", "delivered", "已送达", "Delivered"),
    ],
)
async def test_automation_list_and_execution_endpoints_return_200_for_quota_429_without_webhook(
    monkeypatch: pytest.MonkeyPatch,
    receipt_status: str,
    expected_delivery_status: str,
    expected_zh: str,
    expected_en: str,
) -> None:
    """Regression for Grok blocker: GET /automations, GET /automations/executions, and GET /automations/{id}
    must NOT 500 when a quota-429 failed execution has only a failure_receipt entry and no webhook rows."""
    now = datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)
    auto_id = uuid4()
    exec_id = uuid4()
    receipt = notify_dispatch.build_failure_notification_receipt(
        receipt_status=receipt_status,
        outcome=Outcome.LIMITED.value,
        failure_reason="usage_limit",
        reason=f"test_{receipt_status}",
        error="HTTP 429: quota exhausted",
    )
    exec_row = {
        "automation_execution_id": exec_id,
        "automation_id": auto_id,
        "automation_name": "429 Quota Monitor",
        "trigger_type": "price",
        "workspace_id": None,
        "status": "failed",
        "conversation_thread_id": None,
        "scheduled_at": now,
        "started_at": now,
        "completed_at": now,
        "error_message": "HTTP 429: quota exhausted",
        "skip_reason": None,
        "failure_reason": "usage_limit",
        "wait_reason": None,
        "server_id": None,
        "delivery_result": [receipt],
        "created_at": now,
        "dismissed_at": None,
        "excerpt": None,
    }
    auto_row = {
        "automation_id": auto_id,
        "user_id": "u-1",
        "name": "429 Quota Monitor",
        "description": None,
        "trigger_type": "price",
        "cron_expression": None,
        "timezone": "UTC",
        "trigger_config": None,
        "next_run_at": None,
        "last_run_at": now,
        "instruction": "Check quote",
        "workspace_id": None,
        "llm_model": None,
        "reasoning_effort": None,
        "thread_strategy": "new",
        "conversation_thread_id": None,
        "status": "active",
        "max_failures": 3,
        "failure_count": 1,
        "disable_reason": None,
        "delivery_config": {"methods": []},
        "metadata": None,
        "created_at": now,
        "updated_at": now,
        "last_execution": exec_row,
    }

    monkeypatch.setattr(eds_mod, "is_dalton_enabled", create_autospec(eds_mod.is_dalton_enabled, return_value=False))
    monkeypatch.setattr(
        automations_app.auto_db,
        "list_automations",
        create_autospec(automations_app.auto_db.list_automations, return_value=([auto_row], 1)),
    )
    monkeypatch.setattr(
        automations_app.exec_db,
        "list_executions",
        create_autospec(automations_app.exec_db.list_executions, return_value=([exec_row], False)),
    )
    monkeypatch.setattr(
        notify_dispatch,
        "populate_automation_notify_state",
        create_autospec(notify_dispatch.populate_automation_notify_state, return_value=None),
    )

    principal = SimpleNamespace(user_id="u-1", computer_id=None)

    # 1. List automations (with last_execution enriched)
    list_resp = await automations_app.list_automations(
        principal=principal,
        status=None,
        scope=None,
        limit=50,
        offset=0,
        include_deleted=False,
    )
    assert list_resp.total == 1
    last_summary = list_resp.automations[0].last_execution.delivery_summary
    assert last_summary is not None
    assert last_summary.status == expected_delivery_status
    assert last_summary.failure_notice_status == receipt_status
    assert last_summary.failure_notice_label_zh == expected_zh
    assert last_summary.failure_notice_label_en == expected_en

    # 2. List execution runs / detail page
    runs_resp = await automations_app.list_automation_runs(
        principal=principal,
        scope=None,
        limit=20,
        offset=0,
        thread_id=None,
        status=None,
        wait_reason=None,
    )
    assert len(runs_resp.executions) == 1
    run_summary = runs_resp.executions[0].delivery_summary
    assert run_summary is not None
    assert run_summary.status == expected_delivery_status
    assert run_summary.failure_notice_status == receipt_status
    assert run_summary.failure_notice_label_zh == expected_zh
    assert run_summary.failure_notice_label_en == expected_en

    # 3. Direct model enrichment path also stays resilient if a malformed dict ever arrives
    bad_exec = AutomationExecutionResponse.model_validate(exec_row)
    eds_mod._set_delivery_summary(bad_exec, {"status": "totally_unknown_status"})
    assert bad_exec.delivery_summary is None
