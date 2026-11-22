"""Tests for dalton_schedule_automation llm_model, max_failures, and docstring contract."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import create_autospec

import pytest
from psycopg import AsyncConnection

import src.dalton.autonomy.schedule as schedule_mod
from src.dalton.llm.providers import ProviderDisabledError
from src.dalton.tools.director_tools import build_director_tools
from src.server.database import automation as auto_db
from src.server.database import workspace as ws_db
from src.server.services.automations import lifecycle
from src.server.services.llm.user_models import ModelSource

USER = "user-sched-1"
WS = "11111111-1111-4111-8111-111111111111"
AUTO_ID = "22222222-2222-4222-8222-222222222222"

_CATALOG: dict[str, tuple[str, dict[str, Any]]] = {
    "claude-opus-5-5-oauth": (
        ModelSource.SYSTEM,
        {
            "provider": "claude-oauth",
            "reasoning": {"efforts": ["low", "medium", "high", "max"], "default": "high"},
        },
    ),
    "gemini-4-neon-high-oauth": (
        ModelSource.SYSTEM,
        {
            "provider": "antigravity-oauth",
            "reasoning": {"efforts": ["high"], "default": "high"},
        },
    ),
    "disabled-provider-model": (
        ModelSource.SYSTEM,
        {"provider": "disabled-vendor"},
    ),
}


def _install_schedule_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: list[dict[str, Any]],
    disabled_providers: set[str] | None = None,
    claude_code_refusal: lifecycle.AutomationRefusal | None = None,
) -> None:
    lock_conn = create_autospec(AsyncConnection, instance=True)

    @asynccontextmanager
    async def _tx(*_a: Any, **_kw: Any):
        yield lock_conn

    lock_conn.transaction.side_effect = _tx

    @asynccontextmanager
    async def _get_conn():
        yield lock_conn

    async def _classify(_uid: str, model_name: str, _pref_cache: Any = None, **_kw: Any):
        return _CATALOG.get(model_name, (ModelSource.UNKNOWN, {}))

    monkeypatch.setattr(
        lifecycle.user_models,
        "classify_model",
        create_autospec(lifecycle.user_models.classify_model, side_effect=_classify),
    )
    monkeypatch.setattr(
        lifecycle.user_models,
        "get_model_preference",
        create_autospec(lifecycle.user_models.get_model_preference, return_value={}),
    )
    monkeypatch.setattr(
        lifecycle.user_models,
        "get_custom_provider_config",
        create_autospec(lifecycle.user_models.get_custom_provider_config, return_value=None),
    )
    if claude_code_refusal is not None:
        monkeypatch.setattr(
            lifecycle,
            "check_claude_code_hosted",
            create_autospec(
                lifecycle.check_claude_code_hosted, side_effect=claude_code_refusal
            ),
        )
    else:
        monkeypatch.setattr(
            lifecycle,
            "check_claude_code_hosted",
            create_autospec(lifecycle.check_claude_code_hosted, return_value=None),
        )
    if disabled_providers:
        import src.dalton.llm.providers as providers_mod

        def _ensure(provider: str, parent_provider: str | None = None) -> None:
            if provider in disabled_providers or (
                parent_provider and parent_provider in disabled_providers
            ):
                raise ProviderDisabledError(provider)

        monkeypatch.setattr(
            providers_mod,
            "ensure_provider_enabled",
            create_autospec(providers_mod.ensure_provider_enabled, side_effect=_ensure),
        )

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
        create_autospec(
            ws_db.get_workspace, return_value={"workspace_id": WS, "user_id": USER}
        ),
    )

    async def _list_all(_uid: str, *, conn: Any = None):
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
            "max_failures": kw.get("max_failures", 3),
            "metadata": kw.get("metadata") or {},
        }
        store.append(row)
        return row

    async def _update(aid: str, _uid: str, *, conn: Any = None, **kw: Any):
        for r in store:
            if r["automation_id"] == aid:
                r.update(kw)
                return r
        return None

    monkeypatch.setattr(
        auto_db,
        "list_all_automations",
        create_autospec(auto_db.list_all_automations, side_effect=_list_all),
    )
    monkeypatch.setattr(
        auto_db,
        "create_automation",
        create_autospec(auto_db.create_automation, side_effect=_create),
    )
    monkeypatch.setattr(
        auto_db,
        "update_automation",
        create_autospec(auto_db.update_automation, side_effect=_update),
    )
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: False)


@pytest.mark.asyncio
async def test_schedule_automation_sets_updates_and_clears_llm_model_and_max_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: list[dict[str, Any]] = []
    _install_schedule_env(monkeypatch, store=store)
    sched = {t.name: t for t in build_director_tools(role="chief_of_staff")}[
        "dalton_schedule_automation"
    ]
    cfg = {"configurable": {"user_id": USER, "workspace_id": WS}}

    # Create with explicit llm_model, reasoning_effort, and max_failures
    created = json.loads(
        await sched.ainvoke(
            {
                "action": "upsert",
                "name": "dalton-nvda-watch",
                "trigger_type": "cron",
                "cron_expression": "0 9 * * 1-5",
                "instruction": "Check NVDA.",
                "workspace_id": WS,
                "llm_model": "claude-opus-5-5-oauth",
                "reasoning_effort": "high",
                "max_failures": 5,
            },
            config=cfg,
        )
    )
    assert created["ok"] is True
    assert store[0]["llm_model"] == "claude-opus-5-5-oauth"
    assert store[0]["reasoning_effort"] == "high"
    assert store[0]["max_failures"] == 5

    # List surfaces llm_model and max_failures
    listed = json.loads(
        await sched.ainvoke({"action": "list", "workspace_id": WS}, config=cfg)
    )
    assert listed["automations"][0]["llm_model"] == "claude-opus-5-5-oauth"
    assert listed["automations"][0]["max_failures"] == 5

    # Switching to built-in Antigravity updates llm_model and auto-clears stale effort
    switched = json.loads(
        await sched.ainvoke(
            {
                "action": "upsert",
                "name": "dalton-nvda-watch",
                "workspace_id": WS,
                "llm_model": "gemini-4-neon-high-oauth",
                "max_failures": 12,
            },
            config=cfg,
        )
    )
    assert switched["ok"] is True
    assert store[0]["llm_model"] == "gemini-4-neon-high-oauth"
    assert store[0]["reasoning_effort"] is None
    assert store[0]["max_failures"] == 12

    # Clearing llm_model with "default" sets it back to None
    cleared_default = json.loads(
        await sched.ainvoke(
            {
                "action": "upsert",
                "name": "dalton-nvda-watch",
                "workspace_id": WS,
                "llm_model": "default",
            },
            config=cfg,
        )
    )
    assert cleared_default["ok"] is True
    assert store[0]["llm_model"] is None

    # Set again and clear with empty string ""
    await sched.ainvoke(
        {
            "action": "upsert",
            "name": "dalton-nvda-watch",
            "workspace_id": WS,
            "llm_model": "claude-opus-5-5-oauth",
        },
        config=cfg,
    )
    assert store[0]["llm_model"] == "claude-opus-5-5-oauth"

    cleared_empty = json.loads(
        await sched.ainvoke(
            {
                "action": "upsert",
                "name": "dalton-nvda-watch",
                "workspace_id": WS,
                "llm_model": "",
            },
            config=cfg,
        )
    )
    assert cleared_empty["ok"] is True
    assert store[0]["llm_model"] is None


@pytest.mark.asyncio
async def test_schedule_automation_rejects_unknown_disabled_and_hosted_refused_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: list[dict[str, Any]] = []
    _install_schedule_env(
        monkeypatch, store=store, disabled_providers={"disabled-vendor"}
    )
    sched = {t.name: t for t in build_director_tools(role="chief_of_staff")}[
        "dalton_schedule_automation"
    ]
    cfg = {"configurable": {"user_id": USER, "workspace_id": WS}}

    unknown = json.loads(
        await sched.ainvoke(
            {
                "action": "upsert",
                "name": "dalton-nvda-watch",
                "trigger_type": "cron",
                "cron_expression": "0 9 * * 1-5",
                "instruction": "Check.",
                "workspace_id": WS,
                "llm_model": "nonexistent-model-xyz",
            },
            config=cfg,
        )
    )
    assert unknown["ok"] is False
    assert unknown["error"] == "invalid_llm_model"
    assert store == []

    disabled = json.loads(
        await sched.ainvoke(
            {
                "action": "upsert",
                "name": "dalton-nvda-watch",
                "trigger_type": "cron",
                "cron_expression": "0 9 * * 1-5",
                "instruction": "Check.",
                "workspace_id": WS,
                "llm_model": "disabled-provider-model",
            },
            config=cfg,
        )
    )
    assert disabled["ok"] is False
    assert disabled["error"] == "invalid_llm_model"
    assert store == []


@pytest.mark.asyncio
async def test_schedule_automation_rejects_hosted_claude_code_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: list[dict[str, Any]] = []
    _install_schedule_env(
        monkeypatch,
        store=store,
        claude_code_refusal=lifecycle.AutomationRefusal(
            "Claude Code is disabled on this workspace", field="llm_model"
        ),
    )
    sched = {t.name: t for t in build_director_tools(role="chief_of_staff")}[
        "dalton_schedule_automation"
    ]
    cfg = {"configurable": {"user_id": USER, "workspace_id": WS}}

    hosted_refused = json.loads(
        await sched.ainvoke(
            {
                "action": "upsert",
                "name": "dalton-nvda-watch",
                "trigger_type": "cron",
                "cron_expression": "0 9 * * 1-5",
                "instruction": "Check.",
                "workspace_id": WS,
                "llm_model": "claude-opus-5-5-oauth",
            },
            config=cfg,
        )
    )
    assert hosted_refused["ok"] is False
    assert hosted_refused["error"] == "invalid_llm_model"
    assert store == []


@pytest.mark.asyncio
async def test_schedule_automation_rejects_analyst_targeting_home_with_new_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: list[dict[str, Any]] = []
    _install_schedule_env(monkeypatch, store=store)
    analyst_sched = {t.name: t for t in build_director_tools(role="analyst")}[
        "dalton_schedule_automation"
    ]
    cfg = {"configurable": {"user_id": USER, "workspace_id": WS}}

    refused = json.loads(
        await analyst_sched.ainvoke(
            {
                "action": "upsert",
                "name": "dalton-home-brief",
                "trigger_type": "cron",
                "cron_expression": "0 9 * * 1-5",
                "instruction": "Check.",
                "workspace_id": "home",
                "llm_model": "claude-opus-5-5-oauth",
                "max_failures": 5,
            },
            config=cfg,
        )
    )
    assert refused["ok"] is False
    assert refused["error"] == "forbidden_scope"
    assert store == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", [0, -1, 101])
async def test_schedule_automation_rejects_out_of_range_max_failures(
    monkeypatch: pytest.MonkeyPatch, bad_value: int
) -> None:
    store: list[dict[str, Any]] = []
    _install_schedule_env(monkeypatch, store=store)
    sched = {t.name: t for t in build_director_tools(role="chief_of_staff")}[
        "dalton_schedule_automation"
    ]
    cfg = {"configurable": {"user_id": USER, "workspace_id": WS}}

    res = json.loads(
        await sched.ainvoke(
            {
                "action": "upsert",
                "name": "dalton-nvda-watch",
                "trigger_type": "cron",
                "cron_expression": "0 9 * * 1-5",
                "instruction": "Check.",
                "workspace_id": WS,
                "max_failures": bad_value,
            },
            config=cfg,
        )
    )
    assert res["ok"] is False
    assert res["error"] == "invalid_max_failures"
    assert store == []


@pytest.mark.asyncio
async def test_schedule_automation_helper_rejects_bool_max_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: list[dict[str, Any]] = []
    _install_schedule_env(monkeypatch, store=store)

    res = await schedule_mod.schedule_dalton_automation_helper(
        "dalton-nvda-watch",
        {
            "action": "upsert",
            "trigger_type": "cron",
            "cron_expression": "0 9 * * 1-5",
            "instruction": "Check.",
            "max_failures": True,
        },
        user_id=USER,
        workspace_id=WS,
    )
    assert res["ok"] is False
    assert res["error"] == "invalid_max_failures"
    assert store == []


def test_dalton_schedule_automation_docstring_matches_contract() -> None:
    sched = {t.name: t for t in build_director_tools(role="chief_of_staff")}[
        "dalton_schedule_automation"
    ]
    desc = sched.description or ""
    assert 'trigger_type: "cron" or "once"' in desc
    assert '"price"' not in desc
    assert "dalton_coverage_tracking" in desc
    assert "trigger_config" in desc
    assert "Asia/Shanghai" not in desc
    assert "deployment default" in desc
    assert "keep the existing timezone" in desc
    assert "does not move" in desc
    assert "llm_model:" in desc
    assert "max_failures:" in desc
    assert "1–100" in desc
    assert "Built-in Antigravity" in desc
