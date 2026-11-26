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
    """Production default writer persists receipts via exec_db.record_failure_receipt (never overwriting delivery_result)."""
    from src.dalton import capabilities as cap_mod

    monkeypatch.setattr(cap_mod, "has_capability", create_autospec(cap_mod.has_capability, return_value=True))
    receipt_spy = create_autospec(exec_db.record_failure_receipt, return_value=None)
    delivery_spy = create_autospec(exec_db.record_delivery, return_value=None)
    monkeypatch.setattr(exec_db, "record_failure_receipt", receipt_spy)
    monkeypatch.setattr(exec_db, "record_delivery", delivery_spy)

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
    receipt_spy.assert_awaited_once()
    delivery_spy.assert_not_awaited()
    assert receipt_spy.await_args.args[0] == "exec-prod-writer"
    assert receipt_spy.await_args.args[1]["receipt_status"] == "suppressed"


@pytest.mark.asyncio
async def test_failure_receipt_outbox_held_pending_persists_and_updates_when_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When settlement leaves a failed run to the hook outbox (held_pending=True),
    on_automation_failed persists a 'pending' receipt separately via record_failure_receipt;
    even if outbox already wrote success before the receipt write, the combined rows converge to 'delivered'."""
    receipt_spy = create_autospec(exec_db.record_failure_receipt, return_value=None)
    delivery_spy = create_autospec(exec_db.record_delivery, return_value=None)
    monkeypatch.setattr(exec_db, "record_failure_receipt", receipt_spy)
    monkeypatch.setattr(exec_db, "record_delivery", delivery_spy)

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
    receipt_spy.assert_awaited_once()
    delivery_spy.assert_not_awaited()
    saved_receipt = receipt_spy.await_args.args[1]
    pending_summary = summarize_execution_delivery(
        delivery_result=[saved_receipt],
        execution_status="failed",
    )
    assert pending_summary is not None
    assert pending_summary["status"] == "retrying"
    assert pending_summary["failure_notice_status"] == "pending"

    # Simulate outbox having written success alongside the separately persisted pending receipt.
    settled_summary = summarize_execution_delivery(
        delivery_result=[{"method": "slack:T/C", "success": True}, saved_receipt],
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


@pytest.mark.asyncio
async def test_failure_notice_sends_feishu_card_and_preserves_per_target_bridge_errors() -> None:
    """r4 High #1: quota-429 with Feishu-only target sends a Feishu Card 2.0 payload, and
    preserves per-target bridge errors (e.g. feishu_card_required) without downgrading to dispatch_failed."""
    sent_calls: list[dict] = []

    async def _fake_bridge(
        targets: list[str],
        text: str,
        card: dict | None = None,
        *,
        images: list[dict] | None = None,
        run_id: str | None = None,
    ) -> dict:
        sent_calls.append({"targets": list(targets), "text": text, "card": card, "run_id": run_id})
        if card and card.get("schema") == "2.0":
            return {"ok": True, "results": [{"target": targets[0], "channel": "feishu", "ok": True}]}
        return {
            "ok": False,
            "results": [
                {"target": targets[0], "channel": "feishu", "ok": False, "error": "feishu_card_required"}
            ],
        }

    recorded: list[tuple[str, list[dict]]] = []

    async def _record(execution_id: str, entries: list[dict]) -> None:
        recorded.append((execution_id, entries))

    resolve_enabled = create_autospec(notify_dispatch.resolve_dalton_notify_enabled, return_value=True)
    resolve_overrides = create_autospec(notify_dispatch.resolve_dalton_notify_overrides, return_value={})
    resolve_targets = create_autospec(
        notify_dispatch.resolve_dalton_notify_targets, return_value=["feishu:ou_feishu_only"]
    )
    resolve_routes = create_autospec(notify_dispatch.resolve_dalton_notify_routes, return_value={})
    gate_spy = create_autospec(
        notify_dispatch._resolve_unit_managed_notify_gate,
        return_value=(True, None, None, False),
    )

    # 1. Production bridge path: Feishu Card 2.0 is built & passed, so Feishu target succeeds!
    receipt = await notify_dispatch.on_automation_failed(
        automation={
            "automation_id": "auto-feishu-429",
            "user_id": "u-1",
            "name": "dalton-feishu-move-up",
            "trigger_type": "price",
        },
        execution_id="exec-feishu-card-ok",
        outcome=Outcome.LIMITED.value,
        failure_reason="usage_limit",
        error="HTTP 429 quota exceeded",
        thread_id="thread-abc",
        run_id="run-123",
        resolve_enabled_fn=resolve_enabled,
        resolve_overrides_fn=resolve_overrides,
        resolve_targets_fn=resolve_targets,
        resolve_routes_fn=resolve_routes,
        resolve_unit_gate_fn=gate_spy,
        post_notify_fn=_fake_bridge,
        record_delivery_fn=_record,
        frontend_url_fn=lambda: "https://dalton.example.com",
    )

    assert receipt is not None
    assert receipt["receipt_status"] == "delivered"
    assert len(sent_calls) == 1
    assert sent_calls[0]["targets"] == ["feishu:ou_feishu_only"]
    assert isinstance(sent_calls[0]["card"], dict)
    assert sent_calls[0]["card"]["schema"] == "2.0"
    assert sent_calls[0]["card"]["header"]["template"] == "red"
    assert sent_calls[0]["run_id"] == "run-123"

    # 2. If bridge still returns HTTP 200 with per-target feishu_card_required, preserve exact reason!
    recorded.clear()

    async def _bridge_card_required(
        targets: list[str],
        text: str,
        card: dict | None = None,
        *,
        run_id: str | None = None,
    ) -> dict:
        return {
            "ok": False,
            "results": [
                {
                    "target": targets[0],
                    "channel": "feishu",
                    "ok": False,
                    "error": "feishu_card_required",
                }
            ],
        }

    enqueue_spy = create_autospec(notify_dispatch._enqueue_failure_notice_retries, return_value=True)
    fail_res = await notify_dispatch.on_automation_failed(
        automation={
            "automation_id": "auto-feishu-429",
            "user_id": "u-1",
            "name": "dalton-feishu-move-up",
            "trigger_type": "price",
        },
        execution_id="exec-feishu-card-req",
        outcome=Outcome.LIMITED.value,
        failure_reason="usage_limit",
        error="HTTP 429 quota exceeded",
        resolve_enabled_fn=resolve_enabled,
        resolve_overrides_fn=resolve_overrides,
        resolve_targets_fn=resolve_targets,
        resolve_routes_fn=resolve_routes,
        resolve_unit_gate_fn=gate_spy,
        post_notify_fn=_bridge_card_required,
        record_delivery_fn=_record,
        enqueue_retry_fn=enqueue_spy,
    )

    assert fail_res is not None
    assert fail_res["status"] == "failed"
    assert fail_res["error_code"] == "feishu_card_required"
    enqueue_spy.assert_not_awaited()
    assert len(recorded) == 1
    saved_entry = recorded[0][1][0]
    assert saved_entry["address"] == "feishu:ou_feishu_only"
    assert saved_entry["error_code"] == "feishu_card_required"
    assert saved_entry["error"] == "feishu_card_required"
    summary = summarize_execution_delivery(delivery_result=recorded[0][1], execution_status="failed")
    assert summary is not None
    assert summary["status"] == "failed"
    assert summary["failure_code"] == "feishu_card_required"
    assert summary.get("failure_notice_status") is None


@pytest.mark.asyncio
async def test_failure_notice_enqueue_retry_passes_card_via_payload_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_enqueue_failure_notice_retries must pass `payload=` (including card) to claim_target_delivery."""
    claim_spy = create_autospec(notify_dispatch.claim_target_delivery, return_value=True)
    mark_spy = create_autospec(notify_dispatch.mark_target_delivery, return_value=True)
    monkeypatch.setattr(notify_dispatch, "claim_target_delivery", claim_spy)
    monkeypatch.setattr(notify_dispatch, "mark_target_delivery", mark_spy)

    card = notify_dispatch.build_feishu_failure_notice_card(
        auto_name="Daily Alert",
        outcome="limited",
        failure_reason="usage_limit",
        error="HTTP 429",
    )
    enqueued = await notify_dispatch._enqueue_failure_notice_retries(
        execution_id="exec-retry-card",
        automation_id="auto-1",
        user_id="u-1",
        targets=["feishu:ou_retry_1"],
        notice_text="failed text",
        error_code="http_503",
        run_id="run-1",
        notice_card=card,
        automation_name="Daily Alert",
    )

    assert enqueued is True
    claim_spy.assert_awaited_once()
    kwargs = claim_spy.await_args.kwargs
    assert "payload" in kwargs
    assert kwargs["payload"]["card"] == card
    assert kwargs["payload"]["source"] == "automation_failure_notice"
    mark_spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_after_settling_held_pending_persists_receipt_before_nudging_drainer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 High #2: _after_settling must write the held_pending receipt before calling _nudge_drainer."""
    from src.dalton.database import automation_recycle
    from src.server.services import automation_delivery

    events: list[str] = []

    monkeypatch.setattr(
        automation_recycle,
        "automation_is_soft_deleted",
        create_autospec(automation_recycle.automation_is_soft_deleted, return_value=False),
    )
    monkeypatch.setattr(
        settlement_mod,
        "_nudge_drainer",
        lambda: events.append("nudge_drainer"),
    )

    async def _fake_on_failed(**kwargs):
        assert kwargs.get("held_pending") is True
        events.append("on_automation_failed")
        return {"receipt_status": "pending"}

    monkeypatch.setattr(notify_dispatch, "on_automation_failed", _fake_on_failed)
    monkeypatch.setattr(settlement_mod, "safe_add", create_autospec(settlement_mod.safe_add))

    delivery = SimpleNamespace(id="del-1", targets=["slack:T/C"], held=True)
    await settlement_mod._after_settling(
        outcome=Outcome.FAILED,
        automation={"automation_id": "auto-race-1", "user_id": "u-1", "trigger_type": "cron"},
        execution_id="exec-race-1",
        row={"settled_from": "running", "previous_failure_reason": None},
        thread_id="t-1",
        run_id="r-1",
        workspace_id="ws-1",
        error="boom",
        delivery=delivery,
        final_text=None,
        finish_left=True,
    )

    assert events == ["on_automation_failed", "nudge_drainer"]


