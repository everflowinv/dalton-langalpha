"""Unit tests for mutual exclusion between upstream delivery_config.methods and Dalton notify."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import Any
from unittest.mock import create_autospec

from fastapi import HTTPException
import pytest

import src.dalton.database.automation_recycle as recycle_db
import src.dalton.llm.model_prefs as model_prefs_mod
import src.dalton.notify.dispatch as notify_dispatch
import src.dalton.services.automation_recycle as recycle_svc
import src.dalton.tools.notify_tools as notify_tools_mod
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
from src.server.database import user as user_db
from src.server.database import pool as pool_mod
from src.server.models.automation import AutomationCreate, AutomationUpdate, DeliveryConfig
import src.server.services.automation_delivery as automation_delivery
import src.server.services.automation_settlement as settlement_mod
import src.server.services.automations.lifecycle as lifecycle_mod
from src.server.services.webhook_client import WebhookClient
from src.tools.messaging import tools as messaging


def _held_delivery(execution_id: str, entry: str = "feishu") -> automation_delivery.Delivery:
    return automation_delivery.Delivery(
        id=execution_id,
        targets=[
            automation_delivery.Target(
                entry=entry,
                address="feishu:dm:ou_alice",
                name="Alice DM",
                ok=True,
            )
        ],
    )


def _wire_real_settlement_send_spies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user_routes: dict[str, list[str]],
    user_overrides: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Attach autospec spies to the actual send interfaces on both upstream and Dalton paths."""
    soft_deleted_mock = create_autospec(
        recycle_db.automation_is_soft_deleted,
        return_value=False,
    )
    gateway_request_mock = create_autospec(
        messaging.gateway_request,
        return_value=messaging.GatewayAnswer(
            status=200,
            data={
                "targets": [
                    {
                        "entry": "feishu",
                        "address": "feishu:dm:ou_alice",
                        "name": "Alice DM",
                        "reached": False,
                        "via": "fallback",
                        "error": None,
                    }
                ]
            },
            text="",
        ),
    )
    record_delivery_mock = create_autospec(exec_db.record_delivery, return_value=None)
    keep_record_mock = create_autospec(settlement_mod._keep_record, return_value=True)
    nudge_mock = create_autospec(settlement_mod._nudge_drainer, return_value=None)
    webhook_fire_mock = create_autospec(WebhookClient.fire_event, return_value=None)

    prefs_payload = {
        "model_preference": {
            "dalton_notify_enabled": True,
            "dalton_notify_targets": ["feishu:user:ou_alice"],
            "dalton_notify_overrides": dict(user_overrides or {}),
            "dalton_notify_routes": dict(user_routes),
        },
        "other_preference": {},
    }
    get_prefs_mock = create_autospec(
        model_prefs_mod.get_user_preferences,
        return_value=prefs_payload,
    )
    unit_gate_mock = create_autospec(
        notify_dispatch._resolve_unit_managed_notify_gate,
        return_value=(True, None, None, True),
    )
    claim_gateway_mock = create_autospec(
        notify_dispatch.claim_target_delivery_with_attempt,
        return_value=notify_dispatch.ClaimAttempt(True, 1, "deliv-uuid-1"),
    )
    dalton_gateway_send_mock = create_autospec(
        notify_dispatch.post_automation_gateway_notify,
        return_value={"ok": True},
    )
    dalton_bridge_send_mock = create_autospec(
        notify_dispatch._invoke_post_notify,
        return_value={"ok": True},
    )
    mark_target_mock = create_autospec(
        notify_dispatch._invoke_mark_delivery,
        return_value=True,
    )
    verdict_mock = create_autospec(
        notify_dispatch.get_verdict_for_execution,
        return_value=None,
    )
    via_gateway_mock = create_autospec(
        notify_dispatch.notify_via_gateway_enabled,
        return_value=True,
    )

    monkeypatch.setattr(recycle_db, "automation_is_soft_deleted", soft_deleted_mock)
    monkeypatch.setattr(messaging, "gateway_request", gateway_request_mock)
    monkeypatch.setattr(exec_db, "record_delivery", record_delivery_mock)
    monkeypatch.setattr(settlement_mod, "_keep_record", keep_record_mock)
    monkeypatch.setattr(settlement_mod, "_nudge_drainer", nudge_mock)
    monkeypatch.setattr(WebhookClient, "fire_event", webhook_fire_mock)
    monkeypatch.setattr(model_prefs_mod, "get_user_preferences", get_prefs_mock)
    monkeypatch.setattr(notify_dispatch, "_resolve_unit_managed_notify_gate", unit_gate_mock)
    monkeypatch.setattr(notify_dispatch, "claim_target_delivery_with_attempt", claim_gateway_mock)
    monkeypatch.setattr(notify_dispatch, "post_automation_gateway_notify", dalton_gateway_send_mock)
    monkeypatch.setattr(notify_dispatch, "_invoke_post_notify", dalton_bridge_send_mock)
    monkeypatch.setattr(notify_dispatch, "_invoke_mark_delivery", mark_target_mock)
    monkeypatch.setattr(notify_dispatch, "get_verdict_for_execution", verdict_mock)
    monkeypatch.setattr(notify_dispatch, "notify_via_gateway_enabled", via_gateway_mock)
    # Keep on_automation_settled's keyword-only default get_verdict_fn from touching DB
    monkeypatch.setitem(
        notify_dispatch.on_automation_settled.__kwdefaults__,
        "get_verdict_fn",
        verdict_mock,
    )

    return {
        "gateway_request": gateway_request_mock,
        "dalton_gateway_send": dalton_gateway_send_mock,
        "dalton_bridge_send": dalton_bridge_send_mock,
        "claim_gateway": claim_gateway_mock,
        "nudge": nudge_mock,
        "webhook_fire": webhook_fire_mock,
    }


