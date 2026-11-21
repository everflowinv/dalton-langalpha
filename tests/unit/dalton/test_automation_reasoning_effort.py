"""Tests for per-automation reasoning_effort across REST, file, company, CoS tool, locked recheck, and snapshots."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from fastapi import HTTPException, Request
from psycopg import AsyncConnection

from ptc_agent.agent.backends.automations import AutomationsBackend, _README_CONTENT
from ptc_agent.agent.backends.db_json_route import UserDataValidationError
from ptc_agent.agent.backends.sandbox import SandboxBackend
from ptc_agent.core.sandbox.livefs_mount import CallContext
from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.services import automation_recycle, company_agent_writes, company_automations
from src.dalton.tools.director_tools import build_director_tools
from src.server.app import automations as automations_router
from src.server.database import automation as auto_db
from src.server.database import workspace as ws_db
from src.server.dependencies.principal import Principal
from src.server.models.automation import AutomationCreate, AutomationUpdate
from src.server.services.automations import file as auto_file
from src.server.services.automations import lifecycle
from src.server.services.llm.user_models import ModelSource
from tests.unit.server.services.automations._fake_db import FakeAutomationsDb

USER = "user-effort-1"
WS = "11111111-1111-4111-8111-111111111111"
AUTO_ID = "22222222-2222-4222-8222-222222222222"

_MODELS: dict[str, tuple[str, dict[str, Any]]] = {
    "claude-opus-5-5-oauth": (
        ModelSource.SYSTEM,
        {
            "provider": "claude-oauth",
            "reasoning": {"efforts": ["low", "medium", "high", "max"], "default": "high"},
        },
    ),
    "gpt-6.2-sol-oauth": (
        ModelSource.SYSTEM,
        {
            "provider": "codex-oauth",
            "reasoning": {"efforts": ["low", "medium", "high"], "default": "medium"},
        },
    ),
    "plain-chat-model": (
        ModelSource.SYSTEM,
        {"provider": "openai"},
    ),
    "gemini-4-neon-high-oauth": (
        ModelSource.SYSTEM,
        {
            "provider": "antigravity-oauth",
            "reasoning": {"efforts": ["high"], "default": "high"},
        },
    ),
    "gemini-4-neon-medium-oauth": (
        ModelSource.SYSTEM,
        {
            "provider": "antigravity-oauth",
            "reasoning": {"efforts": ["medium"], "default": "medium"},
        },
    ),
}


def _row(**overrides: Any) -> dict[str, Any]:
    now = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    base: dict[str, Any] = {
        "automation_id": UUID(AUTO_ID),
        "user_id": USER,
        "name": "Morning Brief",
        "file_name": "morning-brief.json",
        "description": None,
        "trigger_type": "cron",
        "cron_expression": "0 9 * * 1-5",
        "timezone": "UTC",
        "trigger_config": {},
        "next_run_at": now,
        "last_run_at": None,
        "instruction": "Summarize market open.",
        "workspace_id": UUID(WS),
        "llm_model": "claude-opus-5-5-oauth",
        "reasoning_effort": "high",
        "additional_context": None,
        "thread_strategy": "new",
        "conversation_thread_id": None,
        "status": "active",
        "max_failures": 3,
        "failure_count": 0,
        "disable_reason": None,
        "delivery_config": {},
        "metadata": {},
        "scope": "personal",
        "notify_storage_mode": "legacy",
        "config_version": 1,
        "active_version": 1,
        "created_by": USER,
        "updated_by": USER,
        "deleted_at": None,
        "deleted_by": None,
        "created_at": now,
        "updated_at": now,
        "last_execution": None,
    }
    base.update(overrides)
    return base


def _install_model_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra_models: dict[str, tuple[str, dict[str, Any]]] | None = None,
    pref: dict[str, Any] | None = None,
) -> None:
    catalog = {**_MODELS, **(extra_models or {})}
    user_pref = dict(pref or {})

    async def _classify(user_id: str, model_name: str, _pref_cache: Any = None, **_kw: Any):
        return catalog.get(model_name, (ModelSource.UNKNOWN, {}))

    monkeypatch.setattr(
        lifecycle.user_models,
        "classify_model",
        create_autospec(lifecycle.user_models.classify_model, side_effect=_classify),
    )
    monkeypatch.setattr(
        lifecycle.user_models,
        "get_model_preference",
        create_autospec(lifecycle.user_models.get_model_preference, return_value=user_pref),
    )
    monkeypatch.setattr(
        lifecycle.user_models,
        "get_custom_provider_config",
        create_autospec(lifecycle.user_models.get_custom_provider_config, return_value=None),
    )
    monkeypatch.setattr(
        lifecycle,
        "check_claude_code_hosted",
        create_autospec(lifecycle.check_claude_code_hosted, return_value=None),
    )
    monkeypatch.setattr(
        lifecycle,
        "get_workspace",
        create_autospec(lifecycle.get_workspace, return_value={"workspace_id": WS, "user_id": USER}),
    )
    monkeypatch.setattr(
        lifecycle,
        "require_target_ownership",
        create_autospec(lifecycle.require_target_ownership, return_value=None),
    )


@pytest.mark.asyncio
async def test_rest_create_and_patch_save_and_clear_reasoning_effort(monkeypatch) -> None:
    _install_model_stubs(monkeypatch)
    created_kwargs: dict[str, Any] = {}
    updated_kwargs: list[dict[str, Any]] = []

    async def _fake_create(*_a: Any, **kw: Any) -> dict[str, Any]:
        created_kwargs.update(kw)
        return _row(llm_model=kw["llm_model"], reasoning_effort=kw["reasoning_effort"])

    async def _fake_update(aid: str, uid: str, *, conn: Any = None, **kw: Any) -> dict[str, Any]:
        updated_kwargs.append(dict(kw))
        return _row(
            llm_model=kw.get("llm_model", "claude-opus-5-5-oauth"),
            reasoning_effort=kw.get("reasoning_effort", "high"),
        )

    monkeypatch.setattr(
        lifecycle.auto_db,
        "create_automation",
        create_autospec(auto_db.create_automation, side_effect=_fake_create),
    )
    monkeypatch.setattr(
        lifecycle.auto_db,
        "get_automation",
        create_autospec(auto_db.get_automation, return_value=_row(reasoning_effort="high")),
    )
    monkeypatch.setattr(
        lifecycle.auto_db,
        "update_automation",
        create_autospec(auto_db.update_automation, side_effect=_fake_update),
    )
    monkeypatch.setattr(
        automations_router.cli_approval,
        "automation_runs",
        create_autospec(automations_router.cli_approval.automation_runs, return_value=None),
    )
    monkeypatch.setattr(
        "src.dalton.notify.dispatch.populate_automation_notify_state",
        create_autospec(
            __import__("src.dalton.notify.dispatch", fromlist=["populate_automation_notify_state"]).populate_automation_notify_state,
            return_value=None,
        ),
    )
    monkeypatch.setattr("src.dalton.settings.is_dalton_enabled", lambda: False)
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: False)

    principal = Principal(user_id=USER, auth="user")
    http_req = create_autospec(Request, instance=True)

    # 1. Create on multi-tier model omitting reasoning_effort -> defaults to "medium"
    resp_create = await automations_router.create_automation(
        AutomationCreate(
            name="Opus Task",
            trigger_type="cron",
            cron_expression="0 9 * * *",
            instruction="Run research.",
            workspace_id=UUID(WS),
            llm_model="claude-opus-5-5-oauth",
        ),
        principal,
        http_req,
    )
    assert created_kwargs["reasoning_effort"] == "medium"
    assert resp_create.reasoning_effort == "medium"

    # 2. PATCH updating reasoning_effort explicitly to "low"
    resp_patch = await automations_router.update_automation(
        AUTO_ID,
        principal,
        http_req,
        body={"reasoning_effort": "low"},
    )
    assert updated_kwargs[-1]["reasoning_effort"] == "low"
    assert resp_patch.reasoning_effort == "low"

    # 3. PATCH explicitly clearing reasoning_effort with null
    resp_clear = await automations_router.update_automation(
        AUTO_ID,
        principal,
        http_req,
        body={"reasoning_effort": None},
    )
    assert "reasoning_effort" in updated_kwargs[-1]
    assert updated_kwargs[-1]["reasoning_effort"] is None
    assert resp_clear.reasoning_effort is None

    # 4. PATCH omitting reasoning_effort leaves stored value untouched
    await automations_router.update_automation(
        AUTO_ID,
        principal,
        http_req,
        body={"name": "Renamed"},
    )
    assert "reasoning_effort" not in updated_kwargs[-1]


@pytest.mark.asyncio
async def test_rest_antigravity_keeps_null_clears_stale_and_refuses_conflict_422(monkeypatch) -> None:
    _install_model_stubs(
        monkeypatch,
        extra_models={
            # User custom model shadowing an Antigravity-looking name must NOT be treated as Antigravity
            "gemini-4-neon-custom-oauth": (
                ModelSource.CUSTOM,
                {
                    "name": "gemini-4-neon-custom-oauth",
                    "provider": "antigravity-oauth",
                    "reasoning": {"efforts": ["low", "medium", "high"], "default": "medium"},
                },
            ),
        },
    )
    captured: list[dict[str, Any]] = []

    async def _fake_create(*_a: Any, **kw: Any) -> dict[str, Any]:
        captured.append(dict(kw))
        return _row(llm_model=kw["llm_model"], reasoning_effort=kw["reasoning_effort"])

    async def _fake_update(aid: str, uid: str, *, conn: Any = None, **kw: Any) -> dict[str, Any]:
        captured.append(dict(kw))
        return _row(
            llm_model=kw.get("llm_model", "gemini-4-neon-high-oauth"),
            reasoning_effort=kw.get("reasoning_effort"),
        )

    monkeypatch.setattr(
        lifecycle.auto_db,
        "create_automation",
        create_autospec(auto_db.create_automation, side_effect=_fake_create),
    )
    monkeypatch.setattr(
        lifecycle.auto_db,
        "get_automation",
        create_autospec(
            auto_db.get_automation,
            return_value=_row(llm_model="claude-opus-5-5-oauth", reasoning_effort="high"),
        ),
    )
    monkeypatch.setattr(
        lifecycle.auto_db,
        "update_automation",
        create_autospec(auto_db.update_automation, side_effect=_fake_update),
    )
    monkeypatch.setattr(
        automations_router.cli_approval,
        "automation_runs",
        create_autospec(automations_router.cli_approval.automation_runs, return_value=None),
    )
    monkeypatch.setattr(
        "src.dalton.notify.dispatch.populate_automation_notify_state",
        create_autospec(
            __import__("src.dalton.notify.dispatch", fromlist=["populate_automation_notify_state"]).populate_automation_notify_state,
            return_value=None,
        ),
    )
    monkeypatch.setattr("src.dalton.settings.is_dalton_enabled", lambda: False)
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: False)

    principal = Principal(user_id=USER, auth="user")
    http_req = create_autospec(Request, instance=True)

    # Create with Antigravity + matching suffix effort -> normalized to NULL
    resp_ag = await automations_router.create_automation(
        AutomationCreate(
            name="Neon Task",
            trigger_type="cron",
            cron_expression="0 9 * * *",
            instruction="Check.",
            workspace_id=UUID(WS),
            llm_model="gemini-4-neon-high-oauth",
            reasoning_effort="high",
        ),
        principal,
        http_req,
    )
    assert captured[-1]["reasoning_effort"] is None
    assert resp_ag.reasoning_effort is None

    # Create with Antigravity + conflicting effort -> HTTP 422
    with pytest.raises(HTTPException) as exc_info:
        await automations_router.create_automation(
            AutomationCreate(
                name="Neon Conflict",
                trigger_type="cron",
                cron_expression="0 9 * * *",
                instruction="Check.",
                workspace_id=UUID(WS),
                llm_model="gemini-4-neon-high-oauth",
                reasoning_effort="low",
            ),
            principal,
            http_req,
        )
    assert exc_info.value.status_code == 422

    # PATCH switching llm_model to Antigravity WITHOUT passing reasoning_effort -> auto-clears old "high" to None
    resp_switch = await automations_router.update_automation(
        AUTO_ID,
        principal,
        http_req,
        body={"llm_model": "gemini-4-neon-medium-oauth"},
    )
    assert captured[-1]["llm_model"] == "gemini-4-neon-medium-oauth"
    assert "reasoning_effort" in captured[-1]
    assert captured[-1]["reasoning_effort"] is None
    assert resp_switch.reasoning_effort is None

    # Custom model with same provider name keeps explicit reasoning_effort
    resp_custom = await automations_router.create_automation(
        AutomationCreate(
            name="Custom Shadow",
            trigger_type="cron",
            cron_expression="0 9 * * *",
            instruction="Check.",
            workspace_id=UUID(WS),
            llm_model="gemini-4-neon-custom-oauth",
            reasoning_effort="low",
        ),
        principal,
        http_req,
    )
    assert captured[-1]["reasoning_effort"] == "low"
    assert resp_custom.reasoning_effort == "low"


@pytest.mark.asyncio
async def test_company_agent_write_saves_clears_and_refuses_invalid_effort(monkeypatch) -> None:
    _install_model_stubs(monkeypatch)
    company_row = _row(
        user_id=COMPANY_SYSTEM_USER_ID,
        scope="company",
        notify_storage_mode="task_row",
        llm_model="claude-opus-5-5-oauth",
        reasoning_effort="medium",
    )
    written: list[dict[str, Any]] = []

    async def _fake_update(aid: str, uid: str, *, conn: Any = None, **kw: Any):
        written.append(dict(kw))
        return {**company_row, **kw}

    monkeypatch.setattr(
        company_agent_writes,
        "_company_write_allowed",
        create_autospec(company_agent_writes._company_write_allowed, return_value=True),
    )
    monkeypatch.setattr(
        company_agent_writes,
        "_model_pref_for_lock",
        create_autospec(company_agent_writes._model_pref_for_lock, return_value={}),
    )
    monkeypatch.setattr(
        lifecycle.auto_db,
        "get_automation",
        create_autospec(auto_db.get_automation, return_value=company_row),
    )
    monkeypatch.setattr(
        company_agent_writes.auto_db,
        "update_automation",
        create_autospec(auto_db.update_automation, side_effect=_fake_update),
    )

    principal = Principal(user_id=USER, auth="user")

    res_set = await company_agent_writes.update_company_agent(
        principal, AUTO_ID, {"reasoning_effort": "high"}, expected_config_version=1
    )
    assert res_set is not None and res_set["reasoning_effort"] == "high"
    assert written[-1]["reasoning_effort"] == "high"

    res_clear = await company_agent_writes.update_company_agent(
        principal, AUTO_ID, {"reasoning_effort": None}, expected_config_version=1
    )
    assert res_clear is not None and res_clear["reasoning_effort"] is None
    assert "reasoning_effort" in written[-1] and written[-1]["reasoning_effort"] is None

    with pytest.raises(HTTPException) as exc_info:
        await company_agent_writes.update_company_agent(
            principal,
            AUTO_ID,
            {"llm_model": "gemini-4-neon-high-oauth", "reasoning_effort": "low"},
            expected_config_version=1,
        )
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_sandbox_file_create_update_clear_and_antigravity_refusal(monkeypatch) -> None:
    _install_model_stubs(monkeypatch)
    assert "reasoning_effort" in _README_CONTENT

    db = FakeAutomationsDb(USER, [AUTO_ID])
    monkeypatch.setattr(auto_file, "auto_db", db)
    monkeypatch.setattr(lifecycle, "auto_db", db)

    @asynccontextmanager
    async def _conn():
        yield db.conn

    from ptc_agent.agent.backends import db_json_route

    monkeypatch.setattr(db_json_route, "get_db_connection", _conn)
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: False)

    root = f"/workspace/{AutomationsBackend.directory}"
    backend = AutomationsBackend(
        user_id=USER,
        call=CallContext(workspace_id=WS, thread_id=None, timezone="UTC"),
        sandbox_backend=create_autospec(SandboxBackend, instance=True),
        root_prefix=root,
    )
    path = f"{root}/effort-brief.json"

    # Create with explicit effort
    created_payload = {
        "name": "Effort Brief",
        "trigger_type": "cron",
        "cron_expression": "0 9 * * 1-5",
        "instruction": "Check market.",
        "workspace_id": WS,
        "llm_model": "claude-opus-5-5-oauth",
        "reasoning_effort": "high",
    }
    await backend.aread_range(path, 0, 2000)
    res_create = await backend.awrite_text(path, json.dumps(created_payload))
    assert "created" in res_create["message"]
    assert db.rows[AUTO_ID]["reasoning_effort"] == "high"

    # Read back includes reasoning_effort
    rendered = await backend.aread_range(path, 0, 2000)
    assert '"reasoning_effort": "high"' in rendered

    # Clear with null via file edit
    updated_payload = {**json.loads(rendered), "reasoning_effort": None}
    res_clear = await backend.awrite_text(path, json.dumps(updated_payload))
    assert "updated" in res_clear["message"]
    assert db.rows[AUTO_ID]["reasoning_effort"] is None

    # Conflicting Antigravity effort is refused with UserDataValidationError on reasoning_effort
    current_rendered = await backend.aread_range(path, 0, 2000)
    bad_payload = {
        **json.loads(current_rendered),
        "llm_model": "gemini-4-neon-high-oauth",
        "reasoning_effort": "low",
    }
    with pytest.raises(UserDataValidationError) as exc_info:
        await backend.awrite_text(path, json.dumps(bad_payload))
    assert "reasoning_effort" in str(exc_info.value)


@pytest.mark.asyncio
async def test_dalton_schedule_automation_tool_saves_clears_lists_and_docs(monkeypatch) -> None:
    _install_model_stubs(monkeypatch)
    import src.dalton.autonomy.schedule as schedule_mod

    tools = {t.name: t for t in build_director_tools(role="chief_of_staff")}
    sched = tools["dalton_schedule_automation"]
    assert "reasoning_effort" in (sched.description or "")

    lock_conn = create_autospec(AsyncConnection, instance=True)

    @asynccontextmanager
    async def _tx(*_a: Any, **_kw: Any):
        yield lock_conn

    lock_conn.transaction.side_effect = _tx

    @asynccontextmanager
    async def _get_conn():
        yield lock_conn

    monkeypatch.setattr(
        schedule_mod,
        "get_db_connection",
        create_autospec(schedule_mod.get_db_connection, side_effect=_get_conn),
    )
    monkeypatch.setattr(
        auto_db,
        "lock_user_automations",
        create_autospec(auto_db.lock_user_automations, return_value=None),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, return_value={"workspace_id": WS, "user_id": USER}),
    )

    store: list[dict[str, Any]] = []

    async def _list_all(uid: str, *, conn: Any = None):
        return list(store)

    async def _create(*_a: Any, **kw: Any):
        row = {
            "automation_id": AUTO_ID,
            "user_id": kw["user_id"],
            "name": kw["name"],
            "workspace_id": kw["workspace_id"],
            "trigger_type": kw["trigger_type"],
            "cron_expression": kw.get("cron_expression"),
            "next_run_at": kw.get("next_run_at"),
            "status": kw.get("status", "active"),
            "llm_model": kw.get("llm_model"),
            "reasoning_effort": kw.get("reasoning_effort"),
            "metadata": kw.get("metadata") or {},
        }
        store.append(row)
        return row

    async def _update(aid: str, uid: str, *, conn: Any = None, **kw: Any):
        for r in store:
            if r["automation_id"] == aid:
                r.update(kw)
                return r
        return None

    monkeypatch.setattr(
        auto_db, "list_all_automations", create_autospec(auto_db.list_all_automations, side_effect=_list_all)
    )
    monkeypatch.setattr(
        auto_db, "create_automation", create_autospec(auto_db.create_automation, side_effect=_create)
    )
    monkeypatch.setattr(
        auto_db, "update_automation", create_autospec(auto_db.update_automation, side_effect=_update)
    )
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: False)

    cfg = {"configurable": {"user_id": USER, "workspace_id": WS}}

    # Create with reasoning_effort="high"
    out_create = json.loads(
        await sched.ainvoke(
            {
                "action": "upsert",
                "name": "dalton-nvda-watch",
                "trigger_type": "cron",
                "cron_expression": "0 9 * * 1-5",
                "instruction": "Check NVDA.",
                "workspace_id": WS,
                "reasoning_effort": "high",
            },
            config=cfg,
        )
    )
    assert out_create["ok"] is True
    assert store[0]["reasoning_effort"] == "high"

    # List exposes reasoning_effort
    out_list = json.loads(
        await sched.ainvoke({"action": "list", "workspace_id": WS}, config=cfg)
    )
    assert out_list["automations"][0]["reasoning_effort"] == "high"

    # Update with reasoning_effort="default" clears back to None
    out_clear = json.loads(
        await sched.ainvoke(
            {
                "action": "upsert",
                "name": "dalton-nvda-watch",
                "workspace_id": WS,
                "reasoning_effort": "default",
            },
            config=cfg,
        )
    )
    assert out_clear["ok"] is True
    assert store[0]["reasoning_effort"] is None


@pytest.mark.asyncio
async def test_locked_recheck_clears_effort_when_concurrent_writer_switched_to_antigravity(
    monkeypatch,
) -> None:
    _install_model_stubs(monkeypatch)
    current = _row(llm_model="claude-opus-5-5-oauth", reasoning_effort="max")
    # Concurrent writer switched the row to Antigravity before our lock
    locked_ag = {**current, "llm_model": "gemini-4-neon-high-oauth", "reasoning_effort": "max"}
    effective_kwargs: dict[str, Any] = {"name": "Renamed"}
    await lifecycle.recheck_locked_automation_update(
        user_id=USER,
        update=AutomationUpdate(name="Renamed"),
        current=current,
        stored_in=WS,
        stored_pin=None,
        new_cron=None,
        new_tz=None,
        pref_for_lock={},
        locked_row=locked_ag,
        effective_kwargs=effective_kwargs,
        locked_conn=None,
    )
    assert "reasoning_effort" in effective_kwargs
    assert effective_kwargs["reasoning_effort"] is None

    # Concurrent writer switched model to narrower ladder (gpt-6.2 max -> high)
    locked_narrow = {**current, "llm_model": "claude-opus-5-5-oauth", "reasoning_effort": "max"}
    eff_narrow: dict[str, Any] = {"llm_model": "gpt-6.2-sol-oauth"}
    await lifecycle.recheck_locked_automation_update(
        user_id=USER,
        update=AutomationUpdate(llm_model="gpt-6.2-sol-oauth"),
        current=current,
        stored_in=WS,
        stored_pin=None,
        new_cron=None,
        new_tz=None,
        pref_for_lock={},
        locked_row=locked_narrow,
        effective_kwargs=eff_narrow,
        locked_conn=None,
    )
    assert eff_narrow["reasoning_effort"] == "high"


def test_audit_snapshot_tracks_19_fields_and_rollback_accepts_pre_d042_snapshot() -> None:
    assert "reasoning_effort" in company_automations.ALLOWED_AUTOMATION_SNAPSHOT_FIELDS
    assert len(company_automations.ALLOWED_AUTOMATION_SNAPSHOT_FIELDS) == 19

    snap_before = company_automations.build_automation_config_snapshot(
        _row(reasoning_effort="medium")
    )
    snap_after = company_automations.build_automation_config_snapshot(
        _row(reasoning_effort="high")
    )
    assert snap_before["reasoning_effort"] == "medium"
    assert snap_after["reasoning_effort"] == "high"
    changes = company_automations.compute_snapshot_changes(snap_before, snap_after)
    assert changes == {"reasoning_effort": {"old": "medium", "new": "high"}}

    # Legacy pre-d042 snapshot missing reasoning_effort must NOT raise 409
    legacy_snap = dict(snap_before)
    legacy_snap.pop("reasoning_effort")
    validated_legacy = automation_recycle._require_complete_snapshot(legacy_snap)
    assert validated_legacy["reasoning_effort"] is None

    # Snapshot with built-in Antigravity model normalizes reasoning_effort to None
    ag_snap = {**snap_before, "llm_model": "gemini-4-neon-high-oauth", "reasoning_effort": "high"}
    validated_ag = automation_recycle._require_complete_snapshot(ag_snap)
    assert validated_ag["reasoning_effort"] is None

    # Snapshot with invalid reasoning_effort string raises 409
    bad_snap = {**snap_before, "reasoning_effort": "ultra"}
    with pytest.raises(HTTPException) as exc_info:
        automation_recycle._require_complete_snapshot(bad_snap)
    assert exc_info.value.status_code == 409