@pytest.mark.asyncio
async def test_record_failure_receipt_and_record_delivery_sql_preserve_each_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 High #2: record_failure_receipt and record_delivery issue non-clobbering JSONB lane updates."""
    executed_sql: list[tuple[str, tuple]] = []

    class _FakeCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql: str, params: tuple) -> None:
            executed_sql.append((sql, params))

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    class _FakeCtx:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(exec_db, "get_db_connection", lambda conn=None: _FakeCtx())

    receipt = notify_dispatch.build_failure_notification_receipt(
        receipt_status="pending",
        outcome="failed",
        reason="outbox_queued",
    )
    await exec_db.record_failure_receipt("exec-sep-1", receipt)
    await exec_db.record_delivery("exec-sep-1", [{"method": "slack:T/C", "success": True}])

    assert len(executed_sql) == 2
    receipt_sql = executed_sql[0][0]
    delivery_sql = executed_sql[1][0]
    # record_failure_receipt filters OUT old failure_receipt entries while preserving non-receipt delivery rows
    assert "WHERE NOT" in receipt_sql and "failure_receipt" in receipt_sql
    # record_delivery preserves existing failure_receipt entries while replacing non-receipt delivery rows
    assert "WHERE jsonb_typeof(elem) = 'object'" in delivery_sql and "failure_receipt" in delivery_sql


