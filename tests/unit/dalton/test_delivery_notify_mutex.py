"""Unit tests for mutual exclusion between upstream delivery_config.methods and Dalton notify."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import create_autospec

from fastapi import HTTPException
import pytest

import src.dalton.database.automation_recycle as recycle_db
import src.dalton.llm.model_prefs as model_prefs_mod
import src.dalton.notify.dispatch as notify_dispatch
import src.dalton.tools.notify_tools as notify_tools_mod
from src.server.database import automation as auto_db
from src.server.models.automation import AutomationCreate, DeliveryConfig
import src.server.services.automation_delivery as automation_delivery
import src.server.services.automation_settlement as settlement_mod
import src.server.services.automations.lifecycle as lifecycle_mod


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


@pytest.mark.asyncio
async def test_settlement_builtin_dalton_task_with_delivery_config_delivers_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Built-in dalton-* task with web delivery_config.methods delivers once (upstream only, Dalton skips)."""
    execution_id = "exec-builtin-1"
    automation: dict[str, Any] = {
        "automation_id": "11111111-1111-4111-8111-111111111111",
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

    soft_deleted_mock = create_autospec(
        recycle_db.automation_is_soft_deleted,
        return_value=False,
    )
    finish_here_mock = create_autospec(settlement_mod._finish_here, return_value=None)
    post_notify_mock = create_autospec(
        notify_dispatch.post_bridge_notify,
        return_value={"ok": True},
    )
    claim_target_mock = create_autospec(
        notify_dispatch.claim_target_delivery,
        return_value=True,
    )
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
        return_value={},
    )
    resolve_routes_mock = create_autospec(
        notify_dispatch.resolve_dalton_notify_routes,
        return_value={},
    )

    monkeypatch.setattr(recycle_db, "automation_is_soft_deleted", soft_deleted_mock)
    monkeypatch.setattr(settlement_mod, "_finish_here", finish_here_mock)
    monkeypatch.setattr(notify_dispatch, "post_bridge_notify", post_notify_mock)
    monkeypatch.setattr(notify_dispatch, "claim_target_delivery", claim_target_mock)
    monkeypatch.setattr(notify_dispatch, "resolve_dalton_notify_enabled", resolve_enabled_mock)
    monkeypatch.setattr(notify_dispatch, "resolve_dalton_notify_targets", resolve_targets_mock)
    monkeypatch.setattr(notify_dispatch, "resolve_dalton_notify_overrides", resolve_overrides_mock)
    monkeypatch.setattr(notify_dispatch, "resolve_dalton_notify_routes", resolve_routes_mock)

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

    assert finish_here_mock.await_count == 1
    assert post_notify_mock.await_count == 0
    assert claim_target_mock.await_count == 0
    assert finish_here_mock.await_count + post_notify_mock.await_count == 1
    assert any(
        "delivery_config.methods" in rec.getMessage() and execution_id in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_settlement_custom_task_with_both_delivery_and_dalton_route_delivers_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Custom task configured with both delivery_config.methods and dalton_notify_routes delivers once."""
    auto_id = "22222222-2222-4222-8222-222222222222"
    execution_id = "exec-custom-1"
    automation: dict[str, Any] = {
        "automation_id": auto_id,
        "user_id": "user-alice",
        "workspace_id": "ws-custom",
        "name": "Morning Semiconductor Brief",
        "trigger_type": "cron",
        "delivery_config": {"methods": ["feishu"]},
        "metadata": {},
    }
    row: dict[str, Any] = {
        "settled_from": "running",
        "result_excerpt": "Morning brief ready.",
    }

    soft_deleted_mock = create_autospec(
        recycle_db.automation_is_soft_deleted,
        return_value=False,
    )
    finish_here_mock = create_autospec(settlement_mod._finish_here, return_value=None)
    post_notify_mock = create_autospec(
        notify_dispatch.post_bridge_notify,
        return_value={"ok": True},
    )
    claim_target_mock = create_autospec(
        notify_dispatch.claim_target_delivery,
        return_value=True,
    )
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

    monkeypatch.setattr(recycle_db, "automation_is_soft_deleted", soft_deleted_mock)
    monkeypatch.setattr(settlement_mod, "_finish_here", finish_here_mock)
    monkeypatch.setattr(notify_dispatch, "post_bridge_notify", post_notify_mock)
    monkeypatch.setattr(notify_dispatch, "claim_target_delivery", claim_target_mock)
    monkeypatch.setattr(notify_dispatch, "resolve_dalton_notify_enabled", resolve_enabled_mock)
    monkeypatch.setattr(notify_dispatch, "resolve_dalton_notify_targets", resolve_targets_mock)
    monkeypatch.setattr(notify_dispatch, "resolve_dalton_notify_overrides", resolve_overrides_mock)
    monkeypatch.setattr(notify_dispatch, "resolve_dalton_notify_routes", resolve_routes_mock)

    with caplog.at_level(logging.INFO, logger=notify_dispatch.__name__):
        await settlement_mod._after_settling(
            settlement_mod.Outcome.COMPLETED,
            automation,
            execution_id,
            row,
            thread_id="thread-2",
            run_id="run-2",
            workspace_id="ws-custom",
            error=None,
            delivery=_held_delivery(execution_id),
            final_text="Morning brief ready.",
            finish_left=False,
        )

    assert finish_here_mock.await_count == 1
    assert post_notify_mock.await_count == 0
    assert claim_target_mock.await_count == 0
    assert finish_here_mock.await_count + post_notify_mock.await_count == 1
    assert any(
        "delivery_config.methods" in rec.getMessage() and execution_id in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_evaluate_user_notify_eligibility_skips_when_delivery_config_methods_set(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """evaluate_user_notify_eligibility returns held_delivery_configured and logs when delivery_config.methods is set."""
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
        return_value={},
    )
    resolve_routes_mock = create_autospec(
        notify_dispatch.resolve_dalton_notify_routes,
        return_value={f"automation:{auto_id}": ["feishu:user:ou_alice"]},
    )

    with caplog.at_level(logging.INFO, logger=notify_dispatch.__name__):
        allowed, reason, targets = await notify_dispatch.evaluate_user_notify_eligibility(
            "user-alice",
            {
                "automation_id": auto_id,
                "name": "Custom Task",
                "delivery_config": {"methods": ["feishu"]},
            },
            resolve_enabled_fn=resolve_enabled_mock,
            resolve_overrides_fn=resolve_overrides_mock,
            resolve_targets_fn=resolve_targets_mock,
            resolve_routes_fn=resolve_routes_mock,
        )

    assert (allowed, reason, targets) == (False, "held_delivery_configured", [])
    resolve_enabled_mock.assert_not_awaited()
    assert any("delivery_config.methods" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_lifecycle_save_validation_rejects_conflicting_delivery_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create_automation and prepare_automation_update reject delivery_config.methods on built-in or routed tasks."""
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
