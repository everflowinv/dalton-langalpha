"""Tests for automation executor first-turn effort recording & continuation/wakeup/retry/fallback inheritance."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import HTTPException
from psycopg import AsyncConnection, AsyncCursor

from src.dalton.services import automation_subscription_fallback as fb_mod
from src.dalton.services import automation_thread_model as atm_mod
from src.llms.model_spec import clamp_reasoning_effort
from src.server.handlers import chat as chat_handler_mod
from src.server.models.chat import ChatMessage, ChatRequest
from src.server.services import automation_executor as exec_mod
from src.server.services import thread_agent
from src.server.services.llm import config as llm_config_mod
from src.server.services.llm import user_models
from src.server.services.shared_workspaces.turn import TurnIdentity
from src.server.services.turn_runtime import TurnRoute

USER = "user-exec-1"
WS = "11111111-1111-4111-8111-111111111111"
THREAD = "22222222-2222-4222-8222-222222222222"
EXEC_A = "33333333-3333-4333-8333-333333333333"
EXEC_B = "44444444-4444-4444-8444-444444444444"
AUTO_A = "55555555-5555-4555-8555-555555555555"
AUTO_B = "66666666-6666-4666-8666-666666666666"
RUN_A = "77777777-7777-4777-8777-777777777777"
RUN_USER = "88888888-8888-4888-8888-888888888888"
WAKEUP_A = "99999999-9999-4999-8999-999999999999"
WAKEUP_USER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _make_cfg(model_name: str, effort: str | None) -> Any:
    cfg = SimpleNamespace(
        llm=SimpleNamespace(name=model_name),
        llm_client=object(),
        resolved_reasoning_effort=effort,
    )
    return cfg


@pytest.mark.asyncio
async def test_executor_first_turn_passes_effort_and_stamps_two_state_metadata(monkeypatch) -> None:
    captured_calls: list[dict[str, Any]] = []

    async def _fake_astream(**kwargs: Any):
        captured_calls.append(kwargs)
        if False:
            yield None

    monkeypatch.setattr(
        chat_handler_mod,
        "astream_ptc_workflow",
        create_autospec(chat_handler_mod.astream_ptc_workflow, side_effect=_fake_astream),
    )
    monkeypatch.setattr(
        exec_mod.TurnIdentity,
        "of",
        create_autospec(TurnIdentity.of, return_value=SimpleNamespace(recorded_as=USER)),
    )

    executor = exec_mod.AutomationExecutor.__new__(exec_mod.AutomationExecutor)
    executor._note_admission = create_autospec(executor._note_admission, return_value=True)

    for expected_effort in ("high", None):
        firing = SimpleNamespace(
            automation={
                "automation_id": AUTO_A,
                "user_id": USER,
                "name": "Morning Task",
                "llm_model": "claude-opus-5-5-oauth",
                "reasoning_effort": expected_effort,
                "instruction": "Do research.",
                "additional_context": None,
                "thread_strategy": "new",
            },
            workspace_id=WS,
            thread_id=THREAD,
            route=TurnRoute(workspace_id=WS, owner_id=USER, role="analyst"),
            late=None,
            held=False,
            delivery=None,
            wake_context=None,
            workspace_owner_id=USER,
            checked_computer_id=None,
            run_id=None,
            admitted=False,
        )
        ok = await executor._run_turn(
            EXEC_A,
            firing,
            has_byok=False,
            deadline=9999999999.0,
        )
        assert ok is True
        call_kw = captured_calls[-1]
        assert call_kw["request"].llm_model == "claude-opus-5-5-oauth"
        assert call_kw["request"].reasoning_effort == expected_effort
        assert call_kw["run_metadata"]["automation_reasoning_effort_configured"] is True
        assert call_kw["run_metadata"]["automation_reasoning_effort"] == expected_effort


@pytest.mark.asyncio
async def test_wakeup_inherits_creator_automation_effort_not_other_running_task_or_user_wakeup(
    monkeypatch,
) -> None:
    exec_a = {
        "automation_execution_id": EXEC_A,
        "automation_id": AUTO_A,
        "status": "completed",
        "conversation_thread_id": THREAD,
        "user_id": USER,
    }
    exec_b = {
        "automation_execution_id": EXEC_B,
        "automation_id": AUTO_B,
        "status": "running",
        "conversation_thread_id": THREAD,
        "user_id": USER,
    }

    async def _wakeup_creators(tid: str, wids: Any) -> list[str]:
        if WAKEUP_A in wids:
            return [RUN_A]
        if WAKEUP_USER in wids:
            return [RUN_USER]
        return []

    async def _exec_for_run(run_id: str) -> dict[str, Any] | None:
        return exec_a if run_id == RUN_A else None

    async def _first_turn_settings(execution: Any, *, fallback_thread_id: str):
        if execution["automation_execution_id"] == EXEC_A:
            return "claude-opus-5-5-oauth", True, "high"
        return "gpt-6.2-sol-oauth", True, "low"

    monkeypatch.setattr(
        atm_mod,
        "_get_wakeup_creator_run_ids",
        create_autospec(atm_mod._get_wakeup_creator_run_ids, side_effect=_wakeup_creators),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(atm_mod._get_execution_for_run_id, side_effect=_exec_for_run),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_first_turn_settings",
        create_autospec(atm_mod._get_execution_first_turn_settings, side_effect=_first_turn_settings),
    )
    monkeypatch.setattr(
        atm_mod.exec_db,
        "get_running_executions_for_thread",
        create_autospec(atm_mod.exec_db.get_running_executions_for_thread, return_value=[exec_b]),
    )
    monkeypatch.setattr(
        atm_mod.user_models,
        "get_model_preference",
        create_autospec(user_models.get_model_preference, return_value={}),
    )
    monkeypatch.setattr(
        atm_mod,
        "model_resolves",
        create_autospec(atm_mod.model_resolves, return_value=True),
    )

    resolved_calls: list[tuple[str | None, str | None]] = []

    async def _fake_resolve_llm_config(_base, _uid, model, _byok, *, reasoning_effort=None, **_kw):
        resolved_calls.append((model, reasoning_effort))
        return _make_cfg(str(model), reasoning_effort)

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, side_effect=_fake_resolve_llm_config),
    )
    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())

    resolver = thread_agent.thread_resolver(
        USER,
        THREAD,
        TurnRoute(workspace_id=WS, owner_id=USER, role="analyst"),
        identity=TurnIdentity(sender=USER, owner=USER, recorded_as=USER, org_id=None),
        thread={"reasoning_effort": "low"},
        is_byok=False,
    )

    # 1. Wakeup created by Automation A inherits Automation A's model + "high" effort (NOT running Automation B's "low")
    req_a = ChatRequest(
        workspace_id=WS,
        messages=[ChatMessage(role="user", content="wakeup A")],
        query_type="wakeup",
        wakeup={"notices": [{"wakeup_id": WAKEUP_A}]},
    )
    ctx_a: dict[str, Any] = {}
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=USER,
        thread_id=THREAD,
        request=req_a,
        requested_model=None,
        held_model="gpt-6.2-sol-oauth",
        from_service=True,
        resolve=resolver,
        on_automation_context=ctx_a.update,
    )
    assert resolved_calls[-1] == ("claude-opus-5-5-oauth", "high")
    assert ctx_a == {
        "automation_execution_id": EXEC_A,
        "automation_reasoning_effort_configured": True,
        "automation_reasoning_effort": "high",
    }

    # 2. Wakeup created by a normal user turn MUST NOT inherit running Automation B's model or effort
    req_user = ChatRequest(
        workspace_id=WS,
        messages=[ChatMessage(role="user", content="user wakeup")],
        query_type="wakeup",
        wakeup={"notices": [{"wakeup_id": WAKEUP_USER}]},
    )
    ctx_user: dict[str, Any] = {}
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=USER,
        thread_id=THREAD,
        request=req_user,
        requested_model=None,
        held_model="claude-sonnet-5-5",
        from_service=True,
        resolve=resolver,
        on_automation_context=ctx_user.update,
    )
    assert resolved_calls[-1] == ("claude-sonnet-5-5", "low")
    assert ctx_user == {}


@pytest.mark.asyncio
async def test_first_turn_configured_null_not_overridden_by_later_db_edit_while_pre_d042_falls_back(
    monkeypatch,
) -> None:
    exec_row = {
        "automation_execution_id": EXEC_A,
        "automation_id": AUTO_A,
        "status": "running",
        "conversation_thread_id": THREAD,
        "user_id": USER,
    }
    req = ChatRequest(
        workspace_id=WS,
        messages=[ChatMessage(role="user", content="done")],
        query_type="system",
        automation_delivery={"id": EXEC_A, "targets": []},
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_by_id",
        create_autospec(atm_mod._get_execution_by_id, return_value=exec_row),
    )
    monkeypatch.setattr(
        atm_mod.user_models,
        "get_model_preference",
        create_autospec(user_models.get_model_preference, return_value={}),
    )
    monkeypatch.setattr(
        atm_mod,
        "model_resolves",
        create_autospec(atm_mod.model_resolves, return_value=True),
    )
    auto_get_mock = create_autospec(
        atm_mod.auto_db.get_automation_including_deleted,
        return_value={"llm_model": "claude-opus-5-5-oauth", "reasoning_effort": "max"},
    )
    monkeypatch.setattr(atm_mod.auto_db, "get_automation_including_deleted", auto_get_mock)

    orig_first_turn_settings = atm_mod._get_execution_first_turn_settings

    # Case 1: first turn explicitly recorded effort_configured=True with effort=None
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_first_turn_settings",
        create_autospec(
            orig_first_turn_settings,
            return_value=("claude-opus-5-5-oauth", True, None),
        ),
    )
    _, model, _, _, configured, effort = await atm_mod._resolve_automation_notification_context(
        user_id=USER, thread_id=THREAD, request=req, from_service=True
    )
    assert model == "claude-opus-5-5-oauth"
    assert configured is True
    assert effort is None
    auto_get_mock.assert_not_awaited()

    # Case 2: pre-d042 in-flight execution (effort_configured=False) falls back to automations table
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_first_turn_settings",
        create_autospec(
            orig_first_turn_settings,
            return_value=("claude-opus-5-5-oauth", False, None),
        ),
    )
    _, model2, _, _, configured2, effort2 = await atm_mod._resolve_automation_notification_context(
        user_id=USER, thread_id=THREAD, request=req, from_service=True
    )
    assert model2 == "claude-opus-5-5-oauth"
    assert configured2 is True
    assert effort2 == "max"
    auto_get_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_model_retry_still_inherits_first_turn_effort_and_propagates_run_meta(
    monkeypatch,
) -> None:
    exec_row = {
        "automation_execution_id": EXEC_A,
        "automation_id": AUTO_A,
        "status": "running",
        "conversation_thread_id": THREAD,
        "user_id": USER,
    }
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(atm_mod._get_execution_for_run_id, return_value=exec_row),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_first_turn_settings",
        create_autospec(
            atm_mod._get_execution_first_turn_settings,
            return_value=("claude-opus-5-5-oauth", True, "high"),
        ),
    )

    resolved: list[tuple[str | None, str | None]] = []

    async def _fake_resolve_llm_config(_base, _uid, model, _byok, *, reasoning_effort=None, **_kw):
        resolved.append((model, reasoning_effort))
        return _make_cfg(str(model), reasoning_effort)

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, side_effect=_fake_resolve_llm_config),
    )
    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())

    resolver = thread_agent.thread_resolver(
        USER,
        THREAD,
        TurnRoute(workspace_id=WS, owner_id=USER, role="analyst"),
        identity=TurnIdentity(sender=USER, owner=USER, recorded_as=USER, org_id=None),
        thread={"reasoning_effort": "low"},
        is_byok=False,
    )

    retry_req = ChatRequest(
        workspace_id=WS,
        messages=[],
        llm_model="gpt-6.2-sol-oauth",
        reasoning_effort=None,
    )
    ctx: dict[str, Any] = {}
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=USER,
        thread_id=THREAD,
        request=retry_req,
        requested_model="gpt-6.2-sol-oauth",
        held_model=None,
        from_service=True,
        is_server_retry=True,
        retry_of_run_id=RUN_A,
        resolve=resolver,
        on_automation_context=ctx.update,
    )
    # Explicit retry model is respected while effort is still inherited from first turn
    assert resolved == [("gpt-6.2-sol-oauth", "high")]
    assert ctx == {
        "automation_execution_id": EXEC_A,
        "automation_reasoning_effort_configured": True,
        "automation_reasoning_effort": "high",
    }


@pytest.mark.asyncio
async def test_subscription_fallback_inherits_effort_for_multi_tier_and_clamps_antigravity(
    monkeypatch,
) -> None:
    exec_row = {
        "automation_execution_id": EXEC_A,
        "automation_id": AUTO_A,
        "status": "running",
        "conversation_thread_id": THREAD,
        "user_id": USER,
    }
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_by_id",
        create_autospec(atm_mod._get_execution_by_id, return_value=exec_row),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_first_turn_settings",
        create_autospec(
            atm_mod._get_execution_first_turn_settings,
            return_value=("claude-opus-5-5-oauth", True, "low"),
        ),
    )
    monkeypatch.setattr(
        atm_mod,
        "model_resolves",
        create_autospec(atm_mod.model_resolves, return_value=True),
    )
    monkeypatch.setattr(
        fb_mod,
        "_subscription_provider_for_model",
        create_autospec(
            fb_mod._subscription_provider_for_model,
            side_effect=lambda _u, m, _p: "claude-oauth" if "claude" in m else "codex-oauth",
        ),
    )

    orig_get_model_pref = user_models.get_model_preference
    orig_resolve_llm_config = llm_config_mod.resolve_llm_config

    for fallback_model, ladder, expected_clamped in (
        ("gpt-6.2-sol-oauth", ("low", "medium", "high"), "low"),
        ("gemini-4-neon-high-oauth", ("high",), "high"),
    ):
        monkeypatch.setattr(
            atm_mod.user_models,
            "get_model_preference",
            create_autospec(
                orig_get_model_pref,
                return_value={"fallback_models": [fallback_model]},
            ),
        )
        attempts: list[tuple[str | None, str | None]] = []

        async def _fake_resolve_llm_config(
            _base, _uid, model, _byok, *, reasoning_effort=None, **_kw
        ):
            attempts.append((model, reasoning_effort))
            if model == "claude-opus-5-5-oauth":
                raise HTTPException(
                    status_code=400,
                    detail={
                        "type": "oauth_reauth_required",
                        "provider": "claude-oauth",
                        "message": "Reconnect it to use 'claude-opus-5-5-oauth'.",
                    },
                )
            clamped = clamp_reasoning_effort(ladder, ladder[-1], reasoning_effort or "")
            return _make_cfg(str(model), clamped)

        monkeypatch.setattr(
            llm_config_mod,
            "resolve_llm_config",
            create_autospec(orig_resolve_llm_config, side_effect=_fake_resolve_llm_config),
        )
        monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())

        resolver = thread_agent.thread_resolver(
            USER,
            THREAD,
            TurnRoute(workspace_id=WS, owner_id=USER, role="analyst"),
            identity=TurnIdentity(sender=USER, owner=USER, recorded_as=USER, org_id=None),
            thread={"reasoning_effort": "max"},
            is_byok=False,
        )
        req = ChatRequest(
            workspace_id=WS,
            messages=[ChatMessage(role="user", content="done")],
            query_type="system",
            automation_delivery={"id": EXEC_A, "targets": []},
        )
        cfg = await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=USER,
            thread_id=THREAD,
            request=req,
            requested_model=None,
            held_model=None,
            from_service=True,
            resolve=resolver,
        )
        assert attempts == [
            ("claude-opus-5-5-oauth", "low"),
            (fallback_model, "low"),
        ]
        assert cfg.llm.name == fallback_model
        assert cfg.resolved_reasoning_effort == expected_clamped


@pytest.mark.asyncio
async def test_first_turn_settings_sql_reads_earliest_response_metadata(monkeypatch) -> None:
    conn = create_autospec(AsyncConnection, instance=True)

    @asynccontextmanager
    async def _get_conn():
        yield conn

    @asynccontextmanager
    async def _cursor(*_a: Any, **_kw: Any):
        cur = create_autospec(AsyncCursor, instance=True)
        cur.fetchone.return_value = {
            "llm_model": "claude-opus-5-5-oauth",
            "effort_configured": True,
            "reasoning_effort": "medium",
        }
        yield cur

    conn.cursor.side_effect = _cursor
    monkeypatch.setattr(
        atm_mod,
        "get_db_connection",
        create_autospec(atm_mod.get_db_connection, side_effect=_get_conn),
    )

    model, configured, effort = await atm_mod._get_execution_first_turn_settings(
        {"automation_execution_id": EXEC_A, "conversation_thread_id": THREAD},
        fallback_thread_id=THREAD,
    )
    assert (model, configured, effort) == ("claude-opus-5-5-oauth", True, "medium")