def test_failure_receipt_converges_away_from_pending_when_retries_exhaust_or_skip() -> None:
    """r4 High #2: once outbox or channel retries exhaust (or skip), summary must NOT remain 'pending' (待发)."""
    pending_receipt = notify_dispatch.build_failure_notification_receipt(
        receipt_status="pending",
        outcome="limited",
        failure_reason="usage_limit",
        reason="http_503",
        targets=["feishu:ou_1"],
        channel="feishu",
    )

    # 1. Channel retry exhausted in dalton_notify_deliveries
    ch_exhausted = summarize_execution_delivery(
        notify_rows=[
            {
                "channel": "feishu",
                "target": "feishu:ou_1",
                "status": "failed",
                "attempt_count": 3,
                "error_code": "http_503",
            }
        ],
        delivery_result=[pending_receipt],
        execution_status="failed",
        max_attempts=3,
    )
    assert ch_exhausted is not None
    assert ch_exhausted["status"] == "failed"
    assert ch_exhausted.get("failure_notice_status") is None
    assert ch_exhausted.get("failure_notification_receipt") is None
    ExecutionDeliverySummary.model_validate(ch_exhausted)

    # 2. Outbox retry exhausted (retrying=False on delivery entry alongside pending receipt)
    outbox_exhausted = summarize_execution_delivery(
        delivery_result=[
            {
                "method": "slack:T/C",
                "success": False,
                "retrying": False,
                "error": "unconfirmed: gateway timeout",
            },
            pending_receipt,
        ],
        execution_status="failed",
    )
    assert outbox_exhausted is not None
    assert outbox_exhausted["status"] == "failed"
    assert outbox_exhausted.get("failure_notice_status") is None
    ExecutionDeliverySummary.model_validate(outbox_exhausted)

    # 3. Channel retry skipped due to toggle_off -> converges from pending to suppressed
    ch_suppressed = summarize_execution_delivery(
        notify_rows=[
            {
                "channel": "feishu",
                "target": "feishu:ou_1",
                "status": "skipped",
                "attempt_count": 1,
                "error_code": "toggle_off",
            }
        ],
        delivery_result=[pending_receipt],
        execution_status="failed",
    )
    assert ch_suppressed is not None
    assert ch_suppressed["status"] == "skipped"
    assert ch_suppressed["failure_notice_status"] == "suppressed"
    assert ch_suppressed["failure_notice_label_zh"] == "已抑制"
    ExecutionDeliverySummary.model_validate(ch_suppressed)

    # 4. Channel retry skipped due to target_removed -> converges from pending to unconfigured
    ch_unconf = summarize_execution_delivery(
        notify_rows=[
            {
                "channel": "feishu",
                "target": "feishu:ou_1",
                "status": "skipped",
                "attempt_count": 1,
                "error_code": "target_removed",
            }
        ],
        delivery_result=[pending_receipt],
        execution_status="failed",
    )
    assert ch_unconf is not None
    assert ch_unconf["status"] == "skipped"
    assert ch_unconf["failure_notice_status"] == "unconfigured"
    assert ch_unconf["failure_notice_label_zh"] == "未配置"
    ExecutionDeliverySummary.model_validate(ch_unconf)