@pytest.mark.asyncio
async def test_settlement_positive_control_and_builtin_held_delivery_count_real_send_interfaces(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spy hooks real send interfaces: Dalton sends once without held delivery, and skips when held delivery exists."""
    auto_id = "11111111-1111-4111-8111-111111111111"
    automation: dict[str, Any] = {
        "automation_id": auto_id,
        "user_id": "user-alice",
        "workspace_id": "ws-nvda",
        "name": "dalton-nvda-earn-2026q3-t0",
        "trigger_type": "cron",
        "delivery_config": {"methods": ["feishu"]},
        "metadata": {"ticker": "NVDA", "dalton_kind": "earnings_t0"},
    }
    row: dict[str, Any] = {
        "settled_from": "running",
        "result_excerpt": "NVDA beat consensus.",
    }

    spies = _wire_real_settlement_send_spies(monkeypatch, user_routes={})

    # 1. Positive control: when execution has NO upstream delivery, Dalton's real send interface is called once
    await settlement_mod._after_settling(
        settlement_mod.Outcome.COMPLETED,
        {**automation, "delivery_config": {}},
        "exec-positive-control",
        row,
        thread_id="thread-ctrl",
        run_id="run-ctrl",
        workspace_id="ws-nvda",
        error=None,
        delivery=None,
        final_text="NVDA beat consensus.",
        finish_left=False,
    )
    assert spies["gateway_request"].await_count == 0
    assert spies["dalton_gateway_send"].await_count == 1
    assert spies["dalton_bridge_send"].await_count == 0

    spies["gateway_request"].reset_mock()
    spies["dalton_gateway_send"].reset_mock()
    spies["claim_gateway"].reset_mock()

    # 2. Built-in task with held delivery: upstream gateway_request sends once, Dalton sends zero times
    execution_id = "exec-builtin-1"
    with caplog.at_level(logging.INFO, logger=notify_dispatch.__name__):
        await settlement_mod._after_settling(
            settlement_mod.Outcome.COMPLETED,
            automation,
            execution_id,
            row,
            thread_id="thread-1",
            run_id="run-1",
            workspace_id="ws-nvda",
            error=None,
            delivery=_held_delivery(execution_id),
            final_text="NVDA beat consensus.",
            finish_left=False,
        )

    assert spies["gateway_request"].await_count == 1
    assert spies["dalton_gateway_send"].await_count == 0
    assert spies["dalton_bridge_send"].await_count == 0
    assert spies["claim_gateway"].await_count == 0
    assert (
        spies["gateway_request"].await_count
        + spies["dalton_gateway_send"].await_count
        + spies["dalton_bridge_send"].await_count
        == 1
    )
    assert any(
        "upstream delivery" in rec.getMessage() and execution_id in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_settlement_outbox_queue_and_midrun_switch_deliver_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mid-run switch from web delivery to Dalton route still delivers once through the normal outbox queue."""
    auto_id = "22222222-2222-4222-8222-222222222222"
    execution_id = "exec-custom-switch-1"
    # During the run, the user cleared delivery_config.methods and configured a Dalton route,
    # while the execution itself was already registered as held with the messaging gateway.
    automation_after_switch: dict[str, Any] = {
        "automation_id": auto_id,
        "user_id": "user-alice",
        "workspace_id": "ws-custom",
        "name": "Morning Semiconductor Brief",
        "trigger_type": "cron",
        "delivery_config": {"methods": []},
        "metadata": {},
    }
    held = _held_delivery(execution_id)
    row: dict[str, Any] = {
        "settled_from": "running",
        "result_excerpt": "Morning brief ready.",
        "delivery": automation_delivery.stamp(held),
    }

    spies = _wire_real_settlement_send_spies(
        monkeypatch,
        user_routes={f"automation:{auto_id}": ["feishu:user:ou_alice"]},
        user_overrides={f"automation:{auto_id}": True},
    )
    get_auto_mock = create_autospec(
        auto_db.get_automation,
        return_value=automation_after_switch,
    )
    monkeypatch.setattr(auto_db, "get_automation", get_auto_mock)

    # Normal settlement queue path: finish_left=True nudges drainer; Dalton skips because execution has held delivery
    with caplog.at_level(logging.INFO, logger=notify_dispatch.__name__):
        await settlement_mod._after_settling(
            settlement_mod.Outcome.COMPLETED,
            automation_after_switch,
            execution_id,
            row,
            thread_id="thread-2",
            run_id="run-2",
            workspace_id="ws-custom",
            error=None,
            delivery=held,
            final_text="Morning brief ready.",
            finish_left=True,
        )

    assert spies["nudge"].call_count == 1
    assert spies["dalton_gateway_send"].await_count == 0
    assert spies["dalton_bridge_send"].await_count == 0
    assert spies["claim_gateway"].await_count == 0

    # Now the hook outbox worker runs the queued finish job -> upstream sends exactly once
    outbox_job: dict[str, Any] = {
        "hook_outbox_id": "job-1",
        "conversation_thread_id": "thread-2",
        "run_id": "run-2",
        "attempts": 1,
        "payload": {
            "execution_id": execution_id,
            "automation_id": auto_id,
            "user_id": "user-alice",
            "run_id": "run-2",
            "status": "completed",
            "final_text": "Morning brief ready.",
            "delivery": automation_delivery.stamp(held),
            "answered": False,
        },
    }
    await settlement_mod._finish_held_run(outbox_job)

    assert spies["gateway_request"].await_count == 1
    assert (
        spies["gateway_request"].await_count
        + spies["dalton_gateway_send"].await_count
        + spies["dalton_bridge_send"].await_count
        == 1
    )
    assert any(
        "upstream delivery" in rec.getMessage() and execution_id in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_settlement_midrun_added_delivery_without_registered_execution_still_sends_dalton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When an execution started without upstream delivery, adding delivery_config mid-run does not drop Dalton notify."""
    auto_id = "22222222-2222-4222-8222-222222222223"
    automation_now: dict[str, Any] = {
        "automation_id": auto_id,
        "user_id": "user-alice",
        "workspace_id": "ws-custom",
        "name": "Morning Semiconductor Brief",
        "trigger_type": "cron",
        "delivery_config": {"methods": ["feishu"]},
        "metadata": {},
    }
    spies = _wire_real_settlement_send_spies(
        monkeypatch,
        user_routes={f"automation:{auto_id}": ["feishu:user:ou_alice"]},
        user_overrides={f"automation:{auto_id}": True},
    )

    await settlement_mod._after_settling(
        settlement_mod.Outcome.COMPLETED,
        automation_now,
        "exec-no-upstream-1",
        {"settled_from": "running", "result_excerpt": "Morning brief ready."},
        thread_id="thread-3",
        run_id="run-3",
        workspace_id="ws-custom",
        error=None,
        delivery=None,
        final_text="Morning brief ready.",
        finish_left=False,
    )

    assert spies["gateway_request"].await_count == 0
    assert spies["dalton_gateway_send"].await_count == 1


@pytest.mark.asyncio
async def test_evaluate_user_notify_eligibility_uses_execution_delivery_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Retry/eligibility checks skip based on this execution's registered delivery, not mutable task delivery_config."""
    auto_id = "33333333-3333-4333-8333-333333333333"
    resolve_enabled_mock = create_autospec(
        notify_dispatch.resolve_dalton_notify_enabled,
        return_value=True,
    )
    resolve_targets_mock = create_autospec(
        notify_dispatch.resolve_dalton_notify_targets,
        return_value=["feishu:user:ou_alice"],
    )
    resolve_overrides_mock = create_autospec(
        notify_dispatch.resolve_dalton_notify_overrides,
        return_value={f"automation:{auto_id}": True},
    )
    resolve_routes_mock = create_autospec(
        notify_dispatch.resolve_dalton_notify_routes,
        return_value={f"automation:{auto_id}": ["feishu:user:ou_alice"]},
    )

    # 1. Execution has held delivery stamp -> skipped even though automation.delivery_config is now empty
    with caplog.at_level(logging.INFO, logger=notify_dispatch.__name__):
        allowed, reason, targets = await notify_dispatch.evaluate_user_notify_eligibility(
            "user-alice",
            {
                "automation_id": auto_id,
                "name": "Custom Task",
                "delivery_config": {},
            },
            execution_delivery={
                "id": "exec-held-1",
                "targets": [{"entry": "feishu", "address": "feishu:dm:ou_alice", "ok": True}],
            },
            resolve_enabled_fn=resolve_enabled_mock,
            resolve_overrides_fn=resolve_overrides_mock,
            resolve_targets_fn=resolve_targets_mock,
            resolve_routes_fn=resolve_routes_mock,
        )
    assert (allowed, reason, targets) == (False, "held_delivery_configured", [])
    resolve_enabled_mock.assert_not_awaited()
    assert any("upstream delivery" in rec.getMessage() for rec in caplog.records)

    # 2. Execution has webhook delivery_result -> skipped
    allowed_wh, reason_wh, targets_wh = await notify_dispatch.evaluate_user_notify_eligibility(
        "user-alice",
        {"automation_id": auto_id, "name": "Custom Task", "delivery_config": {}},
        execution_delivery_result=[{"method": "slack", "success": True}],
        resolve_enabled_fn=resolve_enabled_mock,
        resolve_overrides_fn=resolve_overrides_mock,
        resolve_targets_fn=resolve_targets_mock,
        resolve_routes_fn=resolve_routes_mock,
    )
    assert (allowed_wh, reason_wh, targets_wh) == (False, "held_delivery_configured", [])

    # 3. Execution had NO upstream delivery (only unsent failure) -> NOT skipped even if delivery_config was edited later
    allowed_ok, reason_ok, targets_ok = await notify_dispatch.evaluate_user_notify_eligibility(
        "user-alice",
        {"automation_id": auto_id, "name": "Custom Task", "delivery_config": {"methods": ["feishu"]}},
        execution_delivery={"id": "exec-unsent", "targets": [{"entry": "feishu", "ok": False}]},
        execution_delivery_result=[
            {
                "method": "feishu",
                "address": None,
                "name": None,
                "success": False,
                "via": None,
                "error": "No chat",
            }
        ],
        resolve_enabled_fn=resolve_enabled_mock,
        resolve_overrides_fn=resolve_overrides_mock,
        resolve_targets_fn=resolve_targets_mock,
        resolve_routes_fn=resolve_routes_mock,
    )
    assert allowed_ok is True
    assert reason_ok is None
    assert targets_ok == ["feishu:user:ou_alice"]


@pytest.mark.asyncio
async def test_lifecycle_save_validation_rejects_conflicting_delivery_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_automation and prepare_owned_automation_update reject delivery_config.methods on built-in or routed tasks."""
    ws_id = "44444444-4444-4444-8444-444444444444"
    auto_id = "55555555-5555-4555-8555-555555555555"

    placement_mock = create_autospec(
        lifecycle_mod._placement,
        return_value=ws_id,
    )
    ownership_mock = create_autospec(
        lifecycle_mod.require_target_ownership,
        return_value=None,
    )
    check_claude_mock = create_autospec(lifecycle_mod.check_claude_code_hosted, return_value=None)
    reconcile_effort_mock = create_autospec(
        lifecycle_mod._reconcile_reasoning_effort,
        return_value=(False, None),
    )
    check_delivery_mock = create_autospec(
        lifecycle_mod.check_delivery,
        return_value=["feishu"],
    )
    create_db_mock = create_autospec(auto_db.create_automation)

    monkeypatch.setattr(lifecycle_mod, "_placement", placement_mock)
    monkeypatch.setattr(lifecycle_mod, "require_target_ownership", ownership_mock)
    monkeypatch.setattr(lifecycle_mod, "check_claude_code_hosted", check_claude_mock)
    monkeypatch.setattr(lifecycle_mod, "_reconcile_reasoning_effort", reconcile_effort_mock)
    monkeypatch.setattr(lifecycle_mod, "check_delivery", check_delivery_mock)
    monkeypatch.setattr(auto_db, "create_automation", create_db_mock)

    # 1. Creating a built-in dalton-* automation with delivery_config.methods is refused
    create_payload = AutomationCreate(
        name="dalton-nvda-earn-2026q3-t0",
        trigger_type="cron",
        cron_expression="0 9 * * *",
        timezone="UTC",
        instruction="Check earnings",
        workspace_id=ws_id,
        delivery_config=DeliveryConfig(methods=["feishu"]),
    )
    with pytest.raises(lifecycle_mod.AutomationRefusal) as create_exc:
        await lifecycle_mod.create_automation(
            user_id="user-alice",
            data=create_payload,
            delivery_checked=True,
        )
    assert create_exc.value.field == "delivery_config"
    assert "Dalton" in str(create_exc.value)
    create_db_mock.assert_not_awaited()

    # 2. Updating a custom automation that has a Dalton notify route to add delivery_config.methods is refused
    current_custom = {
        "automation_id": auto_id,
        "user_id": "user-alice",
        "workspace_id": ws_id,
        "name": "Custom Brief",
        "trigger_type": "cron",
        "cron_expression": "0 9 * * *",
        "timezone": "UTC",
        "status": "active",
        "instruction": "Summarize news",
        "thread_strategy": "new",
        "conversation_thread_id": None,
        "llm_model": None,
        "reasoning_effort": None,
        "delivery_config": {},
        "metadata": {},
    }
    get_auto_mock = create_autospec(auto_db.get_automation, return_value=current_custom)
    monkeypatch.setattr(auto_db, "get_automation", get_auto_mock)

    with pytest.raises(lifecycle_mod.AutomationRefusal) as update_exc:
        await lifecycle_mod.prepare_owned_automation_update(
            automation_id=auto_id,
            user_id="user-alice",
            fields={"delivery_config": {"methods": ["feishu"]}},
            model_pref={"dalton_notify_routes": {f"automation:{auto_id}": ["feishu:user:ou_alice"]}},
            delivery_checked=True,
        )
    assert update_exc.value.field == "delivery_config"
    assert "Dalton" in str(update_exc.value)

    # 3. Updating a custom automation when its Dalton route is explicitly empty [] is allowed
    prepared = await lifecycle_mod.prepare_owned_automation_update(
        automation_id=auto_id,
        user_id="user-alice",
        fields={"delivery_config": {"methods": ["feishu"]}},
        model_pref={"dalton_notify_routes": {f"automation:{auto_id}": []}},
        delivery_checked=True,
    )
    assert prepared is not None
    assert prepared.update_kwargs["delivery_config"] == {"methods": ["feishu"]}


@pytest.mark.asyncio
async def test_in_lock_recheck_blocks_concurrent_automation_and_preference_race() -> None:
    """Both save entries re-read the peer table inside the locked transaction regardless of which fields changed."""
    auto_id = "77777777-7777-4777-8777-777777777777"
    ws_id = "44444444-4444-4444-8444-444444444444"
    sql_seen: list[str] = []

    class _TxCursor:
        def __init__(self, *, pref_row: dict[str, Any] | None, auto_rows: list[dict[str, Any]]):
            self._pref_row = pref_row
            self._auto_rows = auto_rows

        async def __aenter__(self) -> "_TxCursor":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            _ = params
            sql_seen.append(sql)

        async def fetchone(self) -> dict[str, Any] | None:
            return self._pref_row

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._auto_rows)

    class _TxConn:
        def __init__(self, *, pref_row: dict[str, Any] | None = None, auto_rows: list[dict[str, Any]] | None = None):
            self._pref_row = pref_row
            self._auto_rows = auto_rows or []

        def cursor(self, **kwargs: Any) -> _TxCursor:
            _ = kwargs
            return _TxCursor(pref_row=self._pref_row, auto_rows=self._auto_rows)

    # 1. Automation update path: locked_row == current (unchanged automation row) and stale pref_for_lock is empty,
    #    but a concurrent preference write committed dalton_notify_routes before we acquired the lock.
    #    Even when updating delivery_config (or another field on a task with delivery_config), locked recheck catches it.
    current_row = {
        "automation_id": auto_id,
        "user_id": "user-alice",
        "workspace_id": ws_id,
        "name": "Custom Brief",
        "trigger_type": "cron",
        "cron_expression": "0 9 * * *",
        "timezone": "UTC",
        "status": "active",
        "instruction": "Summarize news",
        "thread_strategy": "new",
        "conversation_thread_id": None,
        "llm_model": None,
        "reasoning_effort": None,
        "delivery_config": {},
        "metadata": {},
    }
    locked_conn_with_new_route = _TxConn(
        pref_row={
            "model_preference": {
                "dalton_notify_routes": {f"automation:{auto_id}": ["feishu:user:ou_alice"]}
            }
        }
    )
    update_model = AutomationUpdate(delivery_config=DeliveryConfig(methods=["feishu"]))
    with pytest.raises(lifecycle_mod.AutomationRefusal) as race_auto_exc:
        await lifecycle_mod.recheck_locked_automation_update(
            user_id="user-alice",
            update=update_model,
            current=current_row,
            stored_in=ws_id,
            stored_pin=None,
            new_cron=None,
            new_tz=None,
            pref_for_lock={"dalton_notify_routes": {}},  # stale pre-lock cache!
            locked_row=dict(current_row),  # automation row itself did not change!
            effective_kwargs={"delivery_config": {"methods": ["feishu"]}},
            locked_conn=locked_conn_with_new_route,
        )
    assert race_auto_exc.value.field == "delivery_config"
    assert any("FROM user_preferences" in q for q in sql_seen)

    # Also verify non-delivery field update (e.g., description) rechecks conflict under lock
    row_with_delivery = {**current_row, "delivery_config": {"methods": ["feishu"]}}
    desc_update = AutomationUpdate(description="Updated desc")
    with pytest.raises(lifecycle_mod.AutomationRefusal):
        await lifecycle_mod.recheck_locked_automation_update(
            user_id="user-alice",
            update=desc_update,
            current=row_with_delivery,
            stored_in=ws_id,
            stored_pin=None,
            new_cron=None,
            new_tz=None,
            pref_for_lock={},
            locked_row=dict(row_with_delivery),
            effective_kwargs={"description": "Updated desc"},
            locked_conn=locked_conn_with_new_route,
        )

    # 2. Preference update path: inside the preference lock tx, validate_dalton_notify_routes_for_user
    #    locks & reads automations FOR SHARE on the same tx conn and rejects with 409 if delivery_config.methods won.
    sql_seen.clear()
    pref_tx_conn = _TxConn(
        auto_rows=[{"automation_id": auto_id, "delivery_config": {"methods": ["feishu"]}}]
    )
    with pytest.raises(HTTPException) as race_pref_exc:
        await model_prefs_mod.validate_dalton_notify_routes_for_user(
            "user-alice",
            {f"automation:{auto_id}": ["feishu:user:ou_alice"]},
            {},
            conn=pref_tx_conn,
        )
    assert race_pref_exc.value.status_code == 409
    assert "delivery_config.methods" in str(race_pref_exc.value.detail)
    assert any("FROM automations" in q and "FOR SHARE" in q for q in sql_seen)


@pytest.mark.asyncio
async def test_restore_automation_rejects_conflict_with_locked_dalton_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring a soft-deleted task with delivery_config.methods fails with 409 if a Dalton route was added."""
    auto_id = "88888888-8888-4888-8888-888888888888"
    deleted_row = {
        "automation_id": auto_id,
        "user_id": "user-alice",
        "workspace_id": "44444444-4444-4444-8444-444444444444",
        "name": "Custom Brief",
        "trigger_type": "cron",
        "cron_expression": "0 9 * * *",
        "timezone": "UTC",
        "status": "paused",
        "pre_delete_desired_status": "active",
        "deleted_at": "2026-11-28T00:00:00+00:00",
        "config_version": 2,
        "active_version": 2,
        "delivery_config": {"methods": ["feishu"]},
        "metadata": {},
        "file_name": "custom-brief.json",
    }

    class _FakeTx:
        async def __aenter__(self) -> "_FakeTx":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

    class _FakeDb:
        def transaction(self) -> _FakeTx:
            return _FakeTx()

    @asynccontextmanager
    async def _fake_conn(conn: Any = None):
        _ = conn
        yield _FakeDb()

    prepare_prefs_mock = create_autospec(
        recycle_svc._prepare_prefs,
        return_value={
            "model_preference": {
                "dalton_notify_routes": {f"automation:{auto_id}": ["feishu:user:ou_alice"]}
            }
        },
    )
    lock_recycle_mock = create_autospec(
        recycle_db.lock_automation_for_recycle,
        return_value=deleted_row,
    )
    update_recycle_mock = create_autospec(
        recycle_db.update_automation_recycle,
        return_value=True,
    )
    units_enabled_mock = create_autospec(
        lambda: False,
        return_value=False,
    )

    monkeypatch.setattr(recycle_svc, "get_db_connection", _fake_conn)
    monkeypatch.setattr(recycle_svc, "_prepare_prefs", prepare_prefs_mock)
    monkeypatch.setattr(recycle_db, "lock_automation_for_recycle", lock_recycle_mock)
    monkeypatch.setattr(recycle_db, "update_automation_recycle", update_recycle_mock)
    import src.dalton.settings as dalton_settings_mod

    monkeypatch.setattr(dalton_settings_mod, "is_coverage_units_enabled", units_enabled_mock)

    with pytest.raises(HTTPException) as restore_exc:
        await recycle_svc.restore_automation(
            auto_id,
            actor_user_id="user-alice",
            expected_config_version=2,
            owner_user_id="user-alice",
            company=False,
        )
    assert restore_exc.value.status_code == 409
    assert "Dalton" in str(restore_exc.value.detail)
    update_recycle_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_core_profile_without_channel_gateway_allows_web_delivery_but_keeps_report_profile_mutex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When CapabilityId.CHANNEL_GATEWAY is off (core profile), generic Dalton mutex is inactive while report_profile stays guarded."""
    monkeypatch.setenv("DALTON_PROFILE", "core")
    monkeypatch.delenv("DALTON_CAP_CHANNEL_GATEWAY", raising=False)
    auto_id = "99999999-9999-4999-8999-999999999999"

    from src.dalton.notify.delivery_mutex import (
        assert_no_automation_delivery_notify_conflict,
        check_route_held_delivery_conflict,
    )

    # 1. Built-in dalton-* task with Slack/Discord webhook delivery is allowed when CHANNEL_GATEWAY is off
    await assert_no_automation_delivery_notify_conflict(
        user_id="user-alice",
        automation={
            "automation_id": auto_id,
            "name": "dalton-nvda-earn-2026q3-t0",
            "metadata": {"dalton_kind": "earnings_t0"},
        },
        delivery_config={"methods": ["slack"]},
        model_pref={"dalton_notify_routes": {f"automation:{auto_id}": ["feishu:user:ou_alice"]}},
    )

    # 2. Custom task with route + Discord delivery is allowed when CHANNEL_GATEWAY is off
    await assert_no_automation_delivery_notify_conflict(
        user_id="user-alice",
        automation={"automation_id": auto_id, "name": "Custom Brief", "metadata": {}},
        delivery_config={"methods": ["discord"]},
        model_pref={"dalton_notify_routes": {f"automation:{auto_id}": ["feishu:user:ou_alice"]}},
    )
    assert (
        check_route_held_delivery_conflict(
            {"automation_id": auto_id, "delivery_config": {"methods": ["slack"]}},
            ["feishu:user:ou_alice"],
        )
        is None
    )

    # 3. Existing report_profile + delivery_config.methods mutex is STILL enforced
    with pytest.raises(lifecycle_mod.AutomationRefusal) as report_exc:
        await assert_no_automation_delivery_notify_conflict(
            user_id="user-alice",
            automation={
                "automation_id": auto_id,
                "name": "Report Task",
                "metadata": {
                    "report_profile": {
                        "profile": "market-digest-am",
                        "report_targets": ["feishu:chat:oc_group_1"],
                        "failure_targets": [],
                    }
                },
            },
            delivery_config={"methods": ["slack"]},
        )
    assert report_exc.value.field == "delivery_config"
    assert "report_profile" in str(report_exc.value)


@pytest.mark.asyncio
async def test_notify_route_save_entries_reject_tasks_with_delivery_config_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both dalton_notify_settings(set_route) and preferences route validation refuse tasks with delivery_config.methods."""
    auto_id = "66666666-6666-4666-8666-666666666666"
    task_with_delivery = {
        "automation_id": auto_id,
        "user_id": "user-alice",
        "name": "Custom Brief",
        "instruction": "Summarize market",
        "delivery_config": {"methods": ["feishu"]},
        "metadata": {},
    }

    # 1. Agent tool dalton_notify_settings(action="set_route") refuses non-empty targets before HITL
    list_autos_mock = create_autospec(
        auto_db.list_all_automations,
        return_value=[task_with_delivery],
    )
    resolve_enabled_mock = create_autospec(
        notify_tools_mod.resolve_dalton_notify_enabled,
        return_value=True,
    )
    resolve_targets_mock = create_autospec(
        notify_tools_mod.resolve_dalton_notify_targets,
        return_value=["feishu:user:ou_alice"],
    )
    resolve_labels_mock = create_autospec(
        notify_tools_mod.resolve_dalton_notify_target_labels,
        return_value={},
    )
    hitl_mock = create_autospec(notify_tools_mod.hitl_confirm, return_value=(True, {}))

    monkeypatch.setattr(auto_db, "list_all_automations", list_autos_mock)
    monkeypatch.setattr(notify_tools_mod, "resolve_dalton_notify_enabled", resolve_enabled_mock)
    monkeypatch.setattr(notify_tools_mod, "resolve_dalton_notify_targets", resolve_targets_mock)
    monkeypatch.setattr(notify_tools_mod, "resolve_dalton_notify_target_labels", resolve_labels_mock)
    monkeypatch.setattr(notify_tools_mod, "hitl_confirm", hitl_mock)

    cmd = await notify_tools_mod._handle_set_route(
        user_id="user-alice",
        automation=auto_id,
        targets=["feishu:user:ou_alice"],
        tool_call_id="tc-1",
    )
    hitl_mock.assert_not_called()
    msg_content = str(cmd.update["messages"][0].content)
    assert "delivery_config.methods" in msg_content

    # 2. User preferences route validator refuses non-empty route on task with delivery_config.methods (HTTP 409)
    get_auto_mock = create_autospec(auto_db.get_automation, return_value=task_with_delivery)
    monkeypatch.setattr(auto_db, "get_automation", get_auto_mock)

    with pytest.raises(HTTPException) as pref_exc:
        await model_prefs_mod._validate_report_automation_route_entries(
            "user-alice",
            {f"automation:{auto_id}": ["feishu:user:ou_alice"]},
            {},
            via_rest=True,
            user_confirmed=False,
        )
    assert pref_exc.value.status_code == 409
    assert "delivery_config.methods" in str(pref_exc.value.detail)

    # 3. apply_dalton_notify_preference_patch enforces conflict inside the locked transaction too
    class _LockedCur:
        async def __aenter__(self) -> "_LockedCur":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            _ = params
            self._sql = sql

        async def fetchall(self) -> list[dict[str, Any]]:
            if "FROM automations" in getattr(self, "_sql", ""):
                return [{"automation_id": auto_id, "delivery_config": {"methods": ["feishu"]}}]
            return [{"platform": "feishu", "platform_user_id": "ou_alice"}]

    class _LockedTx:
        async def __aenter__(self) -> "_LockedTx":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

    class _LockedConn:
        def transaction(self) -> _LockedTx:
            return _LockedTx()

        def cursor(self, **kwargs: Any) -> _LockedCur:
            _ = kwargs
            return _LockedCur()

    @asynccontextmanager
    async def _fake_pool_conn(conn: Any = None):
        _ = conn
        yield _LockedConn()

    stored_prefs = {
        "model_preference": {
            "dalton_notify_targets": ["feishu:user:ou_alice"],
            "dalton_notify_routes": {},
        },
        "other_preference": {},
    }
    monkeypatch.setattr(pool_mod, "get_db_connection", _fake_pool_conn)
    monkeypatch.setattr(
        model_prefs_mod,
        "get_user_preferences",
        create_autospec(model_prefs_mod.get_user_preferences, return_value=stored_prefs),
    )
    monkeypatch.setattr(
        user_db,
        "lock_user_preferences",
        create_autospec(user_db.lock_user_preferences, return_value=stored_prefs),
    )
    # Simulate pre-lock read seeing an empty delivery_config while the locked tx sees delivery_config.methods
    get_auto_mock.return_value = {**task_with_delivery, "delivery_config": {}}
    upsert_mock = create_autospec(user_db.upsert_user_preferences, return_value=stored_prefs)
    monkeypatch.setattr(user_db, "upsert_user_preferences", upsert_mock)

    with pytest.raises(HTTPException) as locked_patch_exc:
        await model_prefs_mod.apply_dalton_notify_preference_patch(
            "user-alice",
            {model_prefs_mod.DALTON_NOTIFY_ROUTES_KEY: {f"automation:{auto_id}": ["feishu:user:ou_alice"]}},
        )
    assert locked_patch_exc.value.status_code == 409
    assert "delivery_config.methods" in str(locked_patch_exc.value.detail)
    upsert_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_dalton_report_delivery_set_refuses_when_web_channels_configured_including_in_tx_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dalton_report_delivery(action='set') rejects web delivery_config both on precheck and inside the FOR UPDATE tx."""
    import json

    from src.dalton.database import company_automations as ca_db
    from src.dalton.notify.delivery_mutex import REPORT_PROFILE_DELIVERY_CONFLICT_MSG
    from src.dalton.services import report_delivery as rd_svc
    from src.dalton.tools import report_delivery_tools as rd_tools

    auto_id = "88888888-8888-4888-8888-888888888888"
    owner_uid = "user-alice"
    base_auto: dict[str, Any] = {
        "automation_id": auto_id,
        "user_id": owner_uid,
        "workspace_id": "ws-1",
        "name": "Market Digest AM",
        "instruction": "Run market-digest-am report",
        "trigger_type": "cron",
        "status": "active",
        "config_version": 1,
        "active_version": 1,
        "delivery_config": {"methods": ["feishu"]},
        "metadata": {},
        "audit_history": [],
    }

    monkeypatch.setattr(
        rd_svc,
        "get_user_tier",
        create_autospec(rd_svc.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        rd_svc,
        "list_caller_dm_identities",
        create_autospec(
            rd_svc.list_caller_dm_identities,
            return_value=[
                {
                    "platform": "feishu",
                    "platform_user_id": "ou_alice",
                    "target": "feishu:user:ou_alice",
                    "supports_report_cards": True,
                    "supports_text_targets": True,
                },
                {
                    "platform": "discord",
                    "platform_user_id": "424242424242424242",
                    "target": "discord:user:424242424242424242",
                    "supports_report_cards": False,
                    "supports_failure_targets": True,
                },
            ],
        ),
    )
    load_auto_mock = create_autospec(
        rd_svc._load_automation_for_actor,
        return_value=dict(base_auto),
    )
    monkeypatch.setattr(rd_svc, "_load_automation_for_actor", load_auto_mock)

    # 1. Precheck rejects immediately with the exact save-entrypoint conflict message
    with pytest.raises(rd_svc.ReportDeliveryError) as pre_exc:
        await rd_svc.precheck_report_delivery_set(
            caller_user_id=owner_uid,
            automation_id=auto_id,
            profile="market-digest-am",
            report_targets=["feishu:user:ou_alice"],
            failure_targets=["discord:user:424242424242424242"],
        )
    assert pre_exc.value.code == "conflict"
    assert pre_exc.value.message == REPORT_PROFILE_DELIVERY_CONFLICT_MSG

    # 2. Tool entrypoint surfaces the same readable refusal
    monkeypatch.setattr(
        rd_tools,
        "load_report_delivery_hitl_snapshot",
        create_autospec(rd_tools.load_report_delivery_hitl_snapshot, return_value=None),
    )
    tools = rd_tools.create_report_delivery_tools()
    tool_fn = tools[0]
    cmd = await tool_fn.ainvoke(
        {
            "name": "dalton_report_delivery",
            "id": "tc-rd-mutex-1",
            "type": "tool_call",
            "args": {
                "action": "set",
                "automation_id": auto_id,
                "profile": "market-digest-am",
                "report_targets": ["feishu:user:ou_alice"],
                "failure_targets": ["discord:user:424242424242424242"],
            },
        },
        config={
            "configurable": {
                "user_id": owner_uid,
                "thread_id": "th-rd-mutex",
            }
        },
    )
    tool_msg = cmd.update["messages"][0]
    payload = json.loads(tool_msg.content)
    assert payload["ok"] is False
    assert payload["error"] == "conflict"
    assert payload["message"] == REPORT_PROFILE_DELIVERY_CONFLICT_MSG

    # 3. TOCTOU race: pre-tx read sees empty delivery_config, while FOR UPDATE locked row has delivery_config.methods
    load_auto_mock.return_value = {**base_auto, "delivery_config": {}}
    executed_sqls: list[str] = []

    class _RaceCur:
        async def __aenter__(self) -> "_RaceCur":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            _ = params
            executed_sqls.append(" ".join(sql.split()))
            self._sql = sql

        async def fetchone(self) -> dict[str, Any] | None:
            if "FROM automations" in getattr(self, "_sql", "") and "FOR UPDATE" in getattr(self, "_sql", ""):
                return {**base_auto, "delivery_config": {"methods": ["feishu"]}}
            return None

        async def fetchall(self) -> list[dict[str, Any]]:
            return []

    class _RaceTx:
        async def __aenter__(self) -> "_RaceTx":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

    class _RaceConn:
        def transaction(self) -> _RaceTx:
            return _RaceTx()

        def cursor(self, **kwargs: Any) -> _RaceCur:
            _ = kwargs
            return _RaceCur()

    @asynccontextmanager
    async def _fake_rd_conn(conn: Any = None):
        _ = conn
        yield _RaceConn()

    monkeypatch.setattr(rd_svc, "_use_conn", _fake_rd_conn)
    monkeypatch.setattr(
        ca_db,
        "lock_and_fetch_user_preferences_for_snapshot",
        create_autospec(
            ca_db.lock_and_fetch_user_preferences_for_snapshot,
            return_value={"model_preference": {}},
        ),
    )
    sync_routes_mock = create_autospec(rd_svc.sync_report_delivery_routes_in_tx, return_value=None)
    monkeypatch.setattr(rd_svc, "sync_report_delivery_routes_in_tx", sync_routes_mock)

    with pytest.raises(rd_svc.ReportDeliveryError) as race_exc:
        await rd_svc.set_report_delivery_config(
            caller_user_id=owner_uid,
            automation_id=auto_id,
            profile="market-digest-am",
            report_targets=["feishu:user:ou_alice"],
            failure_targets=["discord:user:424242424242424242"],
            hitl_confirmed=True,
            expected_config_version=1,
        )
    assert race_exc.value.code == "conflict"
    assert race_exc.value.message == REPORT_PROFILE_DELIVERY_CONFLICT_MSG
    assert any("FOR UPDATE" in s and "FROM automations" in s for s in executed_sqls)
    assert not any(s.startswith("UPDATE automations") for s in executed_sqls)
    sync_routes_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_report_submit_uses_locked_execution_record_on_mid_run_switch_and_allows_next_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mid-run switch from web channels to report_profile blocks current run's report submit (no double send) and allows next run."""
    import json

    from src.dalton.database import automation_reports as reports_db
    from src.dalton.services import report_delivery as rd_svc

    auto_id = "99999999-9999-4999-8999-999999999999"
    ws_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    th_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    exec_1 = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    exec_2 = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    run_1 = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    run_2 = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    owner_uid = "user-alice"

    report_profile_meta: dict[str, Any] = {
        "report_profile": {
            "profile": "market-digest-am",
            "report_targets": ["feishu:user:ou_alice"],
            "failure_targets": ["discord:user:424242424242424242"],
        }
    }
    # Current task row AFTER user cleared web channels and enabled report_profile mid-run
    automation_row: dict[str, Any] = {
        "automation_id": auto_id,
        "user_id": owner_uid,
        "workspace_id": ws_id,
        "name": "Market Digest AM",
        "trigger_type": "cron",
        "delivery_config": {},
        "metadata": report_profile_meta,
    }
    exec_1_held = _held_delivery(exec_1)
    executions_by_id: dict[str, dict[str, Any]] = {
        exec_1: {
            "automation_execution_id": exec_1,
            "automation_id": auto_id,
            "status": "running",
            "conversation_thread_id": th_id,
            "conversation_response_id": run_1,
            "coverage_unit_id": None,
            "coverage_unit_version": None,
            "execution_delivery": automation_delivery.stamp(exec_1_held),
            "execution_delivery_result": None,
        },
        exec_2: {
            "automation_execution_id": exec_2,
            "automation_id": auto_id,
            "status": "running",
            "conversation_thread_id": th_id,
            "conversation_response_id": run_2,
            "coverage_unit_id": None,
            "coverage_unit_version": None,
            "execution_delivery": None,
            "execution_delivery_result": None,
        },
    }
    stored_report_rows: list[dict[str, Any]] = []

    class _ReportFakeCur:
        def __init__(self) -> None:
            self._one: dict[str, Any] | None = None
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self) -> "_ReportFakeCur":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            norm = " ".join(sql.split())
            self._one = None
            self._rows = []
            if norm.startswith("SELECT pg_advisory_xact_lock"):
                return
            if "FROM automation_executions" in norm and "FOR UPDATE" not in norm and "FOR SHARE" not in norm:
                eid = str(params[0])
                rec = executions_by_id.get(eid)
                self._one = {"automation_id": rec["automation_id"]} if rec else None
            elif "FROM automations" in norm and ("FOR UPDATE" in norm or "FOR SHARE" in norm):
                self._one = dict(automation_row)
            elif "FROM automation_executions" in norm and ("FOR UPDATE" in norm or "FOR SHARE" in norm):
                eid = str(params[0])
                rec = executions_by_id.get(eid)
                self._one = dict(rec) if rec else None
            elif "INSERT INTO dalton_notify_deliveries" in norm:
                did, eid, aid, uid, ch, tgt, pjson = params
                stored_report_rows.append(
                    {
                        "delivery_id": str(did),
                        "execution_id": str(eid),
                        "automation_id": str(aid),
                        "user_id": str(uid),
                        "channel": str(ch),
                        "target": str(tgt),
                        "status": "pending",
                        "attempt_count": 0,
                        "error_code": None,
                        "payload_json": json.loads(pjson),
                    }
                )
            elif "FROM dalton_notify_deliveries" in norm and "<> %s" in norm:
                self._one = None
            elif "FROM dalton_notify_deliveries" in norm:
                run_token = str(params[2])
                self._rows = [
                    r
                    for r in stored_report_rows
                    if str((r.get("payload_json") or {}).get("report_run_id") or "") == run_token
                ]

        async def fetchone(self) -> dict[str, Any] | None:
            return self._one

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

    class _ReportFakeConn:
        def cursor(self, **kwargs: Any) -> _ReportFakeCur:
            _ = kwargs
            return _ReportFakeCur()

        @asynccontextmanager
        async def transaction(self):
            yield

    @asynccontextmanager
    async def _fake_reports_conn(conn: Any = None):
        _ = conn
        yield _ReportFakeConn()

    monkeypatch.setattr(reports_db, "get_db_connection", _fake_reports_conn)
    monkeypatch.setattr(
        reports_db,
        "_authorize_all_targets",
        create_autospec(
            reports_db._authorize_all_targets,
            return_value=(["feishu:user:ou_alice"], None),
        ),
    )
    monkeypatch.setattr(
        rd_svc,
        "finalize_missing_report_profile_notice",
        create_autospec(rd_svc.finalize_missing_report_profile_notice, return_value=None),
    )

    sample_card = {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": "Market Digest AM"}},
        "body": {
            "elements": [
                {"tag": "markdown", "content": "S&P 500 closed higher. Source https://example.com/am"}
            ]
        },
    }

    # 1. Execution #1 (started with web delivery stamped, cleared mid-run + report_profile enabled):
    #    submit_automation_report uses the locked execution record and refuses to create a Dalton report batch.
    res_1 = await reports_db.submit_automation_report(
        user_id=owner_uid,
        workspace_id=ws_id,
        thread_id=th_id,
        run_id=run_1,
        automation_id=auto_id,
        execution_id=exec_1,
        report_run_id="market-digest:AM:2026-11-28-r1",
        material="yes",
        cards=[sample_card],
    )
    assert res_1["ok"] is False
    assert res_1["code"] == "held_delivery_configured"
    assert stored_report_rows == []

    # Execution #1 settles: upstream delivers once via its held target; Dalton sends nothing (no double send)
    spies = _wire_real_settlement_send_spies(
        monkeypatch,
        user_routes={f"automation:{auto_id}": ["feishu:user:ou_alice"]},
    )
    read_confirmed_mock = create_autospec(
        reports_db.read_execution_report_confirmed,
        side_effect=lambda eid: any(r["execution_id"] == eid for r in stored_report_rows),
    )
    ensure_alert_mock = create_autospec(
        reports_db.ensure_report_failure_alert,
        return_value={"ok": False, "status": "not_found"},
    )
    sync_terminal_mock = create_autospec(
        notify_dispatch.sync_terminal_undelivered_execution,
        return_value=None,
    )
    monkeypatch.setattr(reports_db, "read_execution_report_confirmed", read_confirmed_mock)
    monkeypatch.setattr(reports_db, "ensure_report_failure_alert", ensure_alert_mock)
    monkeypatch.setattr(notify_dispatch, "sync_terminal_undelivered_execution", sync_terminal_mock)

    await settlement_mod._after_settling(
        settlement_mod.Outcome.COMPLETED,
        automation_row,
        exec_1,
        {
            "settled_from": "running",
            "result_excerpt": "Morning brief ready.",
            "delivery": automation_delivery.stamp(exec_1_held),
        },
        thread_id=th_id,
        workspace_id=ws_id,
        error=None,
        final_text="Morning brief ready.",
        run_id=run_1,
        delivery=exec_1_held,
        finish_left=False,
    )
    assert spies["gateway_request"].await_count == 1
    assert spies["dalton_gateway_send"].await_count == 0
    assert spies["dalton_bridge_send"].await_count == 0
    assert spies["webhook_fire"].await_count == 0

    # 2. Execution #2 (next run after clearing web channels and enabling report_profile):
    #    no upstream delivery is stamped on exec_2, so report submission succeeds normally.
    res_2 = await reports_db.submit_automation_report(
        user_id=owner_uid,
        workspace_id=ws_id,
        thread_id=th_id,
        run_id=run_2,
        automation_id=auto_id,
        execution_id=exec_2,
        report_run_id="market-digest:AM:2026-11-29-r2",
        material="yes",
        cards=[sample_card],
    )
    assert res_2["ok"] is True
    assert res_2["created"] is True
    assert len(stored_report_rows) == 1
    assert stored_report_rows[0]["execution_id"] == exec_2
    assert stored_report_rows[0]["target"] == "feishu:user:ou_alice"

    # Execution #2 settles: upstream sends nothing, Dalton report path confirms exec_2
    await settlement_mod._after_settling(
        settlement_mod.Outcome.COMPLETED,
        automation_row,
        exec_2,
        {
            "settled_from": "running",
            "result_excerpt": "Next morning brief ready.",
            "delivery": None,
        },
        thread_id=th_id,
        workspace_id=ws_id,
        error=None,
        final_text="Next morning brief ready.",
        run_id=run_2,
        delivery=None,
        finish_left=False,
    )
    # Upstream gateway_request count remains 1 (not called again for exec_2),
    # and exec_2's saved report batch is confirmed on the Dalton report path.
    assert spies["gateway_request"].await_count == 1
    assert any(call.args == (exec_2,) for call in read_confirmed_mock.await_args_list)
