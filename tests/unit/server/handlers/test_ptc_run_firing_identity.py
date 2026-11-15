"""Tests for propagating carried firing identity into resumed turn graph config."""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest

from ptc_agent.core.session import Session
from src.server.handlers.chat import ptc_run, request_prep
from src.server.models.chat import ChatRequest
from src.server.services import automation_delivery, automation_settlement, user_skills
from src.server.services.egress.direct_tools import order_ledger_for_turn
from src.server.services.report_back.handoff import carry
from src.server.services.thread_agent import AgentInputs
from src.server.services.turn_runtime import TurnRoute
from src.server.services.wakeups import cancel_notice
from src.server.services.workspace_manager import WorkspaceManager

PTC = "src.server.handlers.chat.ptc_run"


class _CapturedConfig(Exception):
    """Stop workflow right once build_graph_config has produced graph_config."""


async def _capture_graph_config(
    *,
    carried_firing_value: dict[str, str],
    extra_configurable: dict[str, object] | None = None,
) -> dict[str, object]:
    captured: list[dict[str, object]] = []
    real_build_graph_config = ptc_run.build_graph_config

    def _capture_build_graph_config(*args, **kwargs):
        cfg = real_build_graph_config(*args, **kwargs)
        captured.append(cfg)
        return cfg

    request = ChatRequest(
        workspace_id="ws-1",
        messages=[],
        query_type="system",
        timezone="UTC",
        locale="en-US",
    )
    config = SimpleNamespace(
        llm=SimpleNamespace(name="claude-test"),
        subagents=SimpleNamespace(enabled=False),
        claude_code_admitted=None,
        claude_code=False,
        user_skill_dir=None,
        disabled_skills=None,
        filesystem=SimpleNamespace(enabled_tools=None),
    )

    session_mock = create_autospec(Session, instance=True)
    session_mock.sandbox = None
    ws_manager_instance = create_autospec(WorkspaceManager, instance=True)
    ws_manager_instance.has_ready_session.return_value = True
    ws_manager_instance.get_session_for_workspace.return_value = session_mock
    ws_manager_instance.restore_sibling_folders.return_value = None

    ws_manager_cls = create_autospec(WorkspaceManager)
    ws_manager_cls.get_instance.return_value = ws_manager_instance

    registry_store_instance = create_autospec(
        ptc_run.BackgroundRegistryStore,
        instance=True,
    )
    registry_store_instance.get_or_create_registry.return_value = SimpleNamespace()
    registry_store_cls = create_autospec(ptc_run.BackgroundRegistryStore)
    registry_store_cls.get_instance.return_value = registry_store_instance

    setup_mock = SimpleNamespace(
        agent_config=SimpleNamespace(
            tool_exposure_mode="ptc",
            enabled_tools=None,
            subagents=SimpleNamespace(enabled=False),
        )
    )
    run_handle = SimpleNamespace(
        turn_index=2,
        finalized=False,
        tail_turn_no_new_work=False,
        checkpointer=None,
        guard=None,
    )
    inputs_mock = create_autospec(AgentInputs, instance=True)
    inputs_mock.project = SimpleNamespace(workspace_root="/tmp/ws")
    inputs_mock.direct_mcp = None
    inputs_mock.build_kwargs.return_value = {}

    with ExitStack() as stack:
        for attr, fake in [
            ("setup", setup_mock),
            ("WorkspaceManager", ws_manager_cls),
            ("BackgroundRegistryStore", registry_store_cls),
            ("ensure_thread", create_autospec(ptc_run.ensure_thread, return_value=request_prep.PriorThread())),
            ("wait_or_steer", create_autospec(ptc_run.wait_or_steer, return_value=(True, None))),
            ("read_disk_notice", create_autospec(ptc_run.read_disk_notice, return_value=(None, False))),
            ("get_user_profile_for_prompt", create_autospec(ptc_run.get_user_profile_for_prompt, return_value=None)),
            ("_resolve_fork", create_autospec(ptc_run._resolve_fork, return_value=("system", None))),
            ("_resolve_origin_meta", create_autospec(ptc_run._resolve_origin_meta, return_value={})),
            ("begin_run", create_autospec(ptc_run.begin_run, return_value=run_handle)),
            ("keep_named_model", create_autospec(ptc_run.keep_named_model, return_value=None)),
            ("init_tracking", create_autospec(ptc_run.init_tracking, return_value=(object(), object()))),
            ("build_run_credit_gate", create_autospec(ptc_run.build_run_credit_gate, return_value=None)),
            ("apply_fetch_override", create_autospec(ptc_run.apply_fetch_override, return_value=None)),
            ("update_workspace_activity", create_autospec(ptc_run.update_workspace_activity, return_value=None)),
            ("build_ptc_graph_with_session", create_autospec(ptc_run.build_ptc_graph_with_session, return_value=object())),
            ("turn_start_checkpoint_id", create_autospec(ptc_run.turn_start_checkpoint_id, return_value=None)),
            (
                "_fire_and_forget",
                create_autospec(
                    ptc_run._fire_and_forget,
                    side_effect=lambda coro, **_kw: coro.close() if hasattr(coro, "close") else None,
                ),
            ),
            (
                "build_graph_config",
                create_autospec(ptc_run.build_graph_config, side_effect=_capture_build_graph_config),
            ),
            (
                "open_with_steers_ahead",
                create_autospec(ptc_run.open_with_steers_ahead, side_effect=_CapturedConfig()),
            ),
        ]:
            stack.enter_context(patch(f"{PTC}.{attr}", fake))

        stack.enter_context(
            patch.object(
                carry,
                "carried_pair",
                create_autospec(carry.carried_pair, return_value={}),
            )
        )
        stack.enter_context(
            patch.object(
                carry,
                "carried_firing",
                create_autospec(carry.carried_firing, return_value=carried_firing_value),
            )
        )
        stack.enter_context(
            patch.object(
                carry,
                "carried_delivery",
                create_autospec(carry.carried_delivery, return_value=None),
            )
        )
        stack.enter_context(
            patch.object(
                automation_settlement,
                "take_over_firing",
                create_autospec(automation_settlement.take_over_firing, return_value=None),
            )
        )
        stack.enter_context(
            patch.object(
                automation_delivery,
                "turn_metadata",
                create_autospec(automation_delivery.turn_metadata, return_value={}),
            )
        )
        stack.enter_context(
            patch.object(
                user_skills,
                "skills_delivery_signature",
                create_autospec(user_skills.skills_delivery_signature, return_value="sig"),
            )
        )
        stack.enter_context(
            patch(
                "src.server.services.thread_agent.AgentInputs.read",
                create_autospec(
                    AgentInputs.read,
                    return_value=inputs_mock,
                ),
            )
        )
        stack.enter_context(
            patch(
                "src.server.services.egress.direct_tools.order_ledger_for_turn",
                create_autospec(
                    order_ledger_for_turn,
                    return_value=None,
                ),
            )
        )
        stack.enter_context(
            patch.object(
                cancel_notice,
                "ThreadCancelNotices",
                create_autospec(cancel_notice.ThreadCancelNotices),
            )
        )

        gen = ptc_run.astream_ptc_workflow(
            request=request,
            thread_id="t-1",
            run_id="r-2",
            user_input="",
            user_id="u-1",
            route=TurnRoute("ws-1", "analyst"),
            is_byok=False,
            config=config,
            extra_configurable=extra_configurable,
        )
        try:
            async for _ in gen:
                pass
        except _CapturedConfig:
            pass
        finally:
            await gen.aclose()

    assert len(captured) == 1
    return captured[0]["configurable"]


@pytest.mark.asyncio
async def test_resumed_firing_populates_automation_identity_in_configurable() -> None:
    """续跑回合 firing 非空时，configurable 写入两项自动化工具身份。"""
    configurable = await _capture_graph_config(
        carried_firing_value={
            "automation_execution_id": "exec-carried-1",
            "automation_id": "auto-carried-1",
        },
        extra_configurable=None,
    )
    assert configurable["automation_execution_id"] == "exec-carried-1"
    assert configurable["turn_origin_automation_id"] == "auto-carried-1"


@pytest.mark.asyncio
async def test_resumed_firing_does_not_overwrite_existing_identity_in_configurable() -> None:
    """extra_configurable 已有身份字段时，firing 不覆盖已有值。"""
    configurable = await _capture_graph_config(
        carried_firing_value={
            "automation_execution_id": "exec-carried-1",
            "automation_id": "auto-carried-1",
        },
        extra_configurable={
            "automation_execution_id": "exec-existing",
            "turn_origin_automation_id": "auto-existing",
        },
    )
    assert configurable["automation_execution_id"] == "exec-existing"
    assert configurable["turn_origin_automation_id"] == "auto-existing"
