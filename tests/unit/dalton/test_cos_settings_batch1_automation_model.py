"""Batch 1 unit tests for managed automation llm_model, cross-user system-catalog enforcement,
in-transaction placement/auth rechecks, and end-to-end llm_model_system_only propagation.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from fastapi import HTTPException, Request
from psycopg import AsyncConnection, AsyncCursor

from src.dalton.app import router as dalton_router
from src.dalton.autonomy import schedule as schedule_mod
from src.dalton.services import automation_subscription_fallback as fb_mod
from src.dalton.services import automation_thread_model as atm_mod
from src.dalton.services import company_automations
from src.dalton.tools import director_tools as director_tools_mod
from src.dalton.tools.director_tools import build_director_tools
from src.server.app.threads import messaging as messaging_mod
from src.server.database import automation as auto_db
from src.server.database import computer as computer_db
from src.server.database.runs import lifecycle as runs_lifecycle_db
from src.server.handlers import chat as chat_handler_mod
from src.server.handlers import thread_maintenance as maint_mod
from src.server.models.chat import ChatMessage, ChatRequest, ThreadOrigin
from src.server.services import automation_executor as exec_mod
from src.server.services import home_placement as home_placement_mod
from src.server.services import thread_agent
from src.server.services.automations import lifecycle as lifecycle_mod
from src.server.services.claude_code import hosted as hosted_mod
from src.server.services.llm import availability as avail_mod
from src.server.services.llm import config as llm_config_mod
from src.server.services.llm import user_models
from src.server.services.shared_workspaces.turn import TurnIdentity
from src.server.services.turn_runtime import TurnRoute

OWNER = "11111111-1111-4111-8111-111111111101"
PM = "11111111-1111-4111-8111-111111111102"
WS = "22222222-2222-4222-8222-222222222201"
HOME_WS = "22222222-2222-4222-8222-222222222202"
COMP_1 = "33333333-3333-4333-8333-333333333301"
COMP_2 = "33333333-3333-4333-8333-333333333302"
AUTO_ID = "44444444-4444-4444-8444-444444444401"
THREAD_ID = "55555555-5555-4555-8555-555555555501"
EXEC_ID = "66666666-6666-4666-8666-666666666601"
WAKEUP_ID = "77777777-7777-4777-8777-777777777701"
RUN_ID = "88888888-8888-4888-8888-888888888801"

_SYSTEM_CATALOG: dict[str, dict[str, Any]] = {
    "claude-sonnet-4-6": {
        "provider": "anthropic",
        "model_id": "claude-sonnet-4-6",
        "reasoning": {"efforts": ["low", "medium", "high"], "default": "medium"},
    },
    "gpt-6.2-sol": {
        "provider": "openai",
        "model_id": "gpt-6.2-sol",
        "reasoning": {"efforts": ["low", "medium", "high"], "default": "medium"},
    },
    "gemini-4-neon-high-oauth": {
        "provider": "antigravity-oauth",
        "model_id": "gemini-4-neon-high",
        "reasoning": {"efforts": ["high"], "default": "high"},
    },
}


def _make_auto_row(**overrides: Any) -> dict[str, Any]:
    now = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)
    row: dict[str, Any] = {
        "automation_id": UUID(AUTO_ID),
        "user_id": OWNER,
        "name": "dalton-nvda-watch",
        "description": "Watch NVDA",
        "trigger_type": "cron",
        "cron_expression": "0 9 * * 1-5",
        "timezone": "UTC",
        "trigger_config": {},
        "next_run_at": now,
        "instruction": "Check NVDA filings.",
        "workspace_id": UUID(WS),
        "llm_model": "claude-sonnet-4-6",
        "reasoning_effort": "medium",
        "status": "active",
        "metadata": {"dalton_managed": True, "ticker": "NVDA", "unit_id": "unit-1"},
        "scope": "personal",
    }
    row.update(overrides)
    return row


def _install_catalog_and_prefs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    owner_pref: dict[str, Any],
) -> Any:
    """Install system catalog + owner preference mocks using create_autospec."""
    monkeypatch.setattr(
        lifecycle_mod,
        "get_configured_llm_models",
        create_autospec(
            lifecycle_mod.get_configured_llm_models,
            return_value={"anthropic": ["claude-sonnet-4-6"], "openai": ["gpt-6.2-sol"], "antigravity": ["gemini-4-neon-high-oauth"]},
        ),
    )

    async def _classify(
        uid: str,
        model_name: str,
        _pref_cache: dict | None = None,
        ignore_custom_models: bool = False,
    ) -> tuple[str, dict]:
        pref = _pref_cache if _pref_cache is not None else owner_pref
        if not ignore_custom_models:
            for cm in pref.get("custom_models") or []:
                if isinstance(cm, dict) and cm.get("name") == model_name:
                    return user_models.ModelSource.CUSTOM, dict(cm)
        if model_name in _SYSTEM_CATALOG:
            return user_models.ModelSource.SYSTEM, dict(_SYSTEM_CATALOG[model_name])
        return user_models.ModelSource.UNKNOWN, {}

    classify_mock = create_autospec(user_models.classify_model, side_effect=_classify)
    pref_mock = create_autospec(user_models.get_model_preference, return_value=dict(owner_pref))
    monkeypatch.setattr(user_models, "classify_model", classify_mock)
    monkeypatch.setattr(user_models, "get_model_preference", pref_mock)
    return pref_mock


# ---------------------------------------------------------------------------
# 1. Tool & REST parameter semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_director_tool_llm_model_parameter_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_specs: list[dict[str, Any]] = []

    async def _fake_helper(name, spec, **_kw):
        captured_specs.append(dict(spec))
        return {"ok": True, "action": "updated", "name": name}

    helper_mock = create_autospec(
        director_tools_mod.schedule_dalton_automation_helper, side_effect=_fake_helper
    )
    monkeypatch.setattr(director_tools_mod, "schedule_dalton_automation_helper", helper_mock)

    tool = {t.name: t for t in build_director_tools(role="chief_of_staff")}[
        "dalton_schedule_automation"
    ]
    cfg = {"configurable": {"user_id": OWNER, "workspace_id": WS}}

    # Omitted -> key absent from spec (preserves existing model on update)
    await tool.ainvoke({"action": "upsert", "name": "dalton-nvda-watch"}, config=cfg)
    assert "llm_model" not in captured_specs[-1]

    # Clear sentinels -> spec["llm_model"] = None
    for sentinel in ("default", "CLEAR", "", "   "):
        await tool.ainvoke(
            {"action": "upsert", "name": "dalton-nvda-watch", "llm_model": sentinel},
            config=cfg,
        )
        assert "llm_model" in captured_specs[-1]
        assert captured_specs[-1]["llm_model"] is None

    # Explicit model string -> trimmed
    await tool.ainvoke(
        {"action": "upsert", "name": "dalton-nvda-watch", "llm_model": " claude-sonnet-4-6 "},
        config=cfg,
    )
    assert captured_specs[-1]["llm_model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_rest_put_automation_llm_model_semantics_and_status_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    monkeypatch.setattr(dalton_router, "_require_dalton_enabled", create_autospec(dalton_router._require_dalton_enabled))
    monkeypatch.setattr(
        dalton_router,
        "_verify_workspace_access",
        create_autospec(dalton_router._verify_workspace_access, return_value={"workspace_id": WS, "user_id": OWNER}),
    )

    async def _fake_helper(**kw):
        captured.append(dict(kw["spec"]))
        return {"ok": True, "action": "updated", "name": kw["name"]}

    monkeypatch.setattr(
        director_tools_mod,
        "schedule_dalton_automation_helper",
        create_autospec(director_tools_mod.schedule_dalton_automation_helper, side_effect=_fake_helper),
    )

    # 1. Omitted llm_model -> key not present in spec
    body_omit = dalton_router.DaltonAutomationWriteRequest(workspace_id=WS, instruction="run")
    await dalton_router.put_dalton_automation_endpoint("dalton-nvda-watch", body_omit, OWNER)
    assert "llm_model" not in captured[-1]

    # 2. Explicit null -> spec["llm_model"] = None
    body_null = dalton_router.DaltonAutomationWriteRequest(workspace_id=WS, llm_model=None)
    await dalton_router.put_dalton_automation_endpoint("dalton-nvda-watch", body_null, OWNER)
    assert "llm_model" in captured[-1] and captured[-1]["llm_model"] is None

    # 3. "default" / "" -> spec["llm_model"] = None
    for sentinel in ("default", ""):
        body_clear = dalton_router.DaltonAutomationWriteRequest(workspace_id=WS, llm_model=sentinel)
        await dalton_router.put_dalton_automation_endpoint("dalton-nvda-watch", body_clear, OWNER)
        assert "llm_model" in captured[-1] and captured[-1]["llm_model"] is None

    # 4. AutomationRefusal(field="llm_model") -> 409; reasoning_effort -> 422
    orig_helper = schedule_mod.schedule_dalton_automation_helper
    monkeypatch.setattr(
        director_tools_mod,
        "schedule_dalton_automation_helper",
        create_autospec(
            orig_helper,
            side_effect=lifecycle_mod.AutomationRefusal("bad model", field="llm_model"),
        ),
    )
    with pytest.raises(HTTPException) as exc_409:
        await dalton_router.put_dalton_automation_endpoint("dalton-nvda-watch", body_omit, OWNER)
    assert exc_409.value.status_code == 409

    monkeypatch.setattr(
        director_tools_mod,
        "schedule_dalton_automation_helper",
        create_autospec(
            orig_helper,
            side_effect=lifecycle_mod.AutomationRefusal("bad effort", field="reasoning_effort"),
        ),
    )
    with pytest.raises(HTTPException) as exc_422:
        await dalton_router.put_dalton_automation_endpoint("dalton-nvda-watch", body_omit, OWNER)
    assert exc_422.value.status_code == 422


# ---------------------------------------------------------------------------
# 2. Pre-lock preference loading (zero calls inside _user_automations_lock_for_write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_get_model_preference_inside_user_automations_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inside_lock = False
    pref_calls: list[bool] = []

    async def _track_pref(uid: str) -> dict[str, Any]:
        pref_calls.append(inside_lock)
        return {}

    orig_get_pref = user_models.get_model_preference
    _install_catalog_and_prefs(monkeypatch, owner_pref={})
    monkeypatch.setattr(
        user_models,
        "get_model_preference",
        create_autospec(orig_get_pref, side_effect=_track_pref),
    )

    @asynccontextmanager
    async def _tracked_lock(uid: str, *, conn=None, prelock_ws=None):
        nonlocal inside_lock
        inside_lock = True
        try:
            yield None
        finally:
            inside_lock = False

    monkeypatch.setattr(schedule_mod, "_user_automations_lock_for_write", _tracked_lock)
    monkeypatch.setattr(
        schedule_mod,
        "verify_workspace_access",
        create_autospec(schedule_mod.verify_workspace_access, return_value={"workspace_id": WS, "user_id": OWNER}),
    )
    monkeypatch.setattr(
        computer_db,
        "get_computer_for_workspace",
        create_autospec(computer_db.get_computer_for_workspace, return_value={"computer_id": COMP_1}),
    )
    monkeypatch.setattr(
        lifecycle_mod,
        "check_claude_code_hosted",
        create_autospec(lifecycle_mod.check_claude_code_hosted, return_value=None),
    )
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: False)
    monkeypatch.setattr(
        "src.dalton.database.company_automations.resolve_automation_scope_for_write",
        create_autospec(
            __import__("src.dalton.database.company_automations", fromlist=["resolve_automation_scope_for_write"]).resolve_automation_scope_for_write,
            return_value="personal",
        ),
    )
    monkeypatch.setattr(
        auto_db,
        "list_all_automations",
        create_autospec(auto_db.list_all_automations, return_value=[]),
    )
    monkeypatch.setattr(
        auto_db,
        "create_automation",
        create_autospec(auto_db.create_automation, return_value=_make_auto_row()),
    )

    res = await schedule_mod.schedule_dalton_automation_helper(
        "dalton-nvda-watch",
        {
            "trigger_type": "cron",
            "cron_expression": "0 9 * * 1-5",
            "instruction": "Check NVDA.",
            "llm_model": "claude-sonnet-4-6",
            "reasoning_effort": "high",
        },
        user_id=OWNER,
        workspace_id=WS,
    )
    assert res["ok"] is True
    assert pref_calls, "Expected get_model_preference to be called pre-lock"
    assert all(called_inside is False for called_inside in pref_calls)


# ---------------------------------------------------------------------------
# 3. Cross-user system catalog restriction, error hiding, list masking, clear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_user_model_validation_system_only_flag_list_and_effort_hiding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_pref = {
        "custom_models": [
            {
                "name": "my-secret-private-model",
                "provider": "my-secret-gw",
                "model_id": "secret-v1",
            },
            {
                # Shadows system model claude-sonnet-4-6 with NO reasoning efforts
                "name": "claude-sonnet-4-6",
                "provider": "my-secret-gw",
                "model_id": "private-sonnet",
            },
        ],
        "custom_providers": [{"name": "my-secret-gw", "parent_provider": "openai"}],
    }
    _install_catalog_and_prefs(monkeypatch, owner_pref=owner_pref)

    check_model_spy = create_autospec(
        lifecycle_mod.check_model, side_effect=lifecycle_mod.check_model
    )
    custom_prov_spy = create_autospec(
        user_models.get_custom_provider_config, return_value={"name": "my-secret-gw"}
    )
    monkeypatch.setattr(lifecycle_mod, "check_model", check_model_spy)
    monkeypatch.setattr(user_models, "get_custom_provider_config", custom_prov_spy)
    monkeypatch.setattr(
        lifecycle_mod,
        "check_claude_code_hosted",
        create_autospec(lifecycle_mod.check_claude_code_hosted, return_value=None),
    )

    @asynccontextmanager
    async def _noop_lock(uid: str, *, conn=None, prelock_ws=None):
        yield None

    monkeypatch.setattr(schedule_mod, "_user_automations_lock_for_write", _noop_lock)
    monkeypatch.setattr(
        schedule_mod,
        "verify_workspace_access",
        create_autospec(schedule_mod.verify_workspace_access, return_value={"workspace_id": WS, "user_id": OWNER}),
    )
    monkeypatch.setattr(
        computer_db,
        "get_computer_for_workspace",
        create_autospec(computer_db.get_computer_for_workspace, return_value={"computer_id": COMP_1}),
    )
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr("src.dalton.settings.is_dalton_auth_enabled", lambda: True)
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(
        "src.dalton.autonomy.system_jobs.is_pm_user",
        lambda uid: uid == PM,
    )
    monkeypatch.setattr(
        "src.dalton.access_tiers.classify_owner_tier",
        create_autospec(
            __import__("src.dalton.access_tiers", fromlist=["classify_owner_tier"]).classify_owner_tier,
            return_value="internal",
        ),
    )
    unit_row = {
        "unit_id": "unit-1",
        "kind": "company",
        "ticker": "NVDA",
        "lead_analyst_user_id": OWNER,
        "owner_user_id": OWNER,
        "workspace_id": WS,
    }
    monkeypatch.setattr(
        "src.dalton.database.coverage_units.get_active_unit_by_workspace",
        create_autospec(
            __import__("src.dalton.database.coverage_units", fromlist=["get_active_unit_by_workspace"]).get_active_unit_by_workspace,
            return_value=unit_row,
        ),
    )

    stored_rows: list[dict[str, Any]] = [
        _make_auto_row(
            name="dalton-nvda-watch",
            llm_model="claude-sonnet-4-6",
            reasoning_effort=None,
            metadata={"dalton_managed": True, "ticker": "NVDA", "unit_id": "unit-1"},
        ),
        _make_auto_row(
            automation_id=UUID("44444444-4444-4444-8444-444444444402"),
            name="dalton-nvda-delta",
            llm_model="my-secret-private-model",
            reasoning_effort=None,
            metadata={"dalton_managed": True, "ticker": "NVDA", "unit_id": "unit-1"},
        ),
        _make_auto_row(
            automation_id=UUID("44444444-4444-4444-8444-444444444403"),
            name="dalton-nvda-system",
            llm_model="gpt-6.2-sol",
            reasoning_effort="low",
            metadata={"dalton_managed": True, "ticker": "NVDA", "unit_id": "unit-1"},
        ),
        _make_auto_row(
            automation_id=UUID("44444444-4444-4444-8444-444444444404"),
            name="dalton-nvda-default",
            llm_model=None,
            reasoning_effort=None,
            metadata={"dalton_managed": True, "ticker": "NVDA", "unit_id": "unit-1"},
        ),
    ]
    captured_updates: list[dict[str, Any]] = []

    async def _fake_list_all(uid: str, *, conn=None):
        return [dict(r) for r in stored_rows if r["user_id"] == uid]

    async def _fake_update(aid: str, uid: str, *, conn=None, **kw):
        captured_updates.append(dict(kw))
        for r in stored_rows:
            if str(r["automation_id"]) == str(aid):
                r.update(kw)
                return dict(r)
        return None

    monkeypatch.setattr(auto_db, "list_all_automations", create_autospec(auto_db.list_all_automations, side_effect=_fake_list_all))
    monkeypatch.setattr(auto_db, "update_automation", create_autospec(auto_db.update_automation, side_effect=_fake_update))
    monkeypatch.setattr(
        "src.dalton.database.company_automations.resolve_automation_scope_for_write",
        create_autospec(
            __import__("src.dalton.database.company_automations", fromlist=["resolve_automation_scope_for_write"]).resolve_automation_scope_for_write,
            return_value="personal",
        ),
    )

    # (a) Cross-user list masks private & unflagged shadowed custom models
    pm_list = await schedule_mod.schedule_dalton_automation_helper(
        action="list", user_id=PM, workspace_id=WS
    )
    by_name = {item["name"]: item["llm_model"] for item in pm_list["automations"]}
    assert by_name["dalton-nvda-watch"] == "所有者自定义模型"  # shadowed, system_only not set
    assert by_name["dalton-nvda-delta"] == "所有者自定义模型"  # private custom model
    assert by_name["dalton-nvda-system"] == "gpt-6.2-sol"  # unshadowed system model
    assert by_name["dalton-nvda-default"] is None

    # Owner list sees exact private names
    owner_list = await schedule_mod.schedule_dalton_automation_helper(
        action="list", user_id=OWNER, workspace_id=WS
    )
    owner_by_name = {item["name"]: item["llm_model"] for item in owner_list["automations"]}
    assert owner_by_name["dalton-nvda-watch"] == "claude-sonnet-4-6"
    assert owner_by_name["dalton-nvda-delta"] == "my-secret-private-model"

    # (b) PM trying to set private custom model, custom provider, or unknown model is refused;
    #     the allowed-models list reveals NO custom models of any user
    for bad in ("my-secret-private-model", "my-secret-gw", "totally-unknown"):
        refused = await schedule_mod.schedule_dalton_automation_helper(
            "dalton-nvda-watch",
            {"llm_model": bad},
            user_id=PM,
            workspace_id=WS,
        )
        assert refused["ok"] is False
        assert refused["error"] == "invalid_llm_model"
        assert 'pass "" to clear' in refused["message"]
        assert "null" not in refused["message"].lower()
        allowed_part = refused["message"].split("one of:", 1)[-1]
        assert "my-secret-private-model" not in allowed_part
        assert "my-secret-gw" not in allowed_part
        assert "claude-sonnet-4-6" in allowed_part
        if bad == "totally-unknown":
            assert "my-secret-private-model" not in refused["message"]
            assert "my-secret-gw" not in refused["message"]
    check_model_spy.assert_not_awaited()
    custom_prov_spy.assert_not_awaited()

    # (c) PM explicitly saves SAME name ("claude-sonnet-4-6") + reasoning_effort="high":
    #     stamps llm_model_system_only=True AND validates reasoning_effort using SYSTEM ladder (even though owner's shadow has no efforts)
    res_sys = await schedule_mod.schedule_dalton_automation_helper(
        "dalton-nvda-watch",
        {
            "llm_model": "claude-sonnet-4-6",
            "reasoning_effort": "high",
            "metadata": {"llm_model_system_only": False},  # caller forgery attempt stripped
        },
        user_id=PM,
        workspace_id=WS,
    )
    assert res_sys["ok"] is True
    assert captured_updates[-1]["llm_model"] == "claude-sonnet-4-6"
    assert captured_updates[-1]["reasoning_effort"] == "high"
    assert captured_updates[-1]["metadata"]["llm_model_system_only"] is True

    # Now cross-user list shows "claude-sonnet-4-6" because llm_model_system_only is True!
    pm_list_after = await schedule_mod.schedule_dalton_automation_helper(
        action="list", user_id=PM, workspace_id=WS
    )
    by_name_after = {item["name"]: item["llm_model"] for item in pm_list_after["automations"]}
    assert by_name_after["dalton-nvda-watch"] == "claude-sonnet-4-6"

    # (d) Cross-user edit of ONLY reasoning_effort on a private custom model hides private name in error
    refused_eff = await schedule_mod.schedule_dalton_automation_helper(
        "dalton-nvda-delta",
        {"reasoning_effort": "high"},
        user_id=PM,
        workspace_id=WS,
    )
    assert refused_eff["ok"] is False
    assert refused_eff["error"] == "invalid_reasoning_effort"
    assert "所有者自定义模型" in refused_eff["message"]
    assert "my-secret-private-model" not in refused_eff["message"]

    # (e) PM clears llm_model -> removes llm_model_system_only and sets llm_model=None
    res_clear = await schedule_mod.schedule_dalton_automation_helper(
        "dalton-nvda-watch",
        {"llm_model": "default"},
        user_id=PM,
        workspace_id=WS,
    )
    assert res_clear["ok"] is True
    assert captured_updates[-1]["llm_model"] is None
    assert "llm_model_system_only" not in captured_updates[-1]["metadata"]


# ---------------------------------------------------------------------------
# 4. In-transaction lock ordering, Home single-determination placement, and
#    dalton-earnings-calendar owner exception (codex_r3 #4, C6, C18, C19)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_home_placement_determined_once_in_tx_and_earnings_calendar_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_catalog_and_prefs(monkeypatch, owner_pref={})
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: False)
    monkeypatch.setattr("src.dalton.settings.is_dalton_auth_enabled", lambda: True)
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)
    # OWNER is a regular internal member (NOT PM)
    monkeypatch.setattr("src.dalton.autonomy.system_jobs.is_pm_user", lambda uid: uid == PM)
    monkeypatch.setattr(
        schedule_mod,
        "resolve_caller_home_workspace_id",
        create_autospec(schedule_mod.resolve_caller_home_workspace_id, return_value=HOME_WS),
    )

    executed_sql: list[str] = []
    conn = create_autospec(AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor(*_a: Any, **_kw: Any):
        cur = create_autospec(AsyncCursor, instance=True)
        last: dict[str, Any] = {"rows": []}

        async def _exec(sql: str, params: Any = None) -> None:
            norm = " ".join(sql.split())
            executed_sql.append(norm)
            if "FROM computers" in norm:
                last["rows"] = [{"computer_id": COMP_1, "user_id": OWNER, "kind": "docker", "status": "running", "name": "Primary"}]
            elif "FROM workspaces" in norm:
                last["rows"] = [{"workspace_id": HOME_WS, "user_id": OWNER, "computer_id": COMP_1, "status": "running", "config": {}}]
            else:
                last["rows"] = []

        cur.execute.side_effect = _exec
        cur.fetchall.side_effect = lambda: list(last["rows"])
        cur.fetchone.side_effect = lambda: last["rows"][0] if last["rows"] else None
        yield cur

    conn.cursor.side_effect = _cursor

    @asynccontextmanager
    async def _lock(uid: str, *, conn: Any = None, prelock_ws=None):
        yield conn if conn is not None else lock_conn_ref

    lock_conn_ref = conn
    monkeypatch.setattr(schedule_mod, "_user_automations_lock_for_write", _lock)

    # Simulate gateway flipping ssh_tunnels if home_turn_computer were called a 2nd time in tx:
    # Call 1 (pre-lock snapshot) -> COMP_1
    # Call 2 (in-tx single determination) -> COMP_1
    # Call 3 (MUST NOT HAPPEN!) -> COMP_2
    home_calls = 0

    async def _home_turn(uid: str, *, conn: Any = None):
        nonlocal home_calls
        home_calls += 1
        if home_calls <= 2:
            return {"computer_id": COMP_1, "kind": "docker", "name": "Primary"}, False
        return {"computer_id": COMP_2, "kind": "ssh", "name": "Flipped"}, True

    monkeypatch.setattr(
        home_placement_mod,
        "home_turn_computer",
        create_autospec(home_placement_mod.home_turn_computer, side_effect=_home_turn),
    )

    seen_resolved_computers: list[Any] = []

    async def _spy_cli_placement(
        user_id: str,
        workspace_id: str | None,
        *,
        conn=None,
        token: bool = False,
        resolved_computer: Any = hosted_mod._UNSET_COMPUTER,
    ):
        seen_resolved_computers.append(resolved_computer)
        return hosted_mod.OnOwn(dict(resolved_computer))

    monkeypatch.setattr(hosted_mod, "checks_automations", create_autospec(hosted_mod.checks_automations, return_value=True))
    monkeypatch.setattr(hosted_mod, "cli_placement", create_autospec(hosted_mod.cli_placement, side_effect=_spy_cli_placement))
    monkeypatch.setattr(lifecycle_mod, "is_claude_code_model", lambda name: True)
    monkeypatch.setattr(auto_db, "list_all_automations", create_autospec(auto_db.list_all_automations, return_value=[]))
    monkeypatch.setattr(
        auto_db,
        "create_automation",
        create_autospec(auto_db.create_automation, return_value=_make_auto_row(name="dalton-earnings-calendar", workspace_id=UUID(HOME_WS))),
    )

    # 1. Non-PM owner CAN save llm_model on own dalton-earnings-calendar
    res_cal = await schedule_mod.schedule_dalton_automation_helper(
        "dalton-earnings-calendar",
        {
            "trigger_type": "cron",
            "cron_expression": "0 9 * * 6",
            "instruction": "Refresh earnings calendar.",
            "llm_model": "claude-sonnet-4-6",
        },
        user_id=OWNER,
        workspace_id="home",
        role="chief_of_staff",
    )
    assert res_cal["ok"] is True
    # Exact lock order: computers ORDER BY computer_id FOR SHARE -> workspaces ORDER BY workspace_id FOR SHARE
    comp_idx = next(i for i, s in enumerate(executed_sql) if "FROM computers" in s and "ORDER BY computer_id FOR SHARE" in s)
    ws_idx = next(i for i, s in enumerate(executed_sql) if "FROM workspaces" in s and "ORDER BY workspace_id FOR SHARE" in s)
    assert comp_idx < ws_idx
    # home_turn_computer called ONCE pre-lock + ONCE in-tx (NOT a 3rd time inside check_claude_code_hosted!)
    assert home_calls == 2
    assert len(seen_resolved_computers) == 1
    assert seen_resolved_computers[0]["computer_id"] == COMP_1

    # 2. Non-PM owner trying to modify another Home global job -> forbidden_pm
    res_daily = await schedule_mod.schedule_dalton_automation_helper(
        "dalton-director-daily",
        {
            "trigger_type": "cron",
            "cron_expression": "30 7 * * 1-5",
            "instruction": "Daily brief.",
            "llm_model": "claude-sonnet-4-6",
        },
        user_id=OWNER,
        workspace_id="home",
        role="chief_of_staff",
    )
    assert res_daily["ok"] is False
    assert res_daily["error"] == "forbidden_pm"


# ---------------------------------------------------------------------------
# 5. End-to-end llm_model_system_only propagation (assertions at resolution entries)
# ---------------------------------------------------------------------------


def test_client_body_strips_forged_llm_model_system_only_to_bool_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    req = ChatRequest(
        workspace_id=WS,
        messages=[ChatMessage(role="user", content="hi")],
        llm_model="claude-sonnet-4-6",
        llm_model_system_only=True,
    )
    raw_req = create_autospec(Request, instance=True)

    orig_is_service = messaging_mod._is_service_request
    monkeypatch.setattr(messaging_mod, "_is_service_request", create_autospec(orig_is_service, return_value=False))
    cleaned = messaging_mod._client_body(req, raw_req)
    assert cleaned.llm_model_system_only is False

    monkeypatch.setattr(messaging_mod, "_is_service_request", create_autospec(orig_is_service, return_value=True))
    kept = messaging_mod._client_body(req, raw_req)
    assert kept.llm_model_system_only is True


@pytest.mark.asyncio
async def test_executor_stamps_system_only_and_classifies_antigravity_via_system_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Owner shadows built-in Antigravity model with a custom non-Antigravity entry
    owner_pref = {
        "custom_models": [
            {"name": "gemini-4-neon-high-oauth", "provider": "my-proxy", "model_id": "proxy-gem"}
        ]
    }
    _install_catalog_and_prefs(monkeypatch, owner_pref=owner_pref)

    captured: list[dict[str, Any]] = []

    async def _fake_astream(**kw: Any):
        captured.append(kw)
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
        create_autospec(TurnIdentity.of, return_value=SimpleNamespace(recorded_as=OWNER)),
    )

    executor = exec_mod.AutomationExecutor.__new__(exec_mod.AutomationExecutor)
    executor._note_admission = create_autospec(executor._note_admission, return_value=True)

    firing = SimpleNamespace(
        automation={
            "automation_id": AUTO_ID,
            "user_id": OWNER,
            "name": "dalton-nvda-watch",
            "llm_model": "gemini-4-neon-high-oauth",
            "reasoning_effort": "high",
            "instruction": "Run check.",
            "additional_context": None,
            "thread_strategy": "new",
            # Even if automation metadata was edited after execution creation,
            # _run_turn obeys the immutable execution-level flag on firing.
            "metadata": {},
        },
        workspace_id=WS,
        thread_id=THREAD_ID,
        route=TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
        late=None,
        held=False,
        delivery=None,
        wake_context=None,
        workspace_owner_id=OWNER,
        checked_computer_id=None,
        run_id=None,
        admitted=False,
        llm_model_system_only=True,
    )

    ok = await executor._run_turn(EXEC_ID, firing, has_byok=False, deadline=9999999999.0)
    assert ok is True
    call_kw = captured[-1]
    assert call_kw["request"].llm_model_system_only is True
    assert "llm_model_system_only" not in call_kw["run_metadata"]
    # Because ignore_custom_models=True saw built-in Antigravity, stale effort "high" was cleared to None
    assert call_kw["request"].reasoning_effort is None


@pytest.mark.asyncio
async def test_first_turn_resolve_ptc_turn_llm_config_ignores_owner_shadowed_custom_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assertion hits real resolve_llm_config via resolve_ptc_turn_llm_config."""
    owner_pref = {
        "preferred_model": "claude-sonnet-4-6",
        "custom_models": [
            {
                "name": "claude-sonnet-4-6",
                "provider": "private-gw",
                "model_id": "evil-endpoint-sonnet",
            }
        ],
        "custom_providers": [
            {"name": "private-gw", "parent_provider": "openai", "base_url": "https://private.invalid/v1"}
        ],
    }
    monkeypatch.setattr(
        user_models,
        "get_model_preference",
        create_autospec(user_models.get_model_preference, return_value=dict(owner_pref)),
    )
    monkeypatch.setattr(
        llm_config_mod,
        "require_policy",
        create_autospec(
            llm_config_mod.require_policy,
            return_value=SimpleNamespace(byok=lambda v: v, models_allowed=None, models_denied=()),
        ),
    )
    monkeypatch.setattr(llm_config_mod, "builtins_only", lambda _p, pref: pref)
    monkeypatch.setattr(llm_config_mod, "drop_dead_models", lambda pref, _keys: (pref, []))
    monkeypatch.setattr(llm_config_mod, "drop_claude_code_roles", lambda pref, _keys: (pref, []))
    monkeypatch.setattr(llm_config_mod, "gate_models_by_policy", lambda _u, pref, _m, **_kw: pref)
    monkeypatch.setattr(
        llm_config_mod,
        "gate_models_by_plan",
        create_autospec(llm_config_mod.gate_models_by_plan, side_effect=lambda _b, _u, pref, *_a, **_kw: pref),
    )
    monkeypatch.setattr(
        llm_config_mod,
        "load_user_context",
        create_autospec(llm_config_mod.load_user_context, return_value=None),
    )
    monkeypatch.setattr(
        llm_config_mod,
        "default_to_first_allowed",
        create_autospec(llm_config_mod.default_to_first_allowed, return_value=None),
    )
    monkeypatch.setattr(llm_config_mod, "apply_policy_to_roles", lambda *_a, **_kw: None)
    monkeypatch.setattr(llm_config_mod, "apply_compaction", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        llm_config_mod,
        "gate_capabilities",
        create_autospec(llm_config_mod.gate_capabilities, return_value=None),
    )
    monkeypatch.setattr(
        llm_config_mod,
        "read_user_subagents",
        create_autospec(llm_config_mod.read_user_subagents, return_value=([], {}, {})),
    )
    monkeypatch.setattr(
        llm_config_mod,
        "resolve_user_subagents",
        create_autospec(llm_config_mod.resolve_user_subagents, return_value={}),
    )
    monkeypatch.setattr(
        llm_config_mod,
        "check_model_available",
        create_autospec(llm_config_mod.check_model_available, return_value=False),
    )
    monkeypatch.setattr(llm_config_mod, "subagent_roles", lambda *_a, **_kw: [])
    monkeypatch.setattr(llm_config_mod, "demote_clientless", lambda *_a, **_kw: None)
    monkeypatch.setattr(llm_config_mod, "subagent_models", lambda *_a, **_kw: {})
    monkeypatch.setattr(llm_config_mod, "TaskModelGate", lambda *_a, **_kw: None)

    seen_resolve_clients: list[dict[str, Any]] = []

    async def _capture_clients(config, uid, effective_model, model_pref, roles, **kw):
        seen_resolve_clients.append(
            {
                "effective_model": effective_model,
                "has_custom_models": "custom_models" in model_pref,
                "has_custom_providers": "custom_providers" in model_pref,
                "ignore_custom_models": kw.get("ignore_custom_models", False),
            }
        )
        config.llm_client = object()

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_clients",
        create_autospec(llm_config_mod.resolve_clients, side_effect=_capture_clients),
    )

    class _FakeBaseConfig:
        def __init__(self) -> None:
            self.llm = SimpleNamespace(
                name="claude-sonnet-4-6",
                background=None,
                compaction=None,
                fetch=None,
                fallback=[],
                fallbacks_for=lambda _x: [],
            )
            self.llm_client = None
            self.claude_code = False
            self.claude_code_pins = None
            self.search_api = "none"
            self.search_depth = "basic"
            self.subagents = SimpleNamespace(resolved={})

        def model_copy(self, deep: bool = True) -> "_FakeBaseConfig":
            return _FakeBaseConfig()

    req_sys = ChatRequest(
        workspace_id=WS,
        messages=[ChatMessage(role="user", content="go")],
        llm_model="claude-sonnet-4-6",
        llm_model_system_only=True,
        origin=ThreadOrigin(type="automation", id=AUTO_ID),
    )
    await fb_mod.resolve_ptc_turn_llm_config(
        _FakeBaseConfig(),
        OWNER,
        req_sys,
        is_byok=False,
        thread_id=THREAD_ID,
        workspace_id=WS,
        identity=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        run_metadata={"automation_execution_id": EXEC_ID},
    )
    assert seen_resolve_clients[-1] == {
        "effective_model": "claude-sonnet-4-6",
        "has_custom_models": False,
        "has_custom_providers": False,
        "ignore_custom_models": True,
    }

    # Contrast: without llm_model_system_only, owner's custom_models remain visible
    req_normal = ChatRequest(
        workspace_id=WS,
        messages=[ChatMessage(role="user", content="go")],
        llm_model="claude-sonnet-4-6",
        llm_model_system_only=False,
        origin=ThreadOrigin(type="automation", id=AUTO_ID),
    )
    await fb_mod.resolve_ptc_turn_llm_config(
        _FakeBaseConfig(),
        OWNER,
        req_normal,
        is_byok=False,
        thread_id=THREAD_ID,
        workspace_id=WS,
        identity=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        run_metadata={"automation_execution_id": EXEC_ID},
    )
    assert seen_resolve_clients[-1]["has_custom_models"] is True
    assert seen_resolve_clients[-1]["ignore_custom_models"] is False


@pytest.mark.asyncio
async def test_subscription_fallback_candidate_screening_uses_system_catalog_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex_r3 #3: candidate screening & subscription provider check must ignore owner custom shadow."""
    owner_pref = {
        "fallback_models": ["owner-private-only", "gpt-6.2-sol"],
        "custom_models": [
            {
                "name": "owner-private-only",
                "provider": "openai",
                "model_id": "priv-1",
            },
            {
                # Owner shadows system model gpt-6.2-sol onto the FAILED subscription provider!
                "name": "gpt-6.2-sol",
                "provider": "claude-oauth",
                "model_id": "shadowed-on-failed-sub",
            },
        ],
    }
    _install_catalog_and_prefs(monkeypatch, owner_pref=owner_pref)

    # Stub subscription_route so CUSTOM on "claude-oauth" maps to "claude-oauth",
    # whereas SYSTEM entry for "gpt-6.2-sol" (provider="openai") maps to None (not failed_provider).
    def _fake_sub_route(model_name: str, mc: Any, *, custom_entry: Any = None):
        if isinstance(custom_entry, dict) and custom_entry.get("provider") == "claude-oauth":
            return SimpleNamespace(subscription=SimpleNamespace(id="claude-oauth"))
        return None

    monkeypatch.setattr(fb_mod, "subscription_route", create_autospec(fb_mod.subscription_route, side_effect=_fake_sub_route))
    # Stub manifest lookup inside avail_mod._resolver so only system catalog models exist in manifest
    fake_mc = SimpleNamespace(
        llm_config=_SYSTEM_CATALOG,
        get_model_config=lambda m: _SYSTEM_CATALOG.get(m),
    )
    monkeypatch.setattr("src.llms.llm.LLM.get_model_config", lambda: fake_mc)

    attempted: list[tuple[str | None, bool]] = []

    async def _fake_resolve_llm(_base, _uid, model_name, _byok, **kw):
        attempted.append((model_name, bool(kw.get("ignore_custom_models", False))))
        if model_name == "claude-sonnet-4-6":
            raise HTTPException(
                status_code=400,
                detail={"type": "oauth_reauth_required", "provider": "claude-oauth"},
            )
        return SimpleNamespace(llm=SimpleNamespace(name=model_name), llm_client=object())

    monkeypatch.setattr(
        fb_mod,
        "resolve_llm_config",
        create_autospec(fb_mod.resolve_llm_config, side_effect=_fake_resolve_llm),
    )

    cfg, record = await fb_mod.resolve_automation_turn_llm_config(
        SimpleNamespace(),
        OWNER,
        "claude-sonnet-4-6",
        False,
        ignore_custom_models=True,
    )
    # owner-private-only was filtered out by model_resolves(stripped_prefs) without ever calling resolve_llm_config;
    # gpt-6.2-sol was NOT falsely skipped by _subscription_provider_for_model despite owner's shadow on claude-oauth!
    assert attempted == [
        ("claude-sonnet-4-6", True),
        ("gpt-6.2-sol", True),
    ]
    assert cfg.llm.name == "gpt-6.2-sol"
    assert record is not None and record["fallback_model"] == "gpt-6.2-sol"


@pytest.mark.asyncio
async def test_continuation_turn_inherits_system_only_into_thread_resolver_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assertion at continuation turn resolution entry: thread_resolver -> resolve_llm_config + fallback."""
    exec_row = {
        "automation_execution_id": EXEC_ID,
        "automation_id": AUTO_ID,
        "status": "running",
        "conversation_thread_id": THREAD_ID,
        "user_id": OWNER,
        "llm_model_system_only": True,
    }
    monkeypatch.setattr(
        atm_mod,
        "_get_wakeup_creator_run_ids",
        create_autospec(atm_mod._get_wakeup_creator_run_ids, return_value=[RUN_ID]),
    )
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
            return_value=("claude-sonnet-4-6", True, "high"),
        ),
    )
    monkeypatch.setattr(
        atm_mod.user_models,
        "get_model_preference",
        create_autospec(user_models.get_model_preference, return_value={"fallback_models": ["gpt-6.2-sol"]}),
    )
    monkeypatch.setattr(
        atm_mod,
        "model_resolves",
        create_autospec(atm_mod.model_resolves, return_value=True),
    )
    monkeypatch.setattr(
        atm_mod.thread_model,
        "model_resolves",
        create_autospec(avail_mod.model_resolves, return_value=True),
    )

    resolve_calls: list[dict[str, Any]] = []

    async def _fake_resolve_llm(_base, _uid, model_name, _byok, **kw):
        resolve_calls.append(
            {
                "model": model_name,
                "effort": kw.get("reasoning_effort"),
                "ignore_custom_models": kw.get("ignore_custom_models"),
            }
        )
        return SimpleNamespace(llm=SimpleNamespace(name=model_name), llm_client=object())

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, side_effect=_fake_resolve_llm),
    )
    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())

    resolver = thread_agent.thread_resolver(
        OWNER,
        THREAD_ID,
        TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
        identity=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        thread={"reasoning_effort": "low"},
        is_byok=False,
        ignore_custom_models=False,
    )
    req = ChatRequest(
        workspace_id=WS,
        messages=[ChatMessage(role="user", content="wakeup")],
        query_type="wakeup",
        wakeup={"notices": [{"wakeup_id": WAKEUP_ID}]},
        llm_model_system_only=False,
    )
    ctx_meta: dict[str, Any] = {}
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=req,
        requested_model=None,
        held_model="claude-sonnet-4-6",
        from_service=True,
        resolve=resolver,
        on_automation_context=ctx_meta.update,
    )
    assert resolve_calls == [
        {
            "model": "claude-sonnet-4-6",
            "effort": "high",
            "ignore_custom_models": True,
        }
    ]
    assert "llm_model_system_only" not in ctx_meta
    assert ctx_meta["automation_source_execution_id"] == EXEC_ID


@pytest.mark.asyncio
async def test_thread_maintenance_resolver_forwards_system_only_for_compaction_and_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """thread_maintenance._resolver reads execution-level llm_model_system_only for the latest run."""
    monkeypatch.setattr(
        "src.server.services.account.own_access",
        create_autospec(
            __import__("src.server.services.account", fromlist=["own_access"]).own_access,
            return_value=SimpleNamespace(api_keys=False),
        ),
    )
    monkeypatch.setattr(
        maint_mod.TurnIdentity,
        "of",
        create_autospec(TurnIdentity.of, return_value=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER)),
    )
    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())

    seen_flags: list[bool] = []

    async def _fake_resolve_llm(_base, _uid, model_name, _byok, **kw):
        seen_flags.append(bool(kw.get("ignore_custom_models", False)))
        return SimpleNamespace(llm=SimpleNamespace(name=model_name), llm_client=object())

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, side_effect=_fake_resolve_llm),
    )

    route = TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst")
    sys_run_id = "11111111-1111-4111-8111-111111111101"
    norm_run_id = "22222222-2222-4222-8222-222222222202"
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(
            atm_mod._get_execution_for_run_id,
            side_effect=lambda rid: (
                {"automation_execution_id": EXEC_ID, "llm_model_system_only": True}
                if str(rid) == sys_run_id
                else None
            ),
        ),
    )

    orig_latest = runs_lifecycle_db.get_latest_attempt
    # 1. Latest attempt belongs to a system-only execution -> ignore_custom_models=True
    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(
            orig_latest,
            return_value={
                "conversation_response_id": sys_run_id,
                "metadata": {"subagents_enabled": ["research"]},
            },
        ),
    )
    resolve_sys = await maint_mod._resolver(OWNER, THREAD_ID, {"llm_model": "claude-sonnet-4-6"}, route)
    await resolve_sys("claude-sonnet-4-6")
    assert seen_flags[-1] is True

    # 2. Normal thread run without an automation execution -> ignore_custom_models=False
    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(
            orig_latest,
            return_value={
                "conversation_response_id": norm_run_id,
                "metadata": {"subagents_enabled": ["research"]},
            },
        ),
    )
    resolve_norm = await maint_mod._resolver(OWNER, THREAD_ID, {"llm_model": "claude-sonnet-4-6"}, route)
    await resolve_norm("claude-sonnet-4-6")
    assert seen_flags[-1] is False


@pytest.mark.asyncio
async def test_regular_workspace_locks_in_order_rechecks_same_model_claude_code_and_rejects_move_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C6/C18: regular workspace locks computers -> workspaces FOR SHARE, rechecks Claude Code
    even when saving the identical llm_model, and aborts on move_fenced or computer drift."""
    _install_catalog_and_prefs(monkeypatch, owner_pref={})
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: False)
    monkeypatch.setattr("src.dalton.settings.is_dalton_auth_enabled", lambda: False)
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(
        schedule_mod,
        "verify_workspace_access",
        create_autospec(schedule_mod.verify_workspace_access, return_value={"workspace_id": WS, "user_id": OWNER}),
    )
    monkeypatch.setattr(
        computer_db,
        "get_computer_for_workspace",
        create_autospec(computer_db.get_computer_for_workspace, return_value={"computer_id": COMP_1}),
    )

    executed_sql: list[str] = []
    db_state: dict[str, Any] = {"computer_id": COMP_1, "move_fenced": False}
    conn = create_autospec(AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor(*_a: Any, **_kw: Any):
        cur = create_autospec(AsyncCursor, instance=True)
        last: dict[str, Any] = {"row": None}

        async def _exec(sql: str, params: Any = None) -> None:
            norm = " ".join(sql.split())
            executed_sql.append(norm)
            if "FROM computers" in norm:
                last["row"] = {"computer_id": COMP_1, "user_id": OWNER, "kind": "docker", "status": "running", "name": "Primary"}
            elif "FROM workspaces" in norm:
                last["row"] = {
                    "workspace_id": WS,
                    "user_id": OWNER,
                    "computer_id": db_state["computer_id"],
                    "status": "running",
                    "config": {},
                }
            elif "FROM workspace_moves" in norm:
                last["row"] = {"move_fenced": db_state["move_fenced"]}
            else:
                last["row"] = None

        cur.execute.side_effect = _exec
        cur.fetchone.side_effect = lambda: last["row"]
        yield cur

    conn.cursor.side_effect = _cursor

    @asynccontextmanager
    async def _lock(uid: str, *, conn: Any = None, prelock_ws=None):
        yield lock_conn_ref

    lock_conn_ref = conn
    monkeypatch.setattr(schedule_mod, "_user_automations_lock_for_write", _lock)

    existing_row = _make_auto_row(llm_model="claude-sonnet-4-6", reasoning_effort="medium")
    monkeypatch.setattr(
        auto_db,
        "list_all_automations",
        create_autospec(auto_db.list_all_automations, return_value=[dict(existing_row)]),
    )

    async def _fake_update_with_reconciler(aid: str, uid: str, *, conn: Any = None, **kw: Any):
        effective = dict(kw)
        hist_ctx = company_automations.get_current_history_context()
        assert hist_ctx is not None and hist_ctx.locked_row_reconciler is not None
        await hist_ctx.locked_row_reconciler(existing_row, effective, conn)
        return {**existing_row, **effective}

    monkeypatch.setattr(
        auto_db,
        "update_automation",
        create_autospec(auto_db.update_automation, side_effect=_fake_update_with_reconciler),
    )

    cc_calls: list[dict[str, Any]] = []

    async def _spy_cc(uid, model, ws, c=None, pref=None, *, resolved_computer=lifecycle_mod._UNSET_COMPUTER):
        cc_calls.append({"model": model, "ws": ws, "resolved_computer": resolved_computer})

    monkeypatch.setattr(
        lifecycle_mod,
        "check_claude_code_hosted",
        create_autospec(lifecycle_mod.check_claude_code_hosted, side_effect=_spy_cc),
    )

    # 1. Explicitly saving the SAME llm_model triggers locked placement check + check_claude_code_hosted
    res_same = await schedule_mod.schedule_dalton_automation_helper(
        "dalton-nvda-watch",
        {"llm_model": "claude-sonnet-4-6"},
        user_id=OWNER,
        workspace_id=WS,
    )
    assert res_same["ok"] is True
    comp_idx = next(i for i, s in enumerate(executed_sql) if "FROM computers" in s and "ORDER BY computer_id FOR SHARE" in s)
    ws_idx = next(i for i, s in enumerate(executed_sql) if "FROM workspaces" in s and "ORDER BY w.workspace_id FOR SHARE" in s)
    fence_idx = next(i for i, s in enumerate(executed_sql) if "FROM workspace_moves" in s and "AS move_fenced" in s)
    assert comp_idx < ws_idx < fence_idx
    assert "workspace_moves" not in executed_sql[ws_idx]
    assert len(cc_calls) == 1
    assert cc_calls[0]["model"] == "claude-sonnet-4-6"
    assert cc_calls[0]["resolved_computer"]["computer_id"] == COMP_1

    # 2. Active move fence -> refused with workspace_changed
    db_state["move_fenced"] = True
    res_fenced = await schedule_mod.schedule_dalton_automation_helper(
        "dalton-nvda-watch",
        {"llm_model": "claude-sonnet-4-6"},
        user_id=OWNER,
        workspace_id=WS,
    )
    assert res_fenced["ok"] is False
    assert res_fenced["error"] == "workspace_changed"
    assert res_fenced["message"] == "情况已变化，请重新发起"

    # 3. Binding changed to COMP_2 before lock -> refused with workspace_changed
    db_state["move_fenced"] = False
    db_state["computer_id"] = COMP_2
    res_moved = await schedule_mod.schedule_dalton_automation_helper(
        "dalton-nvda-watch",
        {"llm_model": "claude-sonnet-4-6"},
        user_id=OWNER,
        workspace_id=WS,
    )
    assert res_moved["ok"] is False
    assert res_moved["error"] == "workspace_changed"
    assert res_moved["message"] == "情况已变化，请重新发起"


@pytest.mark.asyncio
async def test_continuation_turn_subscription_fallback_forwards_ignore_custom_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continuation turn subscription fallback must forward ignore_custom_models=True."""
    exec_row = {
        "automation_execution_id": EXEC_ID,
        "automation_id": AUTO_ID,
        "status": "running",
        "conversation_thread_id": THREAD_ID,
        "user_id": OWNER,
        "llm_model_system_only": True,
    }
    owner_pref = {
        "fallback_models": ["owner-private-only", "gpt-6.2-sol"],
        "custom_models": [
            {"name": "owner-private-only", "provider": "openai", "model_id": "priv-1"},
            {"name": "gpt-6.2-sol", "provider": "claude-oauth", "model_id": "shadow"},
        ],
    }
    _install_catalog_and_prefs(monkeypatch, owner_pref=owner_pref)
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
            return_value=("claude-sonnet-4-6", True, "medium"),
        ),
    )
    fake_mc = SimpleNamespace(
        llm_config=_SYSTEM_CATALOG,
        get_model_config=lambda m: _SYSTEM_CATALOG.get(m),
    )
    monkeypatch.setattr("src.llms.llm.LLM.get_model_config", lambda: fake_mc)
    monkeypatch.setattr(
        fb_mod,
        "subscription_route",
        create_autospec(
            fb_mod.subscription_route,
            side_effect=lambda m, mc, *, custom_entry=None: (
                SimpleNamespace(subscription=SimpleNamespace(id="claude-oauth"))
                if isinstance(custom_entry, dict) and custom_entry.get("provider") == "claude-oauth"
                else None
            ),
        ),
    )

    resolve_calls: list[tuple[str | None, bool]] = []

    async def _fake_resolve_llm(_base, _uid, model_name, _byok, **kw):
        resolve_calls.append((model_name, bool(kw.get("ignore_custom_models", False))))
        if model_name == "claude-sonnet-4-6":
            raise HTTPException(
                status_code=400,
                detail={"type": "oauth_reauth_required", "provider": "claude-oauth"},
            )
        return SimpleNamespace(llm=SimpleNamespace(name=model_name), llm_client=object())

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, side_effect=_fake_resolve_llm),
    )
    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())

    resolver = thread_agent.thread_resolver(
        OWNER,
        THREAD_ID,
        TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
        identity=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        thread={},
        is_byok=False,
    )
    req = ChatRequest(
        workspace_id=WS,
        messages=[ChatMessage(role="user", content="done")],
        query_type="system",
        automation_delivery={"id": EXEC_ID, "targets": []},
    )
    cfg = await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=req,
        requested_model=None,
        held_model=None,
        from_service=True,
        resolve=resolver,
    )
    assert cfg.llm.name == "gpt-6.2-sol"
    assert resolve_calls == [
        ("claude-sonnet-4-6", True),
        ("gpt-6.2-sol", True),
    ]



# ---------------------------------------------------------------------------
# Round 2 tests: Decisions 1, 2, 3, 5, 6, 7
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_only_preserved_on_web_retry_hitl_resume_and_running_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execution-level llm_model_system_only applies across web retry (including with both
    llm_model and reasoning_effort), HITL resume, and running_thread server continuations,
    reading from automation_executions.llm_model_system_only without stamping run_meta."""
    from src.server.services.llm import thread_model as thread_model_mod

    exec_row = {
        "automation_execution_id": EXEC_ID,
        "automation_id": AUTO_ID,
        "status": "running",
        "conversation_thread_id": THREAD_ID,
        "user_id": OWNER,
        "llm_model": "claude-sonnet-4-6",
        "reasoning_effort": "medium",
        "llm_model_system_only": True,
    }
    _install_catalog_and_prefs(monkeypatch, owner_pref={})
    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())
    monkeypatch.setattr(
        thread_model_mod,
        "model_resolves",
        create_autospec(thread_model_mod.model_resolves, return_value=True),
    )
    monkeypatch.setattr(
        atm_mod,
        "model_resolves",
        create_autospec(atm_mod.model_resolves, return_value=True),
    )

    resolve_calls: list[tuple[str | None, bool, str | None]] = []

    async def _fake_resolve_llm(_base, _uid, model_name, _byok, **kw):
        resolve_calls.append(
            (model_name, bool(kw.get("ignore_custom_models", False)), kw.get("reasoning_effort"))
        )
        return SimpleNamespace(llm=SimpleNamespace(name=model_name), llm_client=object())

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, side_effect=_fake_resolve_llm),
    )

    def _make_resolver(effort: str | None = None):
        return thread_agent.thread_resolver(
            OWNER,
            THREAD_ID,
            TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
            identity=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
            thread={"llm_model": "claude-sonnet-4-6"},
            is_byok=False,
            reasoning_effort=effort,
        )

    # (a) User web retry sending BOTH llm_model and reasoning_effort reads execution column
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
            return_value=("claude-sonnet-4-6", True, "medium"),
        ),
    )
    run_meta_retry: dict[str, Any] = {}
    req_web_retry = ChatRequest(
        workspace_id=WS,
        messages=[],
        llm_model="claude-sonnet-4-6",
        reasoning_effort="high",
    )
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=req_web_retry,
        requested_model="claude-sonnet-4-6",
        held_model="claude-sonnet-4-6",
        from_service=False,
        is_server_retry=False,
        retry_of_run_id="99999999-9999-4999-8999-999999999999",
        resolve=_make_resolver(effort="high"),
        on_automation_context=run_meta_retry.update,
    )
    assert "llm_model_system_only" not in run_meta_retry
    assert run_meta_retry.get("automation_source_execution_id") == EXEC_ID
    # Round 8 Decision 1: web /retry ignores composer's reasoning_effort="high" and uses execution snapshot "medium"
    assert resolve_calls[-1] == ("claude-sonnet-4-6", True, "medium")

    # (b) User /retry with only reasoning_effort (no llm_model)
    run_meta_effort_retry: dict[str, Any] = {}
    req_effort_retry = ChatRequest(
        workspace_id=WS,
        messages=[],
        reasoning_effort="low",
    )
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=req_effort_retry,
        requested_model=None,
        held_model="claude-sonnet-4-6",
        from_service=False,
        is_server_retry=False,
        retry_of_run_id="99999999-9999-4999-8999-999999999999",
        resolve=_make_resolver(effort="low"),
        on_automation_context=run_meta_effort_retry.update,
    )
    assert "llm_model_system_only" not in run_meta_effort_retry
    assert run_meta_effort_retry.get("automation_source_execution_id") == EXEC_ID
    # Round 8 Decision 1: web /retry ignores composer's reasoning_effort="low" and uses execution snapshot "medium"
    assert resolve_calls[-1] == ("claude-sonnet-4-6", True, "medium")

    # (c) HITL resume submitted from web (user request with hitl_response + llm_model + reasoning_effort)
    monkeypatch.setattr(
        atm_mod,
        "_get_hitl_interrupt_run_id",
        create_autospec(
            atm_mod._get_hitl_interrupt_run_id,
            return_value="88888888-8888-4888-8888-888888888888",
        ),
    )
    run_meta_hitl: dict[str, Any] = {}
    req_hitl = ChatRequest(
        workspace_id=WS,
        messages=[],
        hitl_response={"interrupt-1": {"decisions": [{"type": "approve"}]}},
        llm_model="claude-sonnet-4-6",
        reasoning_effort="medium",
    )
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=req_hitl,
        requested_model="claude-sonnet-4-6",
        held_model="claude-sonnet-4-6",
        from_service=False,
        resolve=_make_resolver(effort="medium"),
        on_automation_context=run_meta_hitl.update,
    )
    assert "llm_model_system_only" not in run_meta_hitl
    assert run_meta_hitl.get("automation_source_execution_id") == EXEC_ID
    assert resolve_calls[-1] == ("claude-sonnet-4-6", True, "medium")

    # (d) Server continuation matching ONLY running_thread fallback reads execution column
    #     and stamps automation_source_execution_id (never llm_model_system_only) to run_meta
    monkeypatch.setattr(
        atm_mod.exec_db,
        "get_running_executions_for_thread",
        create_autospec(atm_mod.exec_db.get_running_executions_for_thread, return_value=[exec_row]),
    )
    run_meta_running: dict[str, Any] = {}
    req_running = ChatRequest(
        workspace_id=WS,
        messages=[ChatMessage(role="user", content="resume")],
        query_type="system",
    )
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=req_running,
        requested_model=None,
        held_model=None,
        from_service=True,
        resolve=_make_resolver(),
        on_automation_context=run_meta_running.update,
    )
    assert "llm_model_system_only" not in run_meta_running
    assert run_meta_running.get("automation_source_execution_id") == EXEC_ID
    assert resolve_calls[-1][0] == "claude-sonnet-4-6"
    assert resolve_calls[-1][1] is True


@pytest.mark.asyncio
async def test_generic_patch_maintains_system_only_marker_and_effort_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 2: generic PATCH clears llm_model_system_only on llm_model=None or custom model,
    rejects client metadata writes to llm_model_system_only, and validates effort-only edits
    against the system catalog when llm_model_system_only is True."""
    from src.server.models.automation import AutomationCreate

    owner_pref = {
        "custom_models": [
            {
                "name": "claude-sonnet-4-6",
                "provider": "openai",
                "model_id": "owner-shadow-no-reasoning",
            },
            {
                "name": "owner-custom-only",
                "provider": "openai",
                "model_id": "owner-custom-id",
                "reasoning": {"efforts": ["low"], "default": "low"},
            },
        ],
    }
    _install_catalog_and_prefs(monkeypatch, owner_pref=owner_pref)
    monkeypatch.setattr(
        lifecycle_mod,
        "check_claude_code_hosted",
        create_autospec(lifecycle_mod.check_claude_code_hosted, return_value=None),
    )

    stored_sys_row = {
        "automation_id": AUTO_ID,
        "user_id": OWNER,
        "name": "dalton-nvda-watch",
        "trigger_type": "cron",
        "cron_expression": "0 9 * * 1-5",
        "timezone": "Asia/Shanghai",
        "status": "active",
        "workspace_id": WS,
        "llm_model": "claude-sonnet-4-6",
        "reasoning_effort": "medium",
        "metadata": {"dalton_managed": True, "llm_model_system_only": True},
    }

    # (a) Clearing llm_model=None removes llm_model_system_only in both prepare & locked recheck
    prep_clear = await lifecycle_mod.prepare_owned_automation_update(
        AUTO_ID, OWNER, {"llm_model": None}, current=dict(stored_sys_row), model_pref=owner_pref
    )
    assert prep_clear is not None
    assert prep_clear.update_kwargs["llm_model"] is None
    assert "llm_model_system_only" not in prep_clear.update_kwargs["metadata"]
    assert prep_clear.update_kwargs["metadata"]["dalton_managed"] is True

    eff_clear = dict(prep_clear.update_kwargs)
    await lifecycle_mod.recheck_locked_automation_update(
        user_id=OWNER,
        update=prep_clear.update,
        current=stored_sys_row,
        stored_in=WS,
        stored_pin=None,
        new_cron=None,
        new_tz=None,
        pref_for_lock=owner_pref,
        locked_row=dict(stored_sys_row),
        effective_kwargs=eff_clear,
        locked_conn=None,
    )
    assert "llm_model_system_only" not in eff_clear["metadata"]

    # (b) Switching explicitly to a custom model removes llm_model_system_only
    prep_custom = await lifecycle_mod.prepare_owned_automation_update(
        AUTO_ID,
        OWNER,
        {"llm_model": "owner-custom-only"},
        current=dict(stored_sys_row),
        model_pref=owner_pref,
    )
    assert prep_custom is not None
    assert prep_custom.update_kwargs["llm_model"] == "owner-custom-only"
    assert "llm_model_system_only" not in prep_custom.update_kwargs["metadata"]

    # (c) Client metadata cannot write llm_model_system_only on PATCH or create
    normal_row = {**stored_sys_row, "metadata": {"dalton_managed": True}}
    prep_forge = await lifecycle_mod.prepare_owned_automation_update(
        AUTO_ID,
        OWNER,
        {"metadata": {"dalton_managed": True, "llm_model_system_only": True}},
        current=normal_row,
        model_pref=owner_pref,
    )
    assert prep_forge is not None
    assert "llm_model_system_only" not in prep_forge.update_kwargs["metadata"]

    # Client metadata on a system-only row without changing llm_model preserves the server flag
    prep_keep = await lifecycle_mod.prepare_owned_automation_update(
        AUTO_ID,
        OWNER,
        {"metadata": {"dalton_managed": True, "note": "keep", "llm_model_system_only": False}},
        current=dict(stored_sys_row),
        model_pref=owner_pref,
    )
    assert prep_keep is not None
    assert prep_keep.update_kwargs["metadata"]["llm_model_system_only"] is True
    assert prep_keep.update_kwargs["metadata"]["note"] == "keep"

    # Generic create_automation also strips client-forged llm_model_system_only
    created_meta_capture: list[Any] = []
    monkeypatch.setattr(
        lifecycle_mod,
        "_placement",
        create_autospec(lifecycle_mod._placement, return_value=WS),
    )
    monkeypatch.setattr(
        lifecycle_mod,
        "require_target_ownership",
        create_autospec(lifecycle_mod.require_target_ownership, return_value=None),
    )
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: False)
    monkeypatch.setattr(
        "src.dalton.database.company_automations.resolve_automation_scope_for_write",
        create_autospec(
            __import__(
                "src.dalton.database.company_automations",
                fromlist=["resolve_automation_scope_for_write"],
            ).resolve_automation_scope_for_write,
            return_value="personal",
        ),
    )

    async def _fake_create_auto(**kw):
        created_meta_capture.append(kw.get("metadata"))
        return {"automation_id": AUTO_ID, **kw}

    monkeypatch.setattr(
        auto_db,
        "create_automation",
        create_autospec(auto_db.create_automation, side_effect=_fake_create_auto),
    )
    await lifecycle_mod.create_automation(
        OWNER,
        AutomationCreate(
            name="Personal job",
            trigger_type="cron",
            cron_expression="0 9 * * 1-5",
            instruction="Run",
            workspace_id=WS,
            metadata={"llm_model_system_only": True, "tag": "x"},
        ),
        model_pref=owner_pref,
    )
    assert created_meta_capture == [{"tag": "x"}]

    # (d) Updating ONLY reasoning_effort="high" on a system-only row whose owner shadow has NO efforts
    #     succeeds because classification uses system catalog (ignore_custom_models=True)
    prep_effort = await lifecycle_mod.prepare_owned_automation_update(
        AUTO_ID,
        OWNER,
        {"reasoning_effort": "high"},
        current=dict(stored_sys_row),
        model_pref=owner_pref,
    )
    assert prep_effort is not None
    assert prep_effort.update_kwargs["reasoning_effort"] == "high"
    eff_effort = dict(prep_effort.update_kwargs)
    await lifecycle_mod.recheck_locked_automation_update(
        user_id=OWNER,
        update=prep_effort.update,
        current=stored_sys_row,
        stored_in=WS,
        stored_pin=None,
        new_cron=None,
        new_tz=None,
        pref_for_lock=owner_pref,
        locked_row=dict(stored_sys_row),
        effective_kwargs=eff_effort,
        locked_conn=None,
    )
    assert eff_effort["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_in_tx_coverage_unit_rechecks_web_access_for_pm_and_non_owner_lead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 3: coverage-unit in-tx auth rechecks web read for PM and web write for non-owner
    lead analyst, refusing if workspace owner flipped web_access to hidden before the lock."""
    from src.dalton import access_tiers, sharing as sharing_mod
    from src.dalton.database import coverage_units as units_db

    ws_owner = "ws-owner-user"
    lead_analyst = "lead-analyst-user"
    conn = create_autospec(AsyncConnection, instance=True)

    @asynccontextmanager
    async def _dummy_cursor(*_a, **_kw):
        yield create_autospec(AsyncCursor, instance=True)

    conn.cursor.side_effect = _dummy_cursor

    unit_row = {
        "unit_id": "unit-1",
        "kind": "company",
        "ticker": "NVDA",
        "workspace_id": WS,
        "owner_user_id": ws_owner,
        "lead_analyst_user_id": lead_analyst,
    }
    monkeypatch.setattr(
        units_db,
        "get_active_unit_by_workspace",
        create_autospec(units_db.get_active_unit_by_workspace, return_value=unit_row),
    )
    monkeypatch.setattr(
        access_tiers,
        "classify_owner_tier",
        create_autospec(access_tiers.classify_owner_tier, return_value="internal"),
    )

    access_calls: list[tuple[str, str, str]] = []

    async def _deny_hidden(ws_arg, actor_id, channel="web", need="read", *, workspace=None, conn=None):
        access_calls.append((actor_id, channel, need))
        raise HTTPException(status_code=404, detail="Workspace not found")

    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(sharing_mod.require_workspace_access, side_effect=_deny_hidden),
    )

    orig_check_admin = schedule_mod._check_active_admin_in_tx
    # (a) PM editing unit automation when owner switched web_access to hidden -> refused with web/read check
    monkeypatch.setattr(
        schedule_mod,
        "_check_active_admin_in_tx",
        create_autospec(orig_check_admin, return_value=True),
    )
    with pytest.raises(lifecycle_mod.AutomationRefusal) as exc_pm:
        await schedule_mod._recheck_operator_authorization_in_tx(
            conn,
            effective_actor_id=PM,
            task_owner=lead_analyst,
            role="chief_of_staff",
            saved_name="dalton-nvda-watch",
            saved_metadata={"dalton_managed": True, "ticker": "NVDA"},
            locked_ws_id=WS,
            locked_ws_row={"workspace_id": WS, "user_id": ws_owner, "status": "running", "config": {}},
            locked_row=None,
            is_home=False,
            units_on=True,
            auth_on=True,
        )
    assert str(exc_pm.value) == "情况已变化，请重新发起"
    assert access_calls[-1] == (PM, "web", "read")

    # (b) Non-owner lead analyst (non-PM) -> rechecks web/write and refuses when revoked/hidden
    monkeypatch.setattr(
        schedule_mod,
        "_check_active_admin_in_tx",
        create_autospec(orig_check_admin, return_value=False),
    )
    with pytest.raises(lifecycle_mod.AutomationRefusal) as exc_lead:
        await schedule_mod._recheck_operator_authorization_in_tx(
            conn,
            effective_actor_id=lead_analyst,
            task_owner=lead_analyst,
            role="analyst",
            saved_name="dalton-nvda-watch",
            saved_metadata={"dalton_managed": True, "ticker": "NVDA"},
            locked_ws_id=WS,
            locked_ws_row={"workspace_id": WS, "user_id": ws_owner, "status": "running", "config": {}},
            locked_row=None,
            is_home=False,
            units_on=True,
            auth_on=True,
        )
    assert str(exc_lead.value) == "情况已变化，请重新发起"
    assert access_calls[-1] == (lead_analyst, "web", "write")


@pytest.mark.asyncio
async def test_global_job_name_on_regular_workspace_validates_against_regular_workspace_computer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 5: naming a task dalton-earnings-calendar on a regular workspace UUID validates
    against that workspace's computer, NOT Home's computer."""
    from src.server.services import home_placement

    _install_catalog_and_prefs(monkeypatch, owner_pref={})
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: False)
    monkeypatch.setattr("src.dalton.settings.is_dalton_auth_enabled", lambda: False)
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(
        schedule_mod,
        "verify_workspace_access",
        create_autospec(
            schedule_mod.verify_workspace_access,
            return_value={"workspace_id": WS, "user_id": OWNER, "computer_id": COMP_2},
        ),
    )
    monkeypatch.setattr(
        computer_db,
        "get_computer_for_workspace",
        create_autospec(computer_db.get_computer_for_workspace, return_value={"computer_id": COMP_2}),
    )
    home_turn_spy = create_autospec(
        home_placement.home_turn_computer,
        return_value=({"computer_id": COMP_1, "kind": "ssh"}, False),
    )
    monkeypatch.setattr(home_placement, "home_turn_computer", home_turn_spy)

    conn = create_autospec(AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor(*_a: Any, **_kw: Any):
        cur = create_autospec(AsyncCursor, instance=True)
        last: dict[str, Any] = {"row": None}

        async def _exec(sql: str, params: Any = None) -> None:
            norm = " ".join(sql.split())
            if "FROM computers" in norm:
                last["row"] = {
                    "computer_id": COMP_2,
                    "user_id": OWNER,
                    "kind": "docker",
                    "status": "running",
                    "name": "Cloud",
                }
            elif "FROM workspaces" in norm:
                last["row"] = {
                    "workspace_id": WS,
                    "user_id": OWNER,
                    "computer_id": COMP_2,
                    "status": "running",
                    "config": {},
                }
            elif "FROM workspace_moves" in norm:
                last["row"] = {"move_fenced": False}
            else:
                last["row"] = None

        cur.execute.side_effect = _exec
        cur.fetchone.side_effect = lambda: last["row"]
        yield cur

    conn.cursor.side_effect = _cursor

    @asynccontextmanager
    async def _lock(uid: str, *, conn: Any = None, prelock_ws=None):
        yield conn_ref

    conn_ref = conn
    monkeypatch.setattr(schedule_mod, "_user_automations_lock_for_write", _lock)
    monkeypatch.setattr(
        auto_db,
        "list_all_automations",
        create_autospec(auto_db.list_all_automations, return_value=[]),
    )
    monkeypatch.setattr(
        auto_db,
        "create_automation",
        create_autospec(
            auto_db.create_automation,
            return_value={"automation_id": AUTO_ID, "workspace_id": WS, "name": "dalton-earnings-calendar"},
        ),
    )
    monkeypatch.setattr(
        "src.dalton.database.company_automations.resolve_automation_scope_for_write",
        create_autospec(
            __import__(
                "src.dalton.database.company_automations",
                fromlist=["resolve_automation_scope_for_write"],
            ).resolve_automation_scope_for_write,
            return_value="personal",
        ),
    )

    seen_computers: list[Any] = []

    async def _spy_cc(uid, model, ws, c=None, pref=None, *, resolved_computer=lifecycle_mod._UNSET_COMPUTER):
        seen_computers.append((ws, resolved_computer))

    monkeypatch.setattr(
        lifecycle_mod,
        "check_claude_code_hosted",
        create_autospec(lifecycle_mod.check_claude_code_hosted, side_effect=_spy_cc),
    )

    res = await schedule_mod.schedule_dalton_automation_helper(
        "dalton-earnings-calendar",
        {
            "trigger_type": "cron",
            "cron_expression": "0 9 * * 6",
            "instruction": "Calendar on regular workspace.",
            "llm_model": "claude-sonnet-4-6",
        },
        user_id=OWNER,
        workspace_id=WS,
        role="chief_of_staff",
    )
    assert res["ok"] is True
    home_turn_spy.assert_not_awaited()
    assert len(seen_computers) == 1
    assert seen_computers[0][0] == WS
    assert seen_computers[0][1]["computer_id"] == COMP_2


@pytest.mark.asyncio
async def test_handoff_and_manual_compaction_only_apply_system_only_to_old_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 6: model-switch handoff and manual compaction apply the previous run's
    llm_model_system_only flag ONLY to the old model, never to the newly switched model."""
    from src.llms import provider_compaction as pc_mod
    from src.server.handlers import thread_model_handoff as handoff_mod
    from src.server.services.llm import thread_model as thread_model_mod

    monkeypatch.setattr(
        "src.server.services.account.own_access",
        create_autospec(
            __import__("src.server.services.account", fromlist=["own_access"]).own_access,
            return_value=SimpleNamespace(api_keys=False),
        ),
    )
    monkeypatch.setattr(
        maint_mod.TurnIdentity,
        "of",
        create_autospec(
            TurnIdentity.of,
            return_value=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        ),
    )
    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())

    route = TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst")
    latest_run_id = "77777777-7777-4777-8777-777777777777"
    owner_run_id = "77777777-7777-4777-8777-777777777778"
    orig_latest = runs_lifecycle_db.get_latest_attempt
    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(
            orig_latest,
            return_value={
                "conversation_response_id": latest_run_id,
                "metadata": {"llm_model": "claude-sonnet-4-6"},
            },
        ),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(
            atm_mod._get_execution_for_run_id,
            side_effect=lambda rid: (
                {"automation_execution_id": EXEC_ID, "llm_model_system_only": True}
                if str(rid) == latest_run_id
                else None
            ),
        ),
    )

    resolve_log: list[tuple[str | None, bool]] = []

    async def _fake_resolve_llm(_base, _uid, model_name, _byok, **kw):
        ign = bool(kw.get("ignore_custom_models", False))
        resolve_log.append((model_name, ign))
        # Distinguish system client vs custom client
        client = SimpleNamespace(model=model_name, system_only=ign)
        return SimpleNamespace(
            llm=SimpleNamespace(name=model_name),
            llm_client=client,
            get_llm_client=lambda c=client: c,
        )

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, side_effect=_fake_resolve_llm),
    )

    orig_thread_route = maint_mod.thread_route
    # (a) _hand_off_config: new model (owner's custom) uses ignore_custom_models=False, old uses True
    monkeypatch.setattr(
        maint_mod,
        "_summary_to_hand_off",
        create_autospec(maint_mod._summary_to_hand_off, return_value=SimpleNamespace()),
    )
    monkeypatch.setattr(
        maint_mod,
        "thread_route",
        create_autospec(
            orig_thread_route,
            return_value=({"llm_model": "owner-custom-only"}, route),
        ),
    )
    monkeypatch.setattr(
        pc_mod,
        "replays",
        create_autospec(
            pc_mod.replays,
            side_effect=lambda _summary, client: client.model == "claude-sonnet-4-6" and client.system_only,
        ),
    )

    res_ho = await maint_mod._hand_off_config(
        THREAD_ID, OWNER, "claude-sonnet-4-6", source_system_only=True
    )
    assert res_ho is not None
    assert resolve_log[-2:] == [
        ("owner-custom-only", False),  # new model -> owner's own choice
        ("claude-sonnet-4-6", True),  # old model -> previous run's execution system-only flag
    ]

    # (b) inspect_hand_off: same split between new model and old model
    resolve_log.clear()
    status, payload = await handoff_mod.inspect_hand_off(
        THREAD_ID, OWNER, "claude-sonnet-4-6", source_system_only=True
    )
    assert status == "ready" and payload is not None
    assert resolve_log == [
        ("owner-custom-only", False),
        ("claude-sonnet-4-6", True),
    ]

    # (c) Manual compaction uses previous run's actual model + execution flag (not thread setting)
    resolve_log.clear()
    monkeypatch.setattr(
        thread_model_mod,
        "turn_model",
        create_autospec(
            thread_model_mod.turn_model,
            side_effect=lambda _u, _t, *, named, held, clear_dead=True: named or held,
        ),
    )
    await maint_mod._compaction_config(THREAD_ID, OWNER)
    assert resolve_log == [("claude-sonnet-4-6", True)]

    # (d) Once an owner turn (no automation execution) runs on owner-custom-only, manual compaction uses it with False
    resolve_log.clear()
    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(
            orig_latest,
            return_value={
                "conversation_response_id": owner_run_id,
                "metadata": {"llm_model": "owner-custom-only"},
            },
        ),
    )
    await maint_mod._compaction_config(THREAD_ID, OWNER)
    assert resolve_log == [("owner-custom-only", False)]


def test_user_profile_prompt_llm_model_guidance_and_autonomy_gate() -> None:
    """Decision 7: user_profile.md.j2 documents llm_model for all managed automations (omit=keep,
    empty/default/clear=clear & inherit owner default, cross-user=system catalog only) and hides
    dalton_schedule_automation when autonomy_enabled=False."""
    from ptc_agent.agent.prompts.loader import PromptLoader

    loader = PromptLoader()
    rendered_on = loader.render(
        "components/user_profile.md.j2",
        user_profile={"name": "Analyst", "timezone": "Asia/Shanghai", "locale": "zh-CN"},
        preferences=SimpleNamespace(empty=True),
        investment_lines=[],
        user_data_counts=None,
        main_agent_steering=True,
        channels_enabled=False,
        wakeups_enabled=False,
        role="chief_of_staff",
        autonomy_enabled=True,
        data_jobs_enabled=True,
    )
    assert "财报任务的新建、改期和剩余窗口补建用 `dalton_schedule_automation`" in rendered_on
    assert "公司公共数据任务用 `dalton_system_jobs`" in rendered_on
    assert "管理受管自动化任务时，可用 `llm_model`" in rendered_on
    assert "省略会保留当前设置" in rendered_on
    assert '传入 `""`、`"default"` 或 `"clear"` 会清除覆盖并继承任务所有者的默认模型' in rendered_on
    assert "跨人修改时，仅填写系统目录模型" in rendered_on

    rendered_off = loader.render(
        "components/user_profile.md.j2",
        user_profile={"name": "Analyst", "timezone": "Asia/Shanghai", "locale": "zh-CN"},
        preferences=SimpleNamespace(empty=True),
        investment_lines=[],
        user_data_counts=None,
        main_agent_steering=True,
        channels_enabled=False,
        wakeups_enabled=False,
        role="chief_of_staff",
        autonomy_enabled=False,
        data_jobs_enabled=True,
    )
    assert "dalton_schedule_automation" not in rendered_off
    assert "dalton_system_jobs" not in rendered_off
    assert "llm_model" not in rendered_off


# ---------------------------------------------------------------------------
# Round 3 tests: Decisions 1, 2, 3, 4, 5
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hitl_resume_reads_interrupt_checkpoint_run_even_after_later_failed_or_cancelled_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 1 (codex high 1): when a card is produced by a system-only run and a later run
    fails/cancels before writing a new graph step, HITL resume resolves llm_model_system_only
    and execution provenance from the interrupt checkpoint's run_id — never from get_latest_attempt."""
    from src.server.services.llm import thread_model as thread_model_mod
    from src.server.utils import checkpoint_helpers as cp_helpers

    orig_run_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
    later_failed_run_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
    exec_row = {
        "automation_execution_id": EXEC_ID,
        "automation_id": AUTO_ID,
        "status": "running",
        "conversation_thread_id": THREAD_ID,
        "user_id": OWNER,
        "llm_model": "claude-sonnet-4-6",
        "reasoning_effort": "medium",
        "llm_model_system_only": True,
        "snapshot_taken": True,
    }

    # Build a checkpointer where:
    # - cp-interrupt was written by orig_run_id with a pending interrupt
    # - cp-flush is a top-of-branch no-op "update" checkpoint (same versions_seen) from a stop/flush
    cp_interrupt = SimpleNamespace(
        config={"configurable": {"thread_id": THREAD_ID, "checkpoint_id": "cp-interrupt"}},
        checkpoint={"versions_seen": {"agent": {"messages": 3}}},
        metadata={"source": "loop", "run_id": orig_run_id},
        parent_config=None,
        pending_writes=[("task-1", "__interrupt__", [{"id": "int-card-1", "value": {"type": "confirm"}}])],
    )
    cp_flush = SimpleNamespace(
        config={"configurable": {"thread_id": THREAD_ID, "checkpoint_id": "cp-flush"}},
        checkpoint={"versions_seen": {"agent": {"messages": 3}}},
        metadata={"source": "update", "run_id": later_failed_run_id},
        parent_config=cp_interrupt.config,
        pending_writes=[],
    )

    class _FakeCheckpointer:
        async def aget_tuple(self, cfg: dict[str, Any]):
            cid = (cfg.get("configurable") or {}).get("checkpoint_id")
            if cid == "cp-interrupt":
                return cp_interrupt
            return cp_flush

    fake_cp = _FakeCheckpointer()
    monkeypatch.setattr(
        cp_helpers,
        "get_checkpointer",
        create_autospec(cp_helpers.get_checkpointer, return_value=fake_cp),
    )
    monkeypatch.setattr(
        cp_helpers,
        "turn_base_checkpoint_id",
        create_autospec(cp_helpers.turn_base_checkpoint_id, return_value="cp-flush"),
    )
    # Latest attempt on the thread is the LATER cancelled/failed run
    latest_spy = create_autospec(
        runs_lifecycle_db.get_latest_attempt,
        return_value={
            "conversation_response_id": later_failed_run_id,
            "status": "cancelled",
            "metadata": {},
        },
    )
    monkeypatch.setattr(runs_lifecycle_db, "get_latest_attempt", latest_spy)

    # _get_execution_for_run_id only returns exec_row (with llm_model_system_only=True) for orig_run_id
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(
            atm_mod._get_execution_for_run_id,
            side_effect=lambda rid: exec_row if str(rid) == orig_run_id else None,
        ),
    )
    _install_catalog_and_prefs(monkeypatch, owner_pref={})
    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())
    monkeypatch.setattr(
        thread_model_mod,
        "model_resolves",
        create_autospec(thread_model_mod.model_resolves, return_value=True),
    )
    monkeypatch.setattr(
        atm_mod,
        "model_resolves",
        create_autospec(atm_mod.model_resolves, return_value=True),
    )

    seen_calls: list[tuple[str | None, bool]] = []

    async def _fake_resolve(_base, _uid, model_name, _byok, **kw):
        seen_calls.append((model_name, bool(kw.get("ignore_custom_models", False))))
        return SimpleNamespace(llm=SimpleNamespace(name=model_name), llm_client=object())

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, side_effect=_fake_resolve),
    )

    run_meta: dict[str, Any] = {}
    resolver = thread_agent.thread_resolver(
        OWNER,
        THREAD_ID,
        TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
        identity=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        thread={"llm_model": "claude-sonnet-4-6"},
        is_byok=False,
    )
    req = ChatRequest(
        workspace_id=WS,
        messages=[],
        hitl_response={"int-card-1": {"decisions": [{"type": "approve"}]}},
        llm_model="claude-sonnet-4-6",
        reasoning_effort="medium",
    )
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=req,
        requested_model="claude-sonnet-4-6",
        held_model="claude-sonnet-4-6",
        from_service=False,
        resolve=resolver,
        on_automation_context=run_meta.update,
    )
    assert "llm_model_system_only" not in run_meta
    assert run_meta.get("automation_source_execution_id") == EXEC_ID
    assert seen_calls == [("claude-sonnet-4-6", True)]
    latest_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_running_thread_continuation_wakeup_chain_and_server_explicit_model_and_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chain 1: running_thread continuation stamps automation_source_execution_id so a wakeup
    created by that continuation finds the execution via created_by_run_id and reads its
    execution-level llm_model_system_only.
    Chain 2: server request with explicit llm_model AND reasoning_effort still queries source
    execution (running_thread) to read llm_model_system_only while keeping the explicit model."""
    from src.server.services.llm import thread_model as thread_model_mod

    cont_run_id = "cccccccc-cccc-4ccc-8ccc-ccccccccccc1"
    wakeup_id = "dddddddd-dddd-4ddd-8ddd-ddddddddddd1"
    exec_row = {
        "automation_execution_id": EXEC_ID,
        "automation_id": AUTO_ID,
        "status": "running",
        "conversation_thread_id": THREAD_ID,
        "user_id": OWNER,
        "llm_model": "claude-sonnet-4-6",
        "reasoning_effort": "medium",
        "llm_model_system_only": True,
        "snapshot_taken": True,
    }
    _install_catalog_and_prefs(monkeypatch, owner_pref={})
    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())
    monkeypatch.setattr(
        thread_model_mod,
        "model_resolves",
        create_autospec(thread_model_mod.model_resolves, return_value=True),
    )
    monkeypatch.setattr(
        atm_mod,
        "model_resolves",
        create_autospec(atm_mod.model_resolves, return_value=True),
    )
    monkeypatch.setattr(
        atm_mod.exec_db,
        "get_running_executions_for_thread",
        create_autospec(atm_mod.exec_db.get_running_executions_for_thread, return_value=[exec_row]),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_first_turn_settings",
        create_autospec(
            atm_mod._get_execution_first_turn_settings,
            return_value=("claude-sonnet-4-6", True, "medium"),
        ),
    )

    resolve_calls: list[tuple[str | None, bool, str | None]] = []

    async def _fake_resolve(_base, _uid, model_name, _byok, **kw):
        resolve_calls.append(
            (model_name, bool(kw.get("ignore_custom_models", False)), kw.get("reasoning_effort"))
        )
        return SimpleNamespace(llm=SimpleNamespace(name=model_name), llm_client=object())

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, side_effect=_fake_resolve),
    )

    # --- Chain 1, Step 1: running_thread server continuation ---
    persisted_runs: dict[str, dict[str, Any]] = {}
    cont_meta: dict[str, Any] = {}
    cont_resolver = thread_agent.thread_resolver(
        OWNER,
        THREAD_ID,
        TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
        identity=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        thread={},
        is_byok=False,
    )
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=ChatRequest(
            workspace_id=WS,
            messages=[ChatMessage(role="user", content="background continue")],
            query_type="system",
        ),
        requested_model=None,
        held_model=None,
        from_service=True,
        resolve=cont_resolver,
        on_automation_context=cont_meta.update,
    )
    assert "llm_model_system_only" not in cont_meta
    assert cont_meta["automation_source_execution_id"] == EXEC_ID
    persisted_runs[cont_run_id] = dict(cont_meta)

    # --- Chain 1, Step 2: wakeup created by cont_run_id ---
    monkeypatch.setattr(
        atm_mod,
        "_get_wakeup_creator_run_ids",
        create_autospec(atm_mod._get_wakeup_creator_run_ids, return_value=[cont_run_id]),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(
            atm_mod._get_execution_for_run_id,
            side_effect=lambda rid: (
                exec_row
                if persisted_runs.get(str(rid), {}).get("automation_source_execution_id") == EXEC_ID
                else None
            ),
        ),
    )
    wakeup_meta: dict[str, Any] = {}
    wakeup_resolver = thread_agent.thread_resolver(
        OWNER,
        THREAD_ID,
        TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
        identity=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        thread={},
        is_byok=False,
    )
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=ChatRequest(
            workspace_id=WS,
            messages=[ChatMessage(role="user", content="wakeup fired")],
            query_type="wakeup",
            wakeup={"notices": [{"wakeup_id": wakeup_id}]},
        ),
        requested_model=None,
        held_model=None,
        from_service=True,
        resolve=wakeup_resolver,
        on_automation_context=wakeup_meta.update,
    )
    assert "llm_model_system_only" not in wakeup_meta
    assert wakeup_meta["automation_source_execution_id"] == EXEC_ID
    assert resolve_calls[-1] == ("claude-sonnet-4-6", True, "medium")

    # --- Chain 2: server request with explicit llm_model AND reasoning_effort ---
    explicit_meta: dict[str, Any] = {}
    explicit_resolver = thread_agent.thread_resolver(
        OWNER,
        THREAD_ID,
        TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
        identity=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        thread={},
        is_byok=False,
        reasoning_effort="high",
    )
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=ChatRequest(
            workspace_id=WS,
            messages=[ChatMessage(role="user", content="steered")],
            query_type="system",
            llm_model="gpt-6.2-sol",
            reasoning_effort="high",
        ),
        requested_model="gpt-6.2-sol",
        held_model=None,
        from_service=True,
        resolve=explicit_resolver,
        on_automation_context=explicit_meta.update,
    )
    assert "llm_model_system_only" not in explicit_meta
    assert explicit_meta["automation_source_execution_id"] == EXEC_ID
    # Round 8 Decision 1: linked execution snapshot wins over request's explicit model & effort
    assert resolve_calls[-1] == ("claude-sonnet-4-6", True, "medium")


@pytest.mark.asyncio
async def test_pause_and_resume_recheck_workspace_web_access_in_tx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 3 (codex medium 3): pause and resume run the same in-tx workspace web-access
    recheck (PM web read, non-owner lead analyst web write) and refuse when hidden."""
    from src.dalton import access_tiers, sharing as sharing_mod
    from src.dalton.database import coverage_units as units_db

    ws_owner = "ws-owner-pr"
    lead_analyst = "lead-analyst-pr"
    unit_row = {
        "unit_id": "unit-pr",
        "kind": "company",
        "ticker": "NVDA",
        "workspace_id": WS,
        "owner_user_id": ws_owner,
        "lead_analyst_user_id": lead_analyst,
    }
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr("src.dalton.settings.is_dalton_auth_enabled", lambda: True)
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(
        schedule_mod,
        "verify_workspace_access",
        create_autospec(
            schedule_mod.verify_workspace_access,
            return_value={"workspace_id": WS, "user_id": ws_owner},
        ),
    )
    monkeypatch.setattr(
        units_db,
        "get_active_unit_by_workspace",
        create_autospec(units_db.get_active_unit_by_workspace, return_value=unit_row),
    )
    monkeypatch.setattr(
        access_tiers,
        "classify_owner_tier",
        create_autospec(access_tiers.classify_owner_tier, return_value="internal"),
    )

    conn = create_autospec(AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor(*_a: Any, **_kw: Any):
        cur = create_autospec(AsyncCursor, instance=True)
        last: dict[str, Any] = {"row": None}

        async def _exec(sql: str, params: Any = None) -> None:
            if "FROM workspaces" in sql:
                last["row"] = {
                    "workspace_id": WS,
                    "user_id": ws_owner,
                    "computer_id": COMP_1,
                    "status": "running",
                    "config": {},
                }
            else:
                last["row"] = None

        cur.execute.side_effect = _exec
        cur.fetchone.side_effect = lambda: last["row"]
        yield cur

    conn.cursor.side_effect = _cursor

    @asynccontextmanager
    async def _lock(uid: str, *, conn: Any = None, prelock_ws=None):
        yield lock_conn_ref

    lock_conn_ref = conn
    monkeypatch.setattr(schedule_mod, "_user_automations_lock_for_write", _lock)

    access_calls: list[tuple[str, str, str]] = []

    async def _deny_hidden(ws_arg, actor_id, channel="web", need="read", *, workspace=None, conn=None):
        access_calls.append((actor_id, channel, need))
        raise HTTPException(status_code=404, detail="Workspace not found")

    monkeypatch.setattr(
        sharing_mod,
        "require_workspace_access",
        create_autospec(sharing_mod.require_workspace_access, side_effect=_deny_hidden),
    )

    orig_list = auto_db.list_all_automations
    orig_update = auto_db.update_automation

    for action_name, initial_status in (("pause", "active"), ("resume", "paused")):
        row = _make_auto_row(
            user_id=lead_analyst,
            name="dalton-nvda-watch",
            status=initial_status,
            metadata={"dalton_managed": True, "ticker": "NVDA"},
        )
        monkeypatch.setattr(
            auto_db,
            "list_all_automations",
            create_autospec(orig_list, return_value=[dict(row)]),
        )

        async def _run_reconciler(aid: str, uid: str, *, conn: Any = None, _r=row, **kw: Any):
            effective = dict(kw)
            hist_ctx = company_automations.get_current_history_context()
            assert hist_ctx is not None and hist_ctx.locked_row_reconciler is not None
            await hist_ctx.locked_row_reconciler(_r, effective, conn)
            return {**_r, **effective}

        monkeypatch.setattr(
            auto_db,
            "update_automation",
            create_autospec(orig_update, side_effect=_run_reconciler),
        )

        # (a) PM: entry passed web/read, then owner flipped web_access to hidden before lock
        monkeypatch.setattr("src.dalton.autonomy.system_jobs.is_pm_user", lambda uid: uid == PM)
        res_pm = await schedule_mod.schedule_dalton_automation_helper(
            "dalton-nvda-watch",
            user_id=PM,
            workspace_id=WS,
            role="chief_of_staff",
            action=action_name,
        )
        assert res_pm["ok"] is False
        assert res_pm["error"] == "workspace_changed"
        assert res_pm["message"] == "情况已变化，请重新发起"
        assert access_calls[-1] == (PM, "web", "read")

        # (b) Non-owner lead analyst: entry passed web/write, then owner hid/downgraded before lock
        monkeypatch.setattr("src.dalton.autonomy.system_jobs.is_pm_user", lambda _uid: False)
        res_lead = await schedule_mod.schedule_dalton_automation_helper(
            "dalton-nvda-watch",
            user_id=lead_analyst,
            workspace_id=WS,
            role="chief_of_staff",
            action=action_name,
        )
        assert res_lead["ok"] is False
        assert res_lead["error"] == "workspace_changed"
        assert res_lead["message"] == "情况已变化，请重新发起"
        assert access_calls[-1] == (lead_analyst, "web", "write")


@pytest.mark.asyncio
async def test_same_name_system_to_custom_switch_triggers_handoff_and_post_handoff_manual_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1) Web PATCH from system-only 'claude-sonnet-4-6' to owner's same-named 'claude-sonnet-4-6'
       triggers summary handoff because (model, execution system_only) changed, without stamping
       owner_model_switch_run_id.
    2) In-chat switch_thread_model(defer_handoff=True) -> run_pending_model_handoff settles
       via clear_pending_model_handoff (no owner_model_switch_run_id)."""
    from src.dalton.database import model_switch as dalton_ms_db
    from src.server.app.threads import crud as crud_mod
    from src.server.database.conversation import threads_read, threads_write
    from src.server.handlers import thread_model_handoff as handoff_mod
    from src.server.models.conversation import ThreadUpdateRequest
    from src.server.services.llm import thread_model as thread_model_mod
    from src.server.services.thread_mutation import ThreadMutationRunner

    latest_run_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1"
    latest_run = {
        "conversation_response_id": latest_run_id,
        "metadata": {"llm_model": "claude-sonnet-4-6"},
    }
    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(runs_lifecycle_db.get_latest_attempt, return_value=latest_run),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(
            atm_mod._get_execution_for_run_id,
            side_effect=lambda rid: (
                {"automation_execution_id": EXEC_ID, "llm_model_system_only": True}
                if str(rid) == latest_run_id
                else None
            ),
        ),
    )

    # --- Part A: Web PATCH /threads/{id} with same model string triggers hand_off_on_switch ---
    thread_state: dict[str, Any] = {
        "conversation_thread_id": THREAD_ID,
        "workspace_id": WS,
        "user_id": OWNER,
        "thread_index": 1,
        "current_status": "idle",
        "subagents_allowed": True,
        "llm_model": "claude-sonnet-4-6",
        "metadata": {},
        "is_pinned": False,
        "archived": False,
        "is_shared": False,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    async def _fake_update_thread(tid: str, **kw: Any):
        if "llm_model" in kw:
            thread_state["llm_model"] = kw["llm_model"]
        if isinstance(kw.get("metadata_merge"), dict):
            thread_state["metadata"] = {
                **thread_state["metadata"],
                **kw["metadata_merge"],
            }
        return dict(thread_state)

    monkeypatch.setattr(
        crud_mod,
        "_authorize_thread_write",
        create_autospec(
            crud_mod._authorize_thread_write,
            return_value=SimpleNamespace(is_shared=False, org_id=None),
        ),
    )
    monkeypatch.setattr(
        crud_mod,
        "require_selectable_model",
        create_autospec(crud_mod.require_selectable_model, return_value=None),
    )
    monkeypatch.setattr(
        crud_mod,
        "get_thread_auth_meta",
        create_autospec(crud_mod.get_thread_auth_meta, side_effect=lambda _t: dict(thread_state)),
    )
    monkeypatch.setattr(
        crud_mod,
        "update_thread_fields",
        create_autospec(crud_mod.update_thread_fields, side_effect=_fake_update_thread),
    )
    monkeypatch.setattr(
        thread_model_mod,
        "next_turn_runs_claude_code",
        create_autospec(thread_model_mod.next_turn_runs_claude_code, return_value=False),
    )
    handoff_spy = create_autospec(maint_mod.hand_off_on_switch, return_value=True)
    monkeypatch.setattr(maint_mod, "hand_off_on_switch", handoff_spy)

    patched_item = await crud_mod.update_thread_endpoint(
        THREAD_ID,
        ThreadUpdateRequest(llm_model="claude-sonnet-4-6"),
        x_user_id=OWNER,
    )
    assert patched_item.summary_handoff is True
    handoff_spy.assert_awaited_once_with(
        THREAD_ID, OWNER, "claude-sonnet-4-6", source_system_only=True
    )
    assert "owner_model_switch_run_id" not in thread_state["metadata"]

    # --- Part B: In-chat switch_thread_model(defer_handoff=True) -> run_pending_model_handoff ---
    orig_req_sel = thread_model_mod.require_selectable_model
    orig_get_auth_meta = threads_read.get_thread_auth_meta
    orig_update_fields = threads_write.update_thread_fields
    orig_clear_meta = dalton_ms_db.clear_thread_metadata_key
    orig_inspect_handoff = handoff_mod.inspect_hand_off

    for settle_mode in ("completed", "not_needed"):
        inchat_state: dict[str, Any] = {
            "conversation_thread_id": THREAD_ID,
            "workspace_id": WS,
            "user_id": OWNER,
            "llm_model": "claude-sonnet-4-6",
            "metadata": {},
        }

        async def _inchat_update(tid: str, *, _st=inchat_state, **kw: Any):
            if "llm_model" in kw:
                _st["llm_model"] = kw["llm_model"]
            if isinstance(kw.get("metadata_merge"), dict):
                _st["metadata"] = {**_st["metadata"], **kw["metadata_merge"]}
            return dict(_st)

        async def _inchat_clear(tid: str, key: str, *, expected_version=None, _st=inchat_state):
            if key in _st["metadata"]:
                _st["metadata"] = {k: v for k, v in _st["metadata"].items() if k != key}
                return True
            return False

        monkeypatch.setattr(
            thread_model_mod,
            "require_selectable_model",
            create_autospec(orig_req_sel, return_value=None),
        )
        monkeypatch.setattr(
            threads_read,
            "get_thread_auth_meta",
            create_autospec(orig_get_auth_meta, side_effect=lambda _t, _st=inchat_state: dict(_st)),
        )
        monkeypatch.setattr(
            threads_write,
            "update_thread_fields",
            create_autospec(orig_update_fields, side_effect=_inchat_update),
        )
        monkeypatch.setattr(
            dalton_ms_db,
            "clear_thread_metadata_key",
            create_autospec(orig_clear_meta, side_effect=_inchat_clear),
        )

        sw_res = await thread_model_mod.switch_thread_model(
            OWNER,
            THREAD_ID,
            "claude-sonnet-4-6",
            defer_handoff=True,
            model_pref={},
            short_circuit_default=True,
            running_model="claude-sonnet-4-6",
            running_run_id=latest_run_id,
        )
        assert sw_res.changed is True
        assert thread_model_mod.PENDING_MODEL_HANDOFF_KEY in inchat_state["metadata"]
        assert "owner_model_switch_run_id" not in inchat_state["metadata"]

        inspect_seq = (
            [("ready", (SimpleNamespace(), None)), ("not_needed", None)]
            if settle_mode == "completed"
            else [("not_needed", None)]
        )
        inspect_iter = iter(inspect_seq)
        monkeypatch.setattr(
            handoff_mod,
            "inspect_hand_off",
            create_autospec(orig_inspect_handoff, side_effect=lambda *_a, **_kw: next(inspect_iter)),
        )
        fake_runner = create_autospec(ThreadMutationRunner, instance=True)
        fake_runner.wait_until_idle.return_value = True
        monkeypatch.setattr(
            ThreadMutationRunner,
            "get_instance",
            staticmethod(lambda _r=fake_runner: _r),
        )

        outcome = await handoff_mod.run_pending_model_handoff(THREAD_ID, user_id=OWNER)
        assert outcome == settle_mode
        assert thread_model_mod.PENDING_MODEL_HANDOFF_KEY not in inchat_state["metadata"]
        assert "owner_model_switch_run_id" not in inchat_state["metadata"]


@pytest.mark.asyncio
async def test_http_retry_and_pending_input_retry_routes_preserve_system_only_to_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 5 (codex medium 5): HTTP POST /api/v1/threads/{id}/retry exercises BOTH
    checkpoint retry and pending-input rerun through FastAPI routing all the way to
    resolve_llm_config and astream_ptc_workflow(run_metadata=...)."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.server.app.threads import messaging as msg_mod
    from src.server.database.conversation import queries as queries_db
    from src.server.dependencies import cli_approval, principal as principal_mod, usage_limits
    from src.server.handlers import chat as chat_handlers, checkpoint_handler
    from src.server.services import turn_runtime
    from src.server.services.llm import thread_model as thread_model_mod
    from src.server.services.runs import pending_input as pending_input_mod
    from src.server.services.shared_workspaces.turn import TurnIdentity as SharedTurnIdentity

    app = FastAPI()
    app.include_router(msg_mod.router)

    async def _fake_limit(uid: str):
        return usage_limits.ChatAuthResult(
            user_id=uid,
            is_byok=False,
            has_oauth=False,
            access_tier=1,
            burst_slot_id="slot-1",
            own_subscriptions=False,
        )

    app.dependency_overrides[principal_mod._RUN] = lambda: principal_mod.Principal(
        user_id=OWNER, auth="session"
    )
    app.dependency_overrides[usage_limits.chat_limiter] = lambda: _fake_limit

    monkeypatch.setattr("src.config.settings.HOST_MODE", "oss")
    monkeypatch.setattr("src.dalton.auth.is_dalton_supabase_auth_enabled", lambda: False)
    monkeypatch.setattr("src.dalton.settings.get_dalton_auth_mode", lambda: "oss")
    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())
    monkeypatch.setattr(
        cli_approval,
        "admit_retry",
        create_autospec(cli_approval.admit_retry, return_value=None),
    )
    monkeypatch.setattr(
        msg_mod,
        "_assert_stream_transport_ready",
        create_autospec(msg_mod._assert_stream_transport_ready, return_value=None),
    )
    monkeypatch.setattr(
        msg_mod,
        "get_thread_auth_meta",
        create_autospec(
            msg_mod.get_thread_auth_meta,
            return_value={"user_id": OWNER, "workspace_id": WS, "llm_model": "claude-sonnet-4-6"},
        ),
    )
    monkeypatch.setattr(
        msg_mod,
        "_settle_against_branch",
        create_autospec(msg_mod._settle_against_branch, side_effect=lambda _t, req, _u: req),
    )
    monkeypatch.setattr(
        "src.dalton.sharing.resolve_colleague_turn_admission",
        create_autospec(
            __import__(
                "src.dalton.sharing", fromlist=["resolve_colleague_turn_admission"]
            ).resolve_colleague_turn_admission,
            return_value=({"workspace_id": WS, "user_id": OWNER, "computer_id": COMP_1}, "web", False),
        ),
    )
    route = TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst")
    monkeypatch.setattr(
        turn_runtime,
        "turn_route",
        create_autospec(turn_runtime.turn_route, return_value=route),
    )
    monkeypatch.setattr(
        TurnRoute,
        "bind",
        create_autospec(TurnRoute.bind, return_value=route),
    )
    monkeypatch.setattr(
        SharedTurnIdentity,
        "of",
        create_autospec(
            SharedTurnIdentity.of,
            return_value=SharedTurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        ),
    )
    monkeypatch.setattr(
        usage_limits,
        "enforce_credit_limit",
        create_autospec(usage_limits.enforce_credit_limit, return_value=None),
    )
    monkeypatch.setattr(
        usage_limits,
        "release_burst_slot",
        create_autospec(usage_limits.release_burst_slot, return_value=None),
    )
    monkeypatch.setattr(
        checkpoint_handler,
        "get_retry_checkpoint",
        create_autospec(checkpoint_handler.get_retry_checkpoint, return_value="cp-retry-1"),
    )
    monkeypatch.setattr(
        pending_input_mod,
        "retried_wake_fields",
        create_autospec(pending_input_mod.retried_wake_fields, return_value={}),
    )
    monkeypatch.setattr(
        queries_db,
        "get_query_at_turn",
        create_autospec(
            queries_db.get_query_at_turn,
            return_value={"content": "retry pending input", "type": "initial", "metadata": {}},
        ),
    )
    monkeypatch.setattr(
        msg_mod,
        "observe_chat_stream",
        lambda gen, **_kw: gen,
    )
    monkeypatch.setattr(
        "src.dalton.auth.dispatch.wrap_local_sse_stream",
        lambda gen, *_a, **_kw: gen,
    )
    _install_catalog_and_prefs(monkeypatch, owner_pref={})
    monkeypatch.setattr(
        thread_model_mod,
        "model_resolves",
        create_autospec(thread_model_mod.model_resolves, return_value=True),
    )

    monkeypatch.setattr(
        atm_mod,
        "model_resolves",
        create_autospec(atm_mod.model_resolves, return_value=True),
    )

    exec_row = {
        "automation_execution_id": EXEC_ID,
        "automation_id": AUTO_ID,
        "status": "running",
        "conversation_thread_id": THREAD_ID,
        "user_id": OWNER,
        "llm_model": "claude-sonnet-4-6",
        "reasoning_effort": "medium",
        "llm_model_system_only": True,
        "snapshot_taken": True,
    }
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(atm_mod._get_execution_for_run_id, return_value=exec_row),
    )

    resolve_seen: list[tuple[str | None, bool, str | None]] = []

    async def _fake_resolve(_base, _uid, model_name, _byok, **kw):
        resolve_seen.append(
            (model_name, bool(kw.get("ignore_custom_models", False)), kw.get("reasoning_effort"))
        )
        return SimpleNamespace(
            llm=SimpleNamespace(name=model_name),
            llm_client=object(),
            credential_source=None,
        )

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, side_effect=_fake_resolve),
    )

    workflow_run_metas: list[dict[str, Any] | None] = []

    async def _fake_astream(*_a: Any, **kw: Any):
        workflow_run_metas.append(kw.get("run_metadata"))
        if kw.get("setup_over") is not None:
            kw["setup_over"].set()
        yield "event: done\ndata: {}\n\n"

    monkeypatch.setattr(
        chat_handlers,
        "astream_ptc_workflow",
        create_autospec(chat_handlers.astream_ptc_workflow, side_effect=_fake_astream),
    )

    failed_run_id = "99999999-9999-4999-8999-999999999901"
    orig_get_latest = runs_lifecycle_db.get_latest_attempt

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # (1) Checkpoint /retry with BOTH llm_model and reasoning_effort
        monkeypatch.setattr(
            runs_lifecycle_db,
            "get_latest_attempt",
            create_autospec(
                orig_get_latest,
                return_value={
                    "conversation_response_id": failed_run_id,
                    "turn_index": 1,
                    "status": "error",
                    "error_message": "transient_failure",
                    "metadata": {"automation_execution_id": EXEC_ID},
                },
            ),
        )
        resp_cp = await client.post(
            f"/api/v1/threads/{THREAD_ID}/retry",
            json={
                "workspace_id": WS,
                "llm_model": "claude-sonnet-4-6",
                "reasoning_effort": "high",
            },
        )
        assert resp_cp.status_code == 200
        # Round 8 Decision 1: web /retry ignores composer's reasoning_effort="high", uses execution snapshot "medium"
        assert resolve_seen[-1] == ("claude-sonnet-4-6", True, "medium")
        assert workflow_run_metas[-1] is not None
        assert "llm_model_system_only" not in workflow_run_metas[-1]
        assert workflow_run_metas[-1]["automation_source_execution_id"] == EXEC_ID

        # (2) Pending-input /retry (failed before graph checkpoint -> input_rerun_request)
        monkeypatch.setattr(
            runs_lifecycle_db,
            "get_latest_attempt",
            create_autospec(
                orig_get_latest,
                return_value={
                    "conversation_response_id": failed_run_id,
                    "turn_index": 1,
                    "status": "error",
                    "error_message": "transient_failure",
                    "metadata": {
                        "automation_execution_id": EXEC_ID,
                        pending_input_mod.PENDING_INPUT_KEY: {
                            "replay": False,
                            "checkpoint_id": None,
                            "resend": False,
                            "context": False,
                        },
                    },
                },
            ),
        )
        resp_pending = await client.post(
            f"/api/v1/threads/{THREAD_ID}/retry",
            json={
                "workspace_id": WS,
                "llm_model": "claude-sonnet-4-6",
                "reasoning_effort": "high",
            },
        )
        assert resp_pending.status_code == 200
        assert resolve_seen[-1] == ("claude-sonnet-4-6", True, "medium")
        assert workflow_run_metas[-1] is not None
        assert "llm_model_system_only" not in workflow_run_metas[-1]
        assert workflow_run_metas[-1]["automation_source_execution_id"] == EXEC_ID


@pytest.mark.asyncio
async def test_http_patch_automation_route_maintains_system_only_marker_to_final_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 5 (codex medium 5): HTTP PATCH /api/v1/automations/{id} routes through FastAPI,
    lifecycle.update_automation, and locked_row_reconciler all the way to auto_db.update_automation."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.dalton.notify import dispatch as notify_dispatch
    from src.server.app import automations as automations_app
    from src.server.dependencies import cli_approval, principal as principal_mod

    app = FastAPI()
    app.include_router(automations_app.router)
    app.dependency_overrides[principal_mod._WORKSPACE_SEND] = lambda: principal_mod.Principal(
        user_id=OWNER, auth="session"
    )

    owner_pref = {
        "custom_models": [
            {
                "name": "claude-sonnet-4-6",
                "provider": "openai",
                "model_id": "owner-shadow-no-efforts",
            },
            {
                "name": "owner-custom-only",
                "provider": "openai",
                "model_id": "owner-custom-id",
                "reasoning": {"efforts": ["low"], "default": "low"},
            },
        ],
    }
    _install_catalog_and_prefs(monkeypatch, owner_pref=owner_pref)
    monkeypatch.setattr("src.dalton.settings.is_dalton_enabled", lambda: False)
    monkeypatch.setattr("src.dalton.settings.is_coverage_units_enabled", lambda: False)
    monkeypatch.setattr(
        cli_approval,
        "automation_runs",
        create_autospec(cli_approval.automation_runs, return_value=None),
    )
    monkeypatch.setattr(
        lifecycle_mod,
        "check_claude_code_hosted",
        create_autospec(lifecycle_mod.check_claude_code_hosted, return_value=None),
    )
    monkeypatch.setattr(
        notify_dispatch,
        "populate_automation_notify_state",
        create_autospec(notify_dispatch.populate_automation_notify_state, return_value=None),
    )
    monkeypatch.setattr(
        "src.dalton.database.company_automations.resolve_automation_scope_for_write",
        create_autospec(
            __import__(
                "src.dalton.database.company_automations",
                fromlist=["resolve_automation_scope_for_write"],
            ).resolve_automation_scope_for_write,
            return_value="personal",
        ),
    )

    now = datetime.now(UTC)
    stored: dict[str, Any] = {
        "automation_id": AUTO_ID,
        "user_id": OWNER,
        "name": "dalton-nvda-watch",
        "description": "Watch",
        "trigger_type": "cron",
        "cron_expression": "0 9 * * 1-5",
        "timezone": "Asia/Shanghai",
        "trigger_config": {},
        "next_run_at": now,
        "last_run_at": None,
        "instruction": "Monitor NVDA.",
        "workspace_id": WS,
        "llm_model": "claude-sonnet-4-6",
        "reasoning_effort": "medium",
        "additional_context": None,
        "thread_strategy": "new",
        "conversation_thread_id": None,
        "status": "active",
        "max_failures": 3,
        "failure_count": 0,
        "delivery_config": {},
        "metadata": {"dalton_managed": True, "llm_model_system_only": True},
        "scope": "personal",
        "created_at": now,
        "updated_at": now,
    }
    final_writes: list[dict[str, Any]] = []

    async def _fake_get_auto(aid: str, uid: str, *, conn: Any = None):
        return dict(stored)

    async def _fake_update_auto(aid: str, uid: str, *, conn: Any = None, **kw: Any):
        effective = dict(kw)
        hist_ctx = company_automations.get_current_history_context()
        if hist_ctx is not None and hist_ctx.locked_row_reconciler is not None:
            await hist_ctx.locked_row_reconciler(dict(stored), effective, conn)
        final_writes.append(dict(effective))
        stored.update(effective)
        return dict(stored)

    monkeypatch.setattr(
        auto_db,
        "get_automation",
        create_autospec(auto_db.get_automation, side_effect=_fake_get_auto),
    )
    monkeypatch.setattr(
        auto_db,
        "update_automation",
        create_autospec(auto_db.update_automation, side_effect=_fake_update_auto),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # (1) Effort-only PATCH on shadowed system-only row validates against system catalog
        r1 = await client.patch(
            f"/api/v1/automations/{AUTO_ID}",
            json={"reasoning_effort": "high"},
        )
        assert r1.status_code == 200
        assert final_writes[-1]["reasoning_effort"] == "high"
        assert stored["metadata"].get("llm_model_system_only") is True

        # (2) Clearing llm_model=null clears llm_model AND removes llm_model_system_only in final write
        r2 = await client.patch(
            f"/api/v1/automations/{AUTO_ID}",
            json={"llm_model": None},
        )
        assert r2.status_code == 200
        assert final_writes[-1]["llm_model"] is None
        assert "llm_model_system_only" not in final_writes[-1]["metadata"]

        # (3) Client metadata cannot forge llm_model_system_only
        r3 = await client.patch(
            f"/api/v1/automations/{AUTO_ID}",
            json={"metadata": {"dalton_managed": True, "llm_model_system_only": True}},
        )
        assert r3.status_code == 200
        assert "llm_model_system_only" not in final_writes[-1]["metadata"]

        # (4) Switching explicitly from system-only to custom model removes llm_model_system_only
        stored["llm_model"] = "claude-sonnet-4-6"
        stored["metadata"] = {"dalton_managed": True, "llm_model_system_only": True}
        r4 = await client.patch(
            f"/api/v1/automations/{AUTO_ID}",
            json={"llm_model": "owner-custom-only"},
        )
        assert r4.status_code == 200
        assert final_writes[-1]["llm_model"] == "owner-custom-only"
        assert "llm_model_system_only" not in final_writes[-1]["metadata"]


# ---------------------------------------------------------------------------
# Round 5 tests: immutable execution-level automation_executions.llm_model_system_only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_patch_handoff_uses_latest_run_model_not_stored_thread_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thread stores 'gemini-2.5-pro', previous run belonged to a system-only execution using
    OpenAI ('gpt-5.4') that wrote an OpenAI native summary, and owner switches on web to
    same-named custom 'gpt-5.4' (or consecutively switches gemini -> claude).
    Web PATCH passes 'gpt-5.4' (previous run's actual model, NOT 'gemini-2.5-pro') to
    hand_off_on_switch, and _hand_off_config resolves the source model under system catalog
    via the execution row's llm_model_system_only=True."""
    from src.llms import provider_compaction as pc_mod
    from src.server.app.threads import crud as crud_mod
    from src.server.models.conversation import ThreadUpdateRequest
    from src.server.services import account as account_mod
    from src.server.services.llm import thread_model as thread_model_mod

    latest_run_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee43"
    latest_run = {
        "conversation_response_id": latest_run_id,
        "metadata": {"llm_model": "gpt-5.4"},
    }
    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(runs_lifecycle_db.get_latest_attempt, return_value=latest_run),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(
            atm_mod._get_execution_for_run_id,
            side_effect=lambda rid: (
                {"automation_execution_id": EXEC_ID, "llm_model_system_only": True}
                if str(rid) == latest_run_id
                else None
            ),
        ),
    )

    thread_state: dict[str, Any] = {
        "conversation_thread_id": THREAD_ID,
        "workspace_id": WS,
        "user_id": OWNER,
        "thread_index": 1,
        "current_status": "idle",
        "subagents_allowed": True,
        "llm_model": "gemini-2.5-pro",
        "metadata": {},
        "is_pinned": False,
        "archived": False,
        "is_shared": False,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    async def _fake_update_thread(tid: str, **kw: Any):
        if "llm_model" in kw:
            thread_state["llm_model"] = kw["llm_model"]
        if isinstance(kw.get("metadata_merge"), dict):
            thread_state["metadata"] = {
                **thread_state["metadata"],
                **kw["metadata_merge"],
            }
        return dict(thread_state)

    monkeypatch.setattr(
        crud_mod,
        "_authorize_thread_write",
        create_autospec(
            crud_mod._authorize_thread_write,
            return_value=SimpleNamespace(is_shared=False, org_id=None),
        ),
    )
    monkeypatch.setattr(
        crud_mod,
        "require_selectable_model",
        create_autospec(crud_mod.require_selectable_model, return_value=None),
    )
    monkeypatch.setattr(
        crud_mod,
        "get_thread_auth_meta",
        create_autospec(crud_mod.get_thread_auth_meta, side_effect=lambda _t: dict(thread_state)),
    )
    monkeypatch.setattr(
        crud_mod,
        "update_thread_fields",
        create_autospec(crud_mod.update_thread_fields, side_effect=_fake_update_thread),
    )
    monkeypatch.setattr(
        thread_model_mod,
        "next_turn_runs_claude_code",
        create_autospec(thread_model_mod.next_turn_runs_claude_code, return_value=False),
    )

    handoff_args: list[tuple[str, str, str | None]] = []
    handoff_resolve_calls: list[tuple[str | None, bool]] = []
    route = TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst")

    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())
    monkeypatch.setattr(
        account_mod,
        "own_access",
        create_autospec(account_mod.own_access, return_value=SimpleNamespace(api_keys=False)),
    )
    monkeypatch.setattr(
        maint_mod.TurnIdentity,
        "of",
        create_autospec(
            TurnIdentity.of,
            return_value=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        ),
    )
    monkeypatch.setattr(
        maint_mod,
        "thread_route",
        create_autospec(maint_mod.thread_route, side_effect=lambda _t, _u: (dict(thread_state), route)),
    )
    monkeypatch.setattr(
        maint_mod,
        "_summary_to_hand_off",
        create_autospec(maint_mod._summary_to_hand_off, return_value=SimpleNamespace(kind="openai_native")),
    )

    async def _resolve_llm(_base, _uid, model_name, _byok, **kw):
        ign = bool(kw.get("ignore_custom_models", False))
        handoff_resolve_calls.append((model_name, ign))
        endpoint = f"{model_name}:{'system' if ign else 'custom'}"
        return SimpleNamespace(get_llm_client=lambda _e=endpoint: _e)

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve_llm),
    )
    monkeypatch.setattr(
        pc_mod,
        "replays",
        create_autospec(pc_mod.replays, side_effect=lambda _s, client: client == "gpt-5.4:system"),
    )

    async def _run_real_handoff_config(
        tid: str,
        uid: str,
        previous_model: str | None,
        *,
        source_system_only: bool | None = None,
    ) -> bool:
        handoff_args.append((tid, uid, previous_model))
        cfg = await maint_mod._hand_off_config(
            tid, uid, previous_model, source_system_only=source_system_only
        )
        return cfg is not None

    monkeypatch.setattr(
        maint_mod,
        "hand_off_on_switch",
        create_autospec(maint_mod.hand_off_on_switch, side_effect=_run_real_handoff_config),
    )

    # First switch: thread had gemini-2.5-pro -> switch to same-named custom gpt-5.4
    patched_item = await crud_mod.update_thread_endpoint(
        THREAD_ID,
        ThreadUpdateRequest(llm_model="gpt-5.4"),
        x_user_id=OWNER,
    )
    assert handoff_args[-1] == (THREAD_ID, OWNER, "gpt-5.4")
    assert handoff_resolve_calls == [
        ("gpt-5.4", False),  # newly switched-to custom endpoint cannot read summary
        ("gpt-5.4", True),   # previous run's system OpenAI endpoint reads summary
    ]
    assert patched_item.summary_handoff is True

    # Consecutive second switch before any new turn (e.g. first handoff had failed/been skipped):
    # switching to claude-sonnet-4-6 still uses previous run's gpt-5.4 + execution system_only=True
    handoff_resolve_calls.clear()
    patched_item_2 = await crud_mod.update_thread_endpoint(
        THREAD_ID,
        ThreadUpdateRequest(llm_model="claude-sonnet-4-6"),
        x_user_id=OWNER,
    )
    assert handoff_args[-1] == (THREAD_ID, OWNER, "gpt-5.4")
    assert handoff_resolve_calls == [
        ("claude-sonnet-4-6", False),
        ("gpt-5.4", True),
    ]
    assert patched_item_2.summary_handoff is True


@pytest.mark.asyncio
@pytest.mark.parametrize("exec_system_only", [True, False], ids=["exec_true", "exec_false"])
async def test_all_continuation_paths_read_execution_system_only_column(
    monkeypatch: pytest.MonkeyPatch,
    exec_system_only: bool,
) -> None:
    """Round 5: every continuation path (HITL confirm, wakeup, command report, subagent task
    report, server retry, deploy restart resume via automation_delivery, and running_thread)
    reads automation_executions.llm_model_system_only directly and never writes
    llm_model_system_only into run_metadata."""
    from src.server.database import automation_executions as exec_db
    from src.server.services.llm import thread_model as thread_model_mod

    _install_catalog_and_prefs(monkeypatch, owner_pref={})
    monkeypatch.setattr(
        thread_model_mod,
        "model_resolves",
        create_autospec(thread_model_mod.model_resolves, return_value=True),
    )
    monkeypatch.setattr(
        atm_mod,
        "model_resolves",
        create_autospec(atm_mod.model_resolves, return_value=True),
    )

    exec_row = {
        "automation_execution_id": EXEC_ID,
        "automation_id": AUTO_ID,
        "status": "running",
        "conversation_thread_id": THREAD_ID,
        "user_id": OWNER,
        "llm_model_system_only": exec_system_only,
    }
    interrupt_run_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaa51"
    creator_run_id = "cccccccc-cccc-4ccc-8ccc-cccccccccc51"
    retry_run_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeee51"
    wakeup_id = "dddddddd-dddd-4ddd-8ddd-dddddddddd51"

    monkeypatch.setattr(
        atm_mod,
        "_get_hitl_interrupt_run_id",
        create_autospec(atm_mod._get_hitl_interrupt_run_id, return_value=interrupt_run_id),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_wakeup_creator_run_ids",
        create_autospec(atm_mod._get_wakeup_creator_run_ids, return_value=[creator_run_id]),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(atm_mod._get_execution_for_run_id, return_value=exec_row),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_by_id",
        create_autospec(atm_mod._get_execution_by_id, return_value=exec_row),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_command",
        create_autospec(atm_mod._get_execution_for_command, return_value=exec_row),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_task",
        create_autospec(atm_mod._get_execution_for_task, return_value=exec_row),
    )
    orig_get_running = exec_db.get_running_executions_for_thread
    monkeypatch.setattr(
        exec_db,
        "get_running_executions_for_thread",
        create_autospec(orig_get_running, return_value=[exec_row]),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_first_turn_settings",
        create_autospec(
            atm_mod._get_execution_first_turn_settings,
            return_value=("claude-sonnet-4-6", True, "high"),
        ),
    )

    cases: list[tuple[str, dict[str, Any]]] = [
        (
            "hitl_confirm",
            dict(
                request=SimpleNamespace(
                    query_type="resume",
                    platform="web",
                    llm_model="claude-sonnet-4-6",
                    reasoning_effort="high",
                    hitl_response={"approved": True},
                    checkpoint_id=None,
                ),
                requested_model="claude-sonnet-4-6",
                held_model="claude-sonnet-4-6",
                from_service=False,
                is_server_retry=False,
                retry_of_run_id=None,
            ),
        ),
        (
            "wakeup",
            dict(
                request=SimpleNamespace(
                    query_type="wakeup",
                    platform=None,
                    llm_model=None,
                    reasoning_effort=None,
                    hitl_response=None,
                    automation_delivery=None,
                    automation_earlier_runs=None,
                    report_source=None,
                    wakeup={"notices": [{"wakeup_id": wakeup_id}]},
                ),
                requested_model=None,
                held_model=None,
                from_service=True,
                is_server_retry=False,
                retry_of_run_id=None,
            ),
        ),
        (
            "command_report",
            dict(
                request=SimpleNamespace(
                    query_type="system",
                    platform=None,
                    llm_model=None,
                    reasoning_effort=None,
                    hitl_response=None,
                    automation_delivery=None,
                    automation_earlier_runs=None,
                    report_source={"kind": "command_report", "command_id": "bg-cmd-51"},
                ),
                requested_model=None,
                held_model=None,
                from_service=True,
                is_server_retry=False,
                retry_of_run_id=None,
            ),
        ),
        (
            "subagent_task_report",
            dict(
                request=SimpleNamespace(
                    query_type="system",
                    platform=None,
                    llm_model=None,
                    reasoning_effort=None,
                    hitl_response=None,
                    automation_delivery=None,
                    automation_earlier_runs=None,
                    report_source={"kind": "task", "task_id": "task-51", "task_run_id": None},
                ),
                requested_model=None,
                held_model=None,
                from_service=True,
                is_server_retry=False,
                retry_of_run_id=None,
            ),
        ),
        (
            "server_retry",
            dict(
                request=SimpleNamespace(
                    query_type="system",
                    platform=None,
                    llm_model=None,
                    reasoning_effort=None,
                    hitl_response=None,
                    automation_delivery=None,
                    automation_earlier_runs=None,
                    report_source=None,
                ),
                requested_model=None,
                held_model=None,
                from_service=True,
                is_server_retry=True,
                retry_of_run_id=retry_run_id,
            ),
        ),
        (
            "deploy_restart_resume",
            dict(
                request=SimpleNamespace(
                    query_type="system",
                    platform=None,
                    llm_model=None,
                    reasoning_effort=None,
                    hitl_response=None,
                    automation_delivery={"id": EXEC_ID},
                    automation_earlier_runs=None,
                    report_source=None,
                ),
                requested_model=None,
                held_model=None,
                from_service=True,
                is_server_retry=False,
                retry_of_run_id=None,
            ),
        ),
        (
            "running_thread",
            dict(
                request=SimpleNamespace(
                    query_type="system",
                    platform=None,
                    llm_model="claude-sonnet-4-6",
                    reasoning_effort="high",
                    hitl_response=None,
                    automation_delivery=None,
                    automation_earlier_runs=None,
                    report_source=None,
                ),
                requested_model="claude-sonnet-4-6",
                held_model=None,
                from_service=True,
                is_server_retry=False,
                retry_of_run_id=None,
            ),
        ),
    ]

    for label, kw in cases:
        seen_ign: list[bool] = []
        ctx_meta: dict[str, Any] = {}

        async def _resolve(
            model_name: str | None,
            *,
            reasoning_effort_override: Any = None,
            ignore_custom_models_override: bool | None = None,
            _log: list[bool] = seen_ign,
        ):
            _log.append(bool(ignore_custom_models_override))
            return SimpleNamespace(model=model_name)

        await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=OWNER,
            thread_id=THREAD_ID,
            resolve=_resolve,
            on_automation_context=ctx_meta.update,
            **kw,
        )
        assert seen_ign == [exec_system_only], f"{label}: expected ignore_custom={exec_system_only}, got {seen_ign}"
        assert "llm_model_system_only" not in ctx_meta, f"{label}: must not stamp llm_model_system_only into run_metadata"
        assert ctx_meta.get("automation_source_execution_id") == EXEC_ID, f"{label}: missing execution provenance"


@pytest.mark.asyncio
async def test_automation_config_edit_after_execution_start_does_not_affect_in_flight_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5: changing an automation's metadata.llm_model_system_only after execution creation
    does NOT change an in-flight execution's effective flag (both at first turn via
    AutomationExecutor._run_turn and across subsequent continuations)."""
    from src.server.services.llm import thread_model as thread_model_mod

    _install_catalog_and_prefs(monkeypatch, owner_pref={})
    monkeypatch.setattr(
        thread_model_mod,
        "model_resolves",
        create_autospec(thread_model_mod.model_resolves, return_value=True),
    )
    monkeypatch.setattr(
        atm_mod,
        "model_resolves",
        create_autospec(atm_mod.model_resolves, return_value=True),
    )

    orig_get_exec = atm_mod._get_execution_by_id
    orig_get_auto = auto_db.get_automation_including_deleted

    # Scenario 1: execution was created when llm_model_system_only=True, then automation was edited
    # to remove llm_model_system_only (metadata={}). Continuation MUST still enforce True.
    exec_started_true = {
        "automation_execution_id": EXEC_ID,
        "automation_id": AUTO_ID,
        "status": "running",
        "conversation_thread_id": THREAD_ID,
        "user_id": OWNER,
        "llm_model_system_only": True,
    }
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_by_id",
        create_autospec(orig_get_exec, return_value=exec_started_true),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_first_turn_settings",
        create_autospec(atm_mod._get_execution_first_turn_settings, return_value=(None, False, None)),
    )
    monkeypatch.setattr(
        auto_db,
        "get_automation_including_deleted",
        create_autospec(
            orig_get_auto,
            return_value={
                "automation_id": AUTO_ID,
                "user_id": OWNER,
                "llm_model": "claude-sonnet-4-6",
                "reasoning_effort": "medium",
                "metadata": {},  # edited AFTER execution started
            },
        ),
    )
    seen_1: list[bool] = []

    async def _resolve_1(model_name: str | None, *, reasoning_effort_override: Any = None, ignore_custom_models_override: bool | None = None):
        seen_1.append(bool(ignore_custom_models_override))
        return SimpleNamespace(model=model_name)

    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=SimpleNamespace(
            query_type="system",
            platform=None,
            llm_model=None,
            reasoning_effort=None,
            hitl_response=None,
            automation_delivery={"id": EXEC_ID},
            automation_earlier_runs=None,
            report_source=None,
        ),
        requested_model=None,
        held_model=None,
        from_service=True,
        resolve=_resolve_1,
    )
    assert seen_1 == [True]

    # Scenario 2: execution was created when llm_model_system_only=False, then PM edited automation
    # to set llm_model_system_only=True. In-flight execution MUST stay False.
    exec_started_false = {**exec_started_true, "llm_model_system_only": False}
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_by_id",
        create_autospec(orig_get_exec, return_value=exec_started_false),
    )
    monkeypatch.setattr(
        auto_db,
        "get_automation_including_deleted",
        create_autospec(
            orig_get_auto,
            return_value={
                "automation_id": AUTO_ID,
                "user_id": OWNER,
                "llm_model": "claude-sonnet-4-6",
                "reasoning_effort": "medium",
                "metadata": {"llm_model_system_only": True},  # edited AFTER execution started
            },
        ),
    )
    seen_2: list[bool] = []

    async def _resolve_2(model_name: str | None, *, reasoning_effort_override: Any = None, ignore_custom_models_override: bool | None = None):
        seen_2.append(bool(ignore_custom_models_override))
        return SimpleNamespace(model=model_name)

    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=SimpleNamespace(
            query_type="system",
            platform=None,
            llm_model=None,
            reasoning_effort=None,
            hitl_response=None,
            automation_delivery={"id": EXEC_ID},
            automation_earlier_runs=None,
            report_source=None,
        ),
        requested_model=None,
        held_model=None,
        from_service=True,
        resolve=_resolve_2,
    )
    assert seen_2 == [False]


@pytest.mark.asyncio
async def test_unlinked_continuations_and_user_started_reports_do_not_inherit_system_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5: when a continuation cannot be linked to an automation execution — including a
    user-started command report, user-started subagent task report, user-started wakeup, server
    retry of a non-automation run, or HITL resume of a non-automation run — it is treated as a
    normal owner turn (ignore_custom_models=False) even while a system-only automation is running
    on the same thread."""
    from src.server.database import automation_executions as exec_db
    from src.server.services.llm import thread_model as thread_model_mod

    _install_catalog_and_prefs(monkeypatch, owner_pref={})
    monkeypatch.setattr(
        thread_model_mod,
        "model_resolves",
        create_autospec(thread_model_mod.model_resolves, return_value=True),
    )

    unrelated_running_exec = {
        "automation_execution_id": EXEC_ID,
        "automation_id": AUTO_ID,
        "status": "running",
        "conversation_thread_id": THREAD_ID,
        "user_id": OWNER,
        "llm_model_system_only": True,
    }
    running_spy = create_autospec(
        exec_db.get_running_executions_for_thread,
        return_value=[unrelated_running_exec],
    )
    monkeypatch.setattr(exec_db, "get_running_executions_for_thread", running_spy)
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_command",
        create_autospec(atm_mod._get_execution_for_command, return_value=None),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_task",
        create_autospec(atm_mod._get_execution_for_task, return_value=None),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_wakeup_creator_run_ids",
        create_autospec(
            atm_mod._get_wakeup_creator_run_ids,
            return_value=["11111111-1111-4111-8111-111111111155"],
        ),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(atm_mod._get_execution_for_run_id, return_value=None),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_hitl_interrupt_run_id",
        create_autospec(
            atm_mod._get_hitl_interrupt_run_id,
            return_value="22222222-2222-4222-8222-222222222255",
        ),
    )

    unlinked_cases: list[tuple[str, dict[str, Any]]] = [
        (
            "user_command_report",
            dict(
                request=SimpleNamespace(
                    query_type="system",
                    platform=None,
                    llm_model=None,
                    reasoning_effort=None,
                    hitl_response=None,
                    automation_delivery=None,
                    automation_earlier_runs=None,
                    report_source={"kind": "command_report", "command_id": "user-cmd-1"},
                ),
                requested_model=None,
                held_model="owner-custom-only",
                from_service=True,
                is_server_retry=False,
                retry_of_run_id=None,
            ),
        ),
        (
            "user_subagent_report",
            dict(
                request=SimpleNamespace(
                    query_type="system",
                    platform=None,
                    llm_model=None,
                    reasoning_effort=None,
                    hitl_response=None,
                    automation_delivery=None,
                    automation_earlier_runs=None,
                    report_source={"kind": "task", "task_id": "user-task-1", "task_run_id": None},
                ),
                requested_model=None,
                held_model="owner-custom-only",
                from_service=True,
                is_server_retry=False,
                retry_of_run_id=None,
            ),
        ),
        (
            "user_wakeup",
            dict(
                request=SimpleNamespace(
                    query_type="wakeup",
                    platform=None,
                    llm_model=None,
                    reasoning_effort=None,
                    hitl_response=None,
                    automation_delivery=None,
                    automation_earlier_runs=None,
                    report_source=None,
                    wakeup={"notices": [{"wakeup_id": WAKEUP_ID}]},
                ),
                requested_model=None,
                held_model="owner-custom-only",
                from_service=True,
                is_server_retry=False,
                retry_of_run_id=None,
            ),
        ),
        (
            "server_retry_of_user_run",
            dict(
                request=SimpleNamespace(
                    query_type="system",
                    platform=None,
                    llm_model=None,
                    reasoning_effort=None,
                    hitl_response=None,
                    automation_delivery=None,
                    automation_earlier_runs=None,
                    report_source=None,
                ),
                requested_model=None,
                held_model="owner-custom-only",
                from_service=True,
                is_server_retry=True,
                retry_of_run_id="33333333-3333-4333-8333-333333333355",
            ),
        ),
        (
            "user_hitl_resume",
            dict(
                request=SimpleNamespace(
                    query_type="resume",
                    platform="web",
                    llm_model="owner-custom-only",
                    reasoning_effort=None,
                    hitl_response={"approved": True},
                    checkpoint_id=None,
                ),
                requested_model="owner-custom-only",
                held_model="owner-custom-only",
                from_service=False,
                is_server_retry=False,
                retry_of_run_id=None,
            ),
        ),
    ]

    for label, kw in unlinked_cases:
        seen_ign: list[bool] = []
        ctx_meta: dict[str, Any] = {}

        async def _resolve(
            model_name: str | None,
            *,
            reasoning_effort_override: Any = None,
            ignore_custom_models_override: bool | None = None,
            _log: list[bool] = seen_ign,
        ):
            _log.append(bool(ignore_custom_models_override))
            return SimpleNamespace(model=model_name)

        await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=OWNER,
            thread_id=THREAD_ID,
            resolve=_resolve,
            on_automation_context=ctx_meta.update,
            **kw,
        )
        assert seen_ign == [False], f"{label}: unlinked turn must NOT apply system-only"
        assert ctx_meta == {}, f"{label}: unlinked turn must NOT stamp automation context"
    running_spy.assert_not_awaited()


# ---------------------------------------------------------------------------
# Round 6 tests: full execution model-config snapshot & continuation/handoff invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snap_model", "snap_effort", "snap_sys", "fresh_model", "fresh_effort", "fresh_meta"),
    [
        (
            "claude-sonnet-4-6",
            "low",
            False,
            "gpt-6.2-sol",
            "high",
            {"llm_model_system_only": True},
        ),
        (
            "gpt-6.2-sol",
            "high",
            True,
            "owner-custom-only",
            "low",
            {},
        ),
    ],
    ids=["false_then_pm_edits_to_sys_true", "true_then_owner_edits_to_custom_false"],
)
async def test_r6_executor_uses_execution_snapshot_after_wait_and_stale_price_cache(
    monkeypatch: pytest.MonkeyPatch,
    snap_model: str,
    snap_effort: str,
    snap_sys: bool,
    fresh_model: str,
    fresh_effort: str,
    fresh_meta: dict[str, Any],
) -> None:
    """Round 6 Decision 1 (codex high 1):
    AutomationExecutor.execute uses the immutable (llm_model, reasoning_effort,
    llm_model_system_only) snapshot returned by begin_execution_if_not_deleted —
    even when (a) dispatched with a stale cached automation dict (price monitor)
    and (b) _wait_for_thread reloads a mutated fresh automation config."""
    from src.dalton.autonomy import preflight as dalton_preflight
    from src.server.database import automation_executions as exec_db
    from src.server.services import account as account_mod

    _install_catalog_and_prefs(monkeypatch, owner_pref={})

    started_row = {
        "conversation_thread_id": THREAD_ID,
        "conversation_response_id": None,
        "scheduled_at": datetime.now(UTC),
        "wait_reason": None,
        "awaits_computer": False,
        "checked_computer_id": None,
        "llm_model": snap_model,
        "reasoning_effort": snap_effort,
        "llm_model_system_only": snap_sys,
    }
    monkeypatch.setattr(
        exec_db,
        "begin_execution_if_not_deleted",
        create_autospec(exec_db.begin_execution_if_not_deleted, return_value=started_row),
    )
    monkeypatch.setattr(
        exec_mod,
        "late_note",
        create_autospec(exec_mod.late_note, return_value=None),
    )
    monkeypatch.setattr(
        exec_mod,
        "_keep_alive",
        create_autospec(exec_mod._keep_alive, return_value=None),
    )
    standing = SimpleNamespace(refuse_if_suspended=lambda: None)
    monkeypatch.setattr(
        account_mod,
        "read_standing",
        create_autospec(account_mod.read_standing, return_value=standing),
    )
    monkeypatch.setattr(
        exec_mod,
        "_thread_busy",
        create_autospec(exec_mod._thread_busy, return_value=True),
    )
    fresh_auto = {
        "automation_id": AUTO_ID,
        "user_id": OWNER,
        "name": "dalton-nvda-watch",
        "trigger_type": "price",
        "llm_model": fresh_model,
        "reasoning_effort": fresh_effort,
        "instruction": "Check NVDA.",
        "additional_context": None,
        "thread_strategy": "continue",
        "conversation_thread_id": THREAD_ID,
        "workspace_id": WS,
        "metadata": fresh_meta,
    }
    stale_cached_auto = {
        **fresh_auto,
        "llm_model": "stale-price-monitor-model",
        "reasoning_effort": "medium",
        "metadata": {"llm_model_system_only": not snap_sys},
    }
    route = TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst")
    monkeypatch.setattr(
        exec_mod,
        "_turn_place",
        create_autospec(
            exec_mod._turn_place,
            return_value=SimpleNamespace(workspace={"workspace_id": WS, "user_id": OWNER}),
        ),
    )
    monkeypatch.setattr(
        exec_mod,
        "turn_route",
        create_autospec(exec_mod.turn_route, return_value=route),
    )
    monkeypatch.setattr(
        TurnRoute,
        "bind",
        create_autospec(TurnRoute.bind, return_value=route),
    )
    monkeypatch.setattr(
        exec_mod,
        "park_for_computer",
        create_autospec(exec_mod.park_for_computer, return_value=False),
    )
    monkeypatch.setattr(
        dalton_preflight,
        "is_dalton_automation",
        create_autospec(dalton_preflight.is_dalton_automation, return_value=False),
    )
    monkeypatch.setattr(
        exec_mod,
        "_credentials",
        create_autospec(exec_mod._credentials, return_value=(False, False)),
    )
    monkeypatch.setattr(
        exec_mod,
        "enforce_credit_limit",
        create_autospec(exec_mod.enforce_credit_limit, return_value=None),
    )
    monkeypatch.setattr(
        exec_mod,
        "_verify_thread_matches_automation",
        create_autospec(exec_mod._verify_thread_matches_automation, return_value=None),
    )
    monkeypatch.setattr(
        exec_mod.TurnIdentity,
        "of",
        create_autospec(TurnIdentity.of, return_value=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER)),
    )

    captured: list[dict[str, Any]] = []

    async def _fake_astream(**kw: Any):
        captured.append(kw)
        if False:
            yield None

    monkeypatch.setattr(
        chat_handler_mod,
        "astream_ptc_workflow",
        create_autospec(chat_handler_mod.astream_ptc_workflow, side_effect=_fake_astream),
    )

    monkeypatch.setattr(
        exec_db,
        "transition_execution",
        create_autospec(exec_db.transition_execution, return_value=True),
    )

    executor = exec_mod.AutomationExecutor.__new__(exec_mod.AutomationExecutor)
    executor._wait_for_thread = create_autospec(executor._wait_for_thread, return_value=fresh_auto)
    executor._resolve_thread = create_autospec(executor._resolve_thread, return_value=THREAD_ID)
    executor._start_delivery = create_autospec(executor._start_delivery, return_value=None)
    executor._note_admission = create_autospec(executor._note_admission, return_value=True)
    executor._hand_to_run = create_autospec(executor._hand_to_run, return_value=True)

    await executor.execute(stale_cached_auto, EXEC_ID)
    assert len(captured) == 1
    req = captured[0]["request"]
    assert req.llm_model == snap_model
    assert req.reasoning_effort == snap_effort
    assert req.llm_model_system_only is snap_sys


@pytest.mark.asyncio
async def test_r6_hitl_resume_ignores_composer_model_effort_and_thread_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 6 Decision 2 (codex medium 5):
    When resuming an automation execution's HITL card, the server uses the execution row's
    (llm_model, reasoning_effort, llm_model_system_only) snapshot and completely ignores
    both the web composer's submitted llm_model/reasoning_effort and the thread's stored model."""
    from src.server.services.llm import thread_model as thread_model_mod

    _install_catalog_and_prefs(monkeypatch, owner_pref={})
    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())
    monkeypatch.setattr(
        thread_model_mod,
        "model_resolves",
        create_autospec(thread_model_mod.model_resolves, return_value=True),
    )
    monkeypatch.setattr(
        atm_mod,
        "model_resolves",
        create_autospec(atm_mod.model_resolves, return_value=True),
    )

    orig_get_exec = atm_mod._get_execution_for_run_id
    interrupt_run_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaa61"
    monkeypatch.setattr(
        atm_mod,
        "_get_hitl_interrupt_run_id",
        create_autospec(atm_mod._get_hitl_interrupt_run_id, return_value=interrupt_run_id),
    )

    resolve_calls: list[tuple[str | None, str | None, bool]] = []

    async def _fake_resolve(_base, _uid, model_name, _byok, **kw):
        resolve_calls.append(
            (model_name, kw.get("reasoning_effort"), bool(kw.get("ignore_custom_models", False)))
        )
        return SimpleNamespace(llm=SimpleNamespace(name=model_name), llm_client=object())

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, side_effect=_fake_resolve),
    )

    # Case 1: Execution snapshot names claude-sonnet-4-6 / high / system_only=True.
    # Owner switched thread to gpt-6.2-sol and web composer submits gpt-6.2-sol / low.
    exec_named = {
        "automation_execution_id": EXEC_ID,
        "automation_id": AUTO_ID,
        "status": "running",
        "conversation_thread_id": THREAD_ID,
        "user_id": OWNER,
        "llm_model": "claude-sonnet-4-6",
        "reasoning_effort": "high",
        "llm_model_system_only": True,
        "snapshot_taken": True,
    }
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(orig_get_exec, return_value=exec_named),
    )
    resolver_1 = thread_agent.thread_resolver(
        OWNER,
        THREAD_ID,
        TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
        identity=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        thread={"llm_model": "gpt-6.2-sol", "reasoning_effort": "low"},
        is_byok=False,
        reasoning_effort="low",
    )
    req_hitl_1 = ChatRequest(
        workspace_id=WS,
        messages=[],
        hitl_response={"int-1": {"decisions": [{"type": "approve"}]}},
        llm_model="gpt-6.2-sol",
        reasoning_effort="low",
    )
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=req_hitl_1,
        requested_model="gpt-6.2-sol",
        held_model="gpt-6.2-sol",
        from_service=False,
        resolve=resolver_1,
    )
    assert resolve_calls[-1] == ("claude-sonnet-4-6", "high", True)

    # Case 2: Execution snapshot has llm_model=None (account default), reasoning_effort=None, system_only=False.
    # Even when composer submits owner-custom-only and thread holds gpt-6.2-sol, HITL resume uses None (account default).
    exec_default = {
        **exec_named,
        "llm_model": None,
        "reasoning_effort": None,
        "llm_model_system_only": False,
    }
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(orig_get_exec, return_value=exec_default),
    )
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=req_hitl_1,
        requested_model="owner-custom-only",
        held_model="gpt-6.2-sol",
        from_service=False,
        resolve=resolver_1,
    )
    assert resolve_calls[-1] == (None, None, False)


@pytest.mark.asyncio
async def test_r6_server_retry_wakeup_falls_back_to_created_by_run_id_when_failed_run_unlinked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 6 Decision 3a (codex high 2):
    When a wakeup turn's first attempt failed before recording automation_source_execution_id,
    server /retry restores the original wakeup notices and MUST fall through to
    wakeups.created_by_run_id to recover the execution snapshot instead of returning early."""
    from src.server.services.llm import thread_model as thread_model_mod

    _install_catalog_and_prefs(monkeypatch, owner_pref={})
    monkeypatch.setattr(
        thread_model_mod,
        "model_resolves",
        create_autospec(thread_model_mod.model_resolves, return_value=True),
    )
    monkeypatch.setattr(
        atm_mod,
        "model_resolves",
        create_autospec(atm_mod.model_resolves, return_value=True),
    )

    failed_wakeup_run_id = "11111111-1111-4111-8111-111111111162"
    creator_run_id = "22222222-2222-4222-8222-222222222262"
    exec_row = {
        "automation_execution_id": EXEC_ID,
        "automation_id": AUTO_ID,
        "status": "running",
        "conversation_thread_id": THREAD_ID,
        "user_id": OWNER,
        "llm_model": "claude-sonnet-4-6",
        "reasoning_effort": "high",
        "llm_model_system_only": True,
        "snapshot_taken": True,
    }
    monkeypatch.setattr(
        atm_mod,
        "_get_wakeup_creator_run_ids",
        create_autospec(atm_mod._get_wakeup_creator_run_ids, return_value=[creator_run_id]),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(
            atm_mod._get_execution_for_run_id,
            side_effect=lambda rid: exec_row if str(rid) == creator_run_id else None,
        ),
    )

    seen: list[tuple[str | None, Any, bool]] = []
    ctx_meta: dict[str, Any] = {}

    async def _resolve(
        model_name: str | None,
        *,
        reasoning_effort_override: Any = None,
        ignore_custom_models_override: bool | None = None,
    ):
        seen.append((model_name, reasoning_effort_override, bool(ignore_custom_models_override)))
        return SimpleNamespace(model=model_name)

    retry_wakeup_req = SimpleNamespace(
        query_type="wakeup",
        platform=None,
        llm_model=None,
        reasoning_effort=None,
        hitl_response=None,
        automation_delivery=None,
        automation_earlier_runs=None,
        report_source=None,
        restart_from_run_id=None,
        wakeup={"notices": [{"wakeup_id": WAKEUP_ID}]},
    )
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=retry_wakeup_req,
        requested_model=None,
        held_model="owner-custom-only",
        from_service=True,
        is_server_retry=True,
        retry_of_run_id=failed_wakeup_run_id,
        resolve=_resolve,
        on_automation_context=ctx_meta.update,
    )
    assert seen == [("claude-sonnet-4-6", "high", True)]
    assert ctx_meta["automation_source_execution_id"] == EXEC_ID


@pytest.mark.asyncio
async def test_r6_restart_resume_carries_interrupted_run_id_and_isolates_user_turns_from_running_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 6 Decision 3c (codex medium 4):
    1) restart.resume_turn stamps restart_from_run_id onto the resume request body.
    2) _client_body strips client-forged restart_from_run_id.
    3) When restart_from_run_id names an ordinary user run, it does NOT borrow an unrelated
       running automation execution via running_thread; when it names an automation run, it
       resolves directly to that execution's snapshot."""
    from src.server.app.threads import messaging as msg_mod
    from src.server.database import automation_executions as exec_db
    from src.server.services.llm import thread_model as thread_model_mod
    from src.server.services.report_back import restart as restart_mod

    user_run_id = "33333333-3333-4333-8333-333333333363"
    auto_run_id = "44444444-4444-4444-8444-444444444463"

    # (1) resume_turn emits restart_from_run_id
    owed_user = restart_mod.OwedResume(
        user_id=OWNER,
        workspace_id=WS,
        interrupted=True,
        interrupted_run_id=user_run_id,
    )
    route_kind, body = restart_mod.resume_turn(owed_user, "req-key-1")
    assert route_kind == "messages"
    assert body["restart_from_run_id"] == user_run_id
    assert "automation_delivery" not in body

    # (2) Client request cannot forge restart_from_run_id
    forged = ChatRequest(
        workspace_id=WS,
        messages=[ChatMessage(role="user", content="hi")],
        restart_from_run_id=auto_run_id,
    )
    monkeypatch.setattr(msg_mod, "_is_service_request", create_autospec(msg_mod._is_service_request, return_value=False))
    stripped = msg_mod._client_body(forged, SimpleNamespace(headers={}))
    assert stripped.restart_from_run_id is None

    # (3) Ordinary user run restart_resume ignores unrelated running automation on the thread
    _install_catalog_and_prefs(monkeypatch, owner_pref={})
    monkeypatch.setattr(
        thread_model_mod,
        "model_resolves",
        create_autospec(thread_model_mod.model_resolves, return_value=True),
    )
    monkeypatch.setattr(
        atm_mod,
        "model_resolves",
        create_autospec(atm_mod.model_resolves, return_value=True),
    )
    exec_row = {
        "automation_execution_id": EXEC_ID,
        "automation_id": AUTO_ID,
        "status": "running",
        "conversation_thread_id": THREAD_ID,
        "user_id": OWNER,
        "llm_model": "claude-sonnet-4-6",
        "reasoning_effort": "high",
        "llm_model_system_only": True,
        "snapshot_taken": True,
    }
    running_spy = create_autospec(exec_db.get_running_executions_for_thread, return_value=[exec_row])
    monkeypatch.setattr(exec_db, "get_running_executions_for_thread", running_spy)
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(
            atm_mod._get_execution_for_run_id,
            side_effect=lambda rid: exec_row if str(rid) == auto_run_id else None,
        ),
    )

    seen_user: list[tuple[str | None, bool]] = []
    ctx_user: dict[str, Any] = {}

    async def _resolve_user(model_name: str | None, *, reasoning_effort_override: Any = None, ignore_custom_models_override: bool | None = None):
        seen_user.append((model_name, bool(ignore_custom_models_override)))
        return SimpleNamespace(model=model_name)

    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=ChatRequest(
            workspace_id=WS,
            messages=[ChatMessage(role="user", content="<system>paused</system>")],
            query_type="system",
            restart_from_run_id=user_run_id,
        ),
        requested_model=None,
        held_model="owner-custom-only",
        from_service=True,
        resolve=_resolve_user,
        on_automation_context=ctx_user.update,
    )
    assert seen_user == [("owner-custom-only", False)]
    assert ctx_user == {}
    running_spy.assert_not_awaited()

    # (4) Automation run restart_resume without automation_delivery still finds its execution via restart_from_run_id
    seen_auto: list[tuple[str | None, Any, bool]] = []
    ctx_auto: dict[str, Any] = {}

    async def _resolve_auto(model_name: str | None, *, reasoning_effort_override: Any = None, ignore_custom_models_override: bool | None = None):
        seen_auto.append((model_name, reasoning_effort_override, bool(ignore_custom_models_override)))
        return SimpleNamespace(model=model_name)

    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=ChatRequest(
            workspace_id=WS,
            messages=[ChatMessage(role="user", content="<system>paused</system>")],
            query_type="system",
            restart_from_run_id=auto_run_id,
        ),
        requested_model=None,
        held_model="owner-custom-only",
        from_service=True,
        resolve=_resolve_auto,
        on_automation_context=ctx_auto.update,
    )
    assert seen_auto == [("claude-sonnet-4-6", "high", True)]
    assert ctx_auto["automation_source_execution_id"] == EXEC_ID


@pytest.mark.asyncio
async def test_r6_handoff_uses_same_run_snapshot_across_concurrent_new_run_for_web_and_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 6 Decision 4 (codex medium 6):
    Both web PATCH handoff and deferred pending-handoff bind the old model name AND
    llm_model_system_only from the SAME run row's execution snapshot — even if a newer
    run with a different flag finishes before handoff resolves the old model."""
    from src.dalton.database import model_switch as dalton_ms_db
    from src.llms import provider_compaction as pc_mod
    from src.server.app.threads import crud as crud_mod
    from src.server.database.conversation import threads_read, threads_write
    from src.server.handlers import thread_model_handoff as handoff_mod
    from src.server.models.conversation import ThreadUpdateRequest
    from src.server.services import account as account_mod
    from src.server.services.llm import thread_model as thread_model_mod
    from src.server.services.thread_mutation import ThreadMutationRunner

    exec_sys_id = "55555555-5555-4555-8555-555555555564"
    run1_sys = {
        "conversation_response_id": "66666666-6666-4666-8666-666666666664",
        "metadata": {
            "llm_model": "gpt-5.4",
            "automation_execution_id": exec_sys_id,
        },
    }
    run2_owner = {
        "conversation_response_id": "77777777-7777-4777-8777-777777777764",
        "metadata": {"llm_model": "claude-sonnet-4-6"},
    }
    latest_box: dict[str, Any] = {"row": run1_sys}

    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(runs_lifecycle_db.get_latest_attempt, side_effect=lambda _t: dict(latest_box["row"])),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_by_id",
        create_autospec(
            atm_mod._get_execution_by_id,
            side_effect=lambda eid: (
                {"automation_execution_id": exec_sys_id, "llm_model": "gpt-5.4", "llm_model_system_only": True}
                if str(eid) == exec_sys_id
                else None
            ),
        ),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(atm_mod._get_execution_for_run_id, return_value=None),
    )

    thread_state: dict[str, Any] = {
        "conversation_thread_id": THREAD_ID,
        "workspace_id": WS,
        "user_id": OWNER,
        "thread_index": 1,
        "current_status": "idle",
        "subagents_allowed": True,
        "llm_model": "gpt-5.4",
        "metadata": {},
        "is_pinned": False,
        "archived": False,
        "is_shared": False,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    async def _update_thread(tid: str, **kw: Any):
        if "llm_model" in kw:
            thread_state["llm_model"] = kw["llm_model"]
        if isinstance(kw.get("metadata_merge"), dict):
            thread_state["metadata"] = {**thread_state["metadata"], **kw["metadata_merge"]}
        return dict(thread_state)

    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())
    monkeypatch.setattr(
        account_mod,
        "own_access",
        create_autospec(account_mod.own_access, return_value=SimpleNamespace(api_keys=False)),
    )
    monkeypatch.setattr(
        maint_mod.TurnIdentity,
        "of",
        create_autospec(TurnIdentity.of, return_value=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER)),
    )
    route = TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst")
    monkeypatch.setattr(
        maint_mod,
        "thread_route",
        create_autospec(maint_mod.thread_route, side_effect=lambda _t, _u: (dict(thread_state), route)),
    )
    monkeypatch.setattr(
        maint_mod,
        "_summary_to_hand_off",
        create_autospec(maint_mod._summary_to_hand_off, return_value=SimpleNamespace(kind="openai_native")),
    )
    monkeypatch.setattr(
        pc_mod,
        "replays",
        create_autospec(pc_mod.replays, side_effect=lambda _s, client: client == "gpt-5.4:system"),
    )

    resolve_log: list[tuple[str | None, bool]] = []

    async def _resolve_llm(_base, _uid, model_name, _byok, **kw):
        ign = bool(kw.get("ignore_custom_models", False))
        resolve_log.append((model_name, ign))
        endpoint = f"{model_name}:{'system' if ign else 'custom'}"
        return SimpleNamespace(get_llm_client=lambda _e=endpoint: _e)

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, side_effect=_resolve_llm),
    )

    # (a) Web PATCH: between crud.py reading run1_sys and _hand_off_config running, run2_owner finishes!
    monkeypatch.setattr(
        crud_mod,
        "_authorize_thread_write",
        create_autospec(crud_mod._authorize_thread_write, return_value=SimpleNamespace(is_shared=False, org_id=None)),
    )
    monkeypatch.setattr(
        crud_mod,
        "require_selectable_model",
        create_autospec(crud_mod.require_selectable_model, return_value=None),
    )
    monkeypatch.setattr(
        crud_mod,
        "get_thread_auth_meta",
        create_autospec(crud_mod.get_thread_auth_meta, side_effect=lambda _t: dict(thread_state)),
    )
    monkeypatch.setattr(
        crud_mod,
        "update_thread_fields",
        create_autospec(crud_mod.update_thread_fields, side_effect=_update_thread),
    )
    monkeypatch.setattr(
        thread_model_mod,
        "next_turn_runs_claude_code",
        create_autospec(thread_model_mod.next_turn_runs_claude_code, return_value=False),
    )

    async def _handoff_after_new_run(
        tid: str,
        uid: str,
        previous_model: str | None,
        *,
        source_system_only: bool | None = None,
        source_run_id: str | None = None,
        strict_system_only: bool = False,
    ) -> bool:
        # Simulate a brand-new non-system-only run completing right before _hand_off_config!
        latest_box["row"] = run2_owner
        cfg = await maint_mod._hand_off_config(
            tid,
            uid,
            previous_model,
            source_system_only=source_system_only,
            source_run_id=source_run_id,
            strict_system_only=strict_system_only,
        )
        return cfg is not None

    monkeypatch.setattr(
        maint_mod,
        "hand_off_on_switch",
        create_autospec(maint_mod.hand_off_on_switch, side_effect=_handoff_after_new_run),
    )

    patched = await crud_mod.update_thread_endpoint(
        THREAD_ID,
        ThreadUpdateRequest(llm_model="gpt-5.4"),
        x_user_id=OWNER,
    )
    assert patched.summary_handoff is True
    assert resolve_log == [
        ("gpt-5.4", False),  # target owner custom endpoint
        ("gpt-5.4", True),   # source uses run1_sys's True, NOT run2_owner's False!
    ]

    # (b) Deferred handoff: switch_thread_model(defer_handoff=True) saves candidate_sources=[("gpt-5.4", True)];
    #     even after run2_owner finishes before run_pending_model_handoff runs, inspect_hand_off still uses True!
    latest_box["row"] = run1_sys
    thread_state["metadata"] = {}
    thread_state["llm_model"] = "gpt-5.4"
    monkeypatch.setattr(
        thread_model_mod,
        "require_selectable_model",
        create_autospec(thread_model_mod.require_selectable_model, return_value=None),
    )
    monkeypatch.setattr(
        threads_read,
        "get_thread_auth_meta",
        create_autospec(threads_read.get_thread_auth_meta, side_effect=lambda _t: dict(thread_state)),
    )
    monkeypatch.setattr(
        threads_write,
        "update_thread_fields",
        create_autospec(threads_write.update_thread_fields, side_effect=_update_thread),
    )
    await thread_model_mod.switch_thread_model(
        OWNER,
        THREAD_ID,
        "gpt-5.4",
        defer_handoff=True,
        model_pref={},
        running_model="gpt-5.4",
        running_run_id=run1_sys["conversation_response_id"],
    )
    marker = thread_state["metadata"][thread_model_mod.PENDING_MODEL_HANDOFF_KEY]
    assert marker["candidate_sources"] == [
        {
            "model": "gpt-5.4",
            "system_only": True,
            "run_id": run1_sys["conversation_response_id"],
        }
    ]

    # Now run2_owner completes before run_pending_model_handoff runs!
    latest_box["row"] = run2_owner
    resolve_log.clear()
    inspect_calls = 0
    orig_inspect = handoff_mod.inspect_hand_off

    async def _real_inspect_then_done(
        tid: str,
        uid: str,
        model: str | None,
        *,
        source_system_only: bool | None = None,
        source_run_id: str | None = None,
    ):
        nonlocal inspect_calls
        inspect_calls += 1
        if inspect_calls == 1:
            return await orig_inspect(
                tid,
                uid,
                model,
                source_system_only=source_system_only,
                source_run_id=source_run_id,
            )
        return "not_needed", None

    monkeypatch.setattr(
        handoff_mod,
        "inspect_hand_off",
        create_autospec(orig_inspect, side_effect=_real_inspect_then_done),
    )
    fake_runner = create_autospec(ThreadMutationRunner, instance=True)
    fake_runner.wait_until_idle.return_value = True
    monkeypatch.setattr(ThreadMutationRunner, "get_instance", staticmethod(lambda: fake_runner))
    monkeypatch.setattr(
        dalton_ms_db,
        "clear_thread_metadata_key",
        create_autospec(dalton_ms_db.clear_thread_metadata_key, return_value=True),
    )

    outcome = await handoff_mod.run_pending_model_handoff(THREAD_ID, user_id=OWNER)
    assert outcome == "completed"
    # Both inspect_hand_off and _hand_off_config used the persisted system_only=True despite run2_owner!
    assert ("gpt-5.4", True) in resolve_log


@pytest.mark.asyncio
async def test_r6_create_execution_raises_when_parent_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 6 Decision 5a (codex low 7):
    create_execution raises ValueError when the parent automation does not exist (0 rows inserted)
    instead of returning an unpersisted UUID."""
    from src.server.database import automation_executions as exec_db

    conn = create_autospec(AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor(*_a: Any, **_kw: Any):
        cur = create_autospec(AsyncCursor, instance=True)
        cur.rowcount = 0
        yield cur

    conn.cursor.side_effect = _cursor

    @asynccontextmanager
    async def _get_conn(*_a: Any, **_kw: Any):
        yield conn

    monkeypatch.setattr(exec_db, "get_db_connection", _get_conn)
    with pytest.raises(ValueError, match="Automation not found"):
        await exec_db.create_execution(AUTO_ID, datetime.now(UTC), "srv-r6")


@pytest.mark.asyncio
async def test_r7_running_thread_false_records_execution_id_and_subsequent_wakeup_inherits_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 7 (grok R5 low 1):
    When a server continuation hits running_thread with llm_model_system_only=False, it still
    records automation_source_execution_id so a wakeup created by that continuation run traces
    back to the execution and inherits its full (llm_model, reasoning_effort, system_only=False)
    snapshot."""
    from src.server.database import automation_executions as exec_db
    from src.server.services.llm import thread_model as thread_model_mod

    cont_run_id = "11111111-1111-4111-8111-111111111171"
    wakeup_id = "22222222-2222-4222-8222-222222222271"
    exec_row = {
        "automation_execution_id": EXEC_ID,
        "automation_id": AUTO_ID,
        "status": "running",
        "conversation_thread_id": THREAD_ID,
        "user_id": OWNER,
        "llm_model": "gpt-6.2-sol",
        "reasoning_effort": "high",
        "llm_model_system_only": False,
        "snapshot_taken": True,
    }
    _install_catalog_and_prefs(monkeypatch, owner_pref={})
    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())
    monkeypatch.setattr(
        thread_model_mod,
        "model_resolves",
        create_autospec(thread_model_mod.model_resolves, return_value=True),
    )
    monkeypatch.setattr(
        atm_mod,
        "model_resolves",
        create_autospec(atm_mod.model_resolves, return_value=True),
    )
    orig_get_running = exec_db.get_running_executions_for_thread
    monkeypatch.setattr(
        exec_db,
        "get_running_executions_for_thread",
        create_autospec(orig_get_running, return_value=[exec_row]),
    )

    seen_calls: list[tuple[str | None, str | None, bool]] = []

    async def _fake_resolve(_base, _uid, model_name, _byok, **kw):
        seen_calls.append(
            (model_name, kw.get("reasoning_effort"), bool(kw.get("ignore_custom_models", False)))
        )
        return SimpleNamespace(llm=SimpleNamespace(name=model_name), llm_client=object())

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, side_effect=_fake_resolve),
    )

    # Step 1: running_thread continuation with llm_model_system_only=False stamps execution id
    cont_ctx: dict[str, Any] = {}
    resolver = thread_agent.thread_resolver(
        OWNER,
        THREAD_ID,
        TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
        identity=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        thread={"llm_model": "claude-sonnet-4-6", "reasoning_effort": "low"},
        is_byok=False,
    )
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=ChatRequest(
            workspace_id=WS,
            messages=[ChatMessage(role="user", content="bg continuation")],
            query_type="system",
        ),
        requested_model=None,
        held_model="claude-sonnet-4-6",
        from_service=True,
        resolve=resolver,
        on_automation_context=cont_ctx.update,
    )
    assert cont_ctx["automation_source_execution_id"] == EXEC_ID
    assert "automation_execution_id" not in cont_ctx
    # Round 8 Decision 1: running_thread also uses execution snapshot reasoning_effort ("high"), not thread's "low"
    assert seen_calls[-1] == ("gpt-6.2-sol", "high", False)

    # Step 2: subsequent wakeup created by cont_run_id finds EXEC_ID via created_by_run_id
    # even after the execution is no longer running on the thread, and inherits its full snapshot.
    monkeypatch.setattr(
        exec_db,
        "get_running_executions_for_thread",
        create_autospec(orig_get_running, return_value=[]),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_wakeup_creator_run_ids",
        create_autospec(atm_mod._get_wakeup_creator_run_ids, return_value=[cont_run_id]),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(
            atm_mod._get_execution_for_run_id,
            side_effect=lambda rid: exec_row if str(rid) == cont_run_id else None,
        ),
    )
    wakeup_ctx: dict[str, Any] = {}
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=ChatRequest(
            workspace_id=WS,
            messages=[ChatMessage(role="user", content="wakeup fired")],
            query_type="wakeup",
            wakeup={"notices": [{"wakeup_id": wakeup_id}]},
        ),
        requested_model=None,
        held_model="claude-sonnet-4-6",
        from_service=True,
        resolve=resolver,
        on_automation_context=wakeup_ctx.update,
    )
    assert wakeup_ctx["automation_source_execution_id"] == EXEC_ID
    assert seen_calls[-1] == ("gpt-6.2-sol", "high", False)


@pytest.mark.asyncio
async def test_r7_deferred_handoff_earlier_candidate_uses_its_own_run_snapshot_and_keeps_legacy_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 7 (grok R5 low 2):
    When a thread already holds a pending handoff candidate from an earlier system-only run
    ('claude-sonnet-4-6', system_only=True) and a later normal owner run ('gpt-6.2-sol',
    system_only=False) switches models again, deferred handoff resolves the earlier candidate
    with source_system_only=True (its own run's snapshot), the later candidate with
    source_system_only=False, and never drops legacy candidate_models lacking candidate_sources."""
    from src.dalton.database import model_switch as dalton_ms_db
    from src.server.handlers import thread_maintenance as maint_mod
    from src.server.handlers import thread_model_handoff as handoff_mod
    from src.server.services.llm import thread_model as thread_model_mod
    from src.server.services.thread_mutation import ThreadMutationRunner

    run2_owner = {
        "conversation_response_id": "33333333-3333-4333-8333-333333333372",
        "metadata": {"llm_model": "gpt-6.2-sol"},
    }
    existing_pending = thread_model_mod.build_pending_model_handoff_marker(
        previous_model="claude-sonnet-4-6",
        candidate_models=("legacy-older-model", "claude-sonnet-4-6"),
        candidate_sources=(("claude-sonnet-4-6", True),),
        version="v1",
        user_id=OWNER,
    )

    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(runs_lifecycle_db.get_latest_attempt, return_value=run2_owner),
    )
    monkeypatch.setattr(
        atm_mod,
        "get_run_model_and_system_only",
        create_autospec(
            atm_mod.get_run_model_and_system_only,
            return_value=("gpt-6.2-sol", False),
        ),
    )
    orig_get_meta = thread_model_mod.threads_read.get_thread_auth_meta
    monkeypatch.setattr(
        thread_model_mod.threads_read,
        "get_thread_auth_meta",
        create_autospec(
            orig_get_meta,
            return_value={
                "user_id": OWNER,
                "workspace_id": WS,
                "llm_model": "gpt-6.2-sol",
                "metadata": {thread_model_mod.PENDING_MODEL_HANDOFF_KEY: existing_pending},
            },
        ),
    )
    monkeypatch.setattr(
        thread_model_mod.user_models,
        "get_model_preference",
        create_autospec(thread_model_mod.user_models.get_model_preference, return_value={}),
    )
    monkeypatch.setattr(
        thread_model_mod,
        "model_resolves",
        create_autospec(thread_model_mod.model_resolves, return_value=True),
    )
    monkeypatch.setattr(
        thread_model_mod,
        "effective_default_model",
        create_autospec(thread_model_mod.effective_default_model, return_value="claude-sonnet-4-6"),
    )

    monkeypatch.setattr(
        thread_model_mod,
        "require_selectable_model",
        create_autospec(thread_model_mod.require_selectable_model, return_value=None),
    )

    plan = await thread_model_mod.prepare_thread_model_switch(
        THREAD_ID,
        OWNER,
        "gemini-2.5-pro",
        running_model="gpt-6.2-sol",
        running_run_id=run2_owner["conversation_response_id"],
    )
    assert ("claude-sonnet-4-6", True) in plan.pending_candidate_sources
    assert ("legacy-older-model", False) in plan.pending_candidate_sources
    assert ("gpt-6.2-sol", False) in plan.pending_candidate_sources
    assert "legacy-older-model" in plan.pending_candidate_models

    merged_marker = thread_model_mod.build_pending_model_handoff_marker(
        previous_model=plan.pending_previous_model,
        candidate_models=plan.pending_candidate_models,
        candidate_sources=plan.pending_candidate_sources,
        version="v2",
        user_id=OWNER,
    )
    monkeypatch.setattr(
        thread_model_mod.threads_read,
        "get_thread_auth_meta",
        create_autospec(
            orig_get_meta,
            return_value={
                "user_id": OWNER,
                "workspace_id": WS,
                "llm_model": "gemini-2.5-pro",
                "metadata": {thread_model_mod.PENDING_MODEL_HANDOFF_KEY: merged_marker},
            },
        ),
    )
    monkeypatch.setattr(
        thread_model_mod,
        "next_turn_runs_claude_code",
        create_autospec(thread_model_mod.next_turn_runs_claude_code, return_value=False),
    )

    inspected: list[tuple[str | None, bool | None]] = []
    handed_off: list[tuple[str | None, bool | None]] = []

    async def _fake_inspect(_tid, _uid, candidate, *, source_system_only=None):
        inspected.append((candidate, source_system_only))
        # Simulate that the summary was written by the last candidate so we observe
        # EVERY candidate's (model, source_system_only) pair in order before handoff.
        if candidate == "gpt-6.2-sol" and not handed_off:
            return "ready", None
        return "not_needed", None

    async def _fake_hand_off(_tid, _uid, source, *, source_system_only=None):
        handed_off.append((source, source_system_only))
        return True

    monkeypatch.setattr(
        handoff_mod,
        "inspect_hand_off",
        create_autospec(handoff_mod.inspect_hand_off, side_effect=_fake_inspect),
    )
    monkeypatch.setattr(
        maint_mod,
        "hand_off_on_switch",
        create_autospec(maint_mod.hand_off_on_switch, side_effect=_fake_hand_off),
    )
    fake_runner = create_autospec(ThreadMutationRunner, instance=True)
    fake_runner.wait_until_idle.return_value = True
    monkeypatch.setattr(ThreadMutationRunner, "get_instance", staticmethod(lambda: fake_runner))
    monkeypatch.setattr(
        dalton_ms_db,
        "clear_thread_metadata_key",
        create_autospec(dalton_ms_db.clear_thread_metadata_key, return_value=True),
    )

    outcome = await handoff_mod.run_pending_model_handoff(THREAD_ID, user_id=OWNER)
    assert outcome == "completed"
    # Earlier system-only candidate used True, legacy candidate without candidate_sources
    # resolved as non-system-only (False), and later normal candidate used False.
    assert inspected[:3] == [
        ("claude-sonnet-4-6", True),
        ("legacy-older-model", False),
        ("gpt-6.2-sol", False),
    ]
    assert handed_off == [("gpt-6.2-sol", False)]


def test_r7_d050_migration_sql_contains_backfill_and_downgrade() -> None:
    """Round 7 (grok R5 high): offline Alembic SQL generation for d046 -> d050 and d050 -> d046
    produces valid DDL + backfill UPDATE from earliest run metadata / automations config + DROP."""
    import io
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    project_root = Path(__file__).resolve().parents[3]

    def _render_sql(direction: str, rev_range: str) -> str:
        from contextlib import redirect_stdout

        buf = io.StringIO()
        # Omit config_file_name so env.py skips logging.config.fileConfig() (which disables existing loggers).
        cfg = Config(stdout=buf)
        cfg.set_main_option("script_location", str(project_root / "src" / "dalton" / "migrations"))
        cfg.set_main_option("sqlalchemy.url", "postgresql+psycopg://u:p@localhost/db")
        with redirect_stdout(buf):
            if direction == "upgrade":
                command.upgrade(cfg, rev_range, sql=True)
            else:
                command.downgrade(cfg, rev_range, sql=True)
        return buf.getvalue()

    up_sql = _render_sql("upgrade", "d046:d050")
    assert "ALTER TABLE automation_executions" in up_sql
    assert "ADD COLUMN IF NOT EXISTS llm_model TEXT" in up_sql
    assert "ADD COLUMN IF NOT EXISTS reasoning_effort VARCHAR(16)" in up_sql
    assert "ADD COLUMN IF NOT EXISTS llm_model_system_only BOOLEAN NOT NULL DEFAULT FALSE" in up_sql
    assert "ADD COLUMN IF NOT EXISTS snapshot_taken BOOLEAN NOT NULL DEFAULT FALSE" in up_sql
    assert "UPDATE automation_executions AS e" in up_sql
    assert "snapshot_taken = TRUE" in up_sql
    assert "FROM conversation_responses AS r" in up_sql
    assert "automation_source_execution_id" in up_sql
    assert "automation_delivery" in up_sql
    assert "ORDER BY r.created_at ASC, r.run_seq ASC" in up_sql
    assert "automation_reasoning_effort_configured" in up_sql
    assert "a.metadata->>'llm_model_system_only'" in up_sql

    down_sql = _render_sql("downgrade", "d050:d046")
    assert "DROP COLUMN IF EXISTS snapshot_taken" in down_sql
    assert "DROP COLUMN IF EXISTS llm_model_system_only" in down_sql
    assert "DROP COLUMN IF EXISTS reasoning_effort" in down_sql
    assert "DROP COLUMN IF EXISTS llm_model" in down_sql


@pytest.mark.asyncio
async def test_r8_linked_continuations_use_snapshot_and_null_model_inherits_account_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 8 Decision 1 (codex high 1):
    1) Web /retry linked to an execution ignores composer llm_model & reasoning_effort and uses snapshot.
    2) running_thread uses execution snapshot's model & reasoning_effort (ignoring thread settings).
    3) When execution snapshot has llm_model=None ('inherit account default'), server /retry, web /retry,
       and running_thread resolve with model=None (account default), NEVER the thread's stored model."""
    from src.server.database import automation_executions as exec_db
    from src.server.services.llm import thread_model as thread_model_mod

    _install_catalog_and_prefs(monkeypatch, owner_pref={"preferred_model": "gpt-6.2-sol"})
    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())
    monkeypatch.setattr(
        thread_model_mod,
        "model_resolves",
        create_autospec(thread_model_mod.model_resolves, return_value=True),
    )
    monkeypatch.setattr(
        atm_mod,
        "model_resolves",
        create_autospec(atm_mod.model_resolves, return_value=True),
    )

    seen: list[tuple[str | None, str | None, bool]] = []

    async def _fake_resolve(_base, _uid, model_name, _byok, **kw):
        seen.append(
            (model_name, kw.get("reasoning_effort"), bool(kw.get("ignore_custom_models", False)))
        )
        return SimpleNamespace(llm=SimpleNamespace(name=model_name or "account-default"), llm_client=object())

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, side_effect=_fake_resolve),
    )

    # (a) Web /retry ignores composer model + effort and uses execution snapshot
    exec_named = {
        "automation_execution_id": EXEC_ID,
        "automation_id": AUTO_ID,
        "status": "failed",
        "conversation_thread_id": THREAD_ID,
        "user_id": OWNER,
        "llm_model": "claude-sonnet-4-6",
        "reasoning_effort": "high",
        "llm_model_system_only": True,
        "snapshot_taken": True,
    }
    orig_exec_for_run = atm_mod._get_execution_for_run_id
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(orig_exec_for_run, return_value=exec_named),
    )
    resolver_web = thread_agent.thread_resolver(
        OWNER,
        THREAD_ID,
        TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
        identity=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        thread={"llm_model": "owner-thread-model", "reasoning_effort": "low"},
        is_byok=False,
        reasoning_effort="low",
    )
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=ChatRequest(
            workspace_id=WS,
            messages=[],
            llm_model="owner-composer-model",
            reasoning_effort="low",
        ),
        requested_model="owner-composer-model",
        held_model="owner-thread-model",
        from_service=False,
        is_server_retry=False,
        retry_of_run_id=RUN_ID,
        resolve=resolver_web,
    )
    assert seen[-1] == ("claude-sonnet-4-6", "high", True)

    # (b) running_thread uses execution snapshot model & effort (ignores thread effort & held model)
    orig_get_running = exec_db.get_running_executions_for_thread
    monkeypatch.setattr(
        exec_db,
        "get_running_executions_for_thread",
        create_autospec(orig_get_running, return_value=[exec_named]),
    )
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=ChatRequest(
            workspace_id=WS,
            messages=[ChatMessage(role="user", content="bg continuation")],
            query_type="system",
        ),
        requested_model=None,
        held_model="owner-thread-model",
        from_service=True,
        resolve=resolver_web,
    )
    assert seen[-1] == ("claude-sonnet-4-6", "high", True)

    # (c) Execution snapshot with llm_model=None ('inherit account default') resolves model=None,
    #     ignoring thread's held_model='owner-thread-model' and composer's 'owner-composer-model'.
    exec_default_model = {
        "automation_execution_id": EXEC_ID,
        "automation_id": AUTO_ID,
        "status": "running",
        "conversation_thread_id": THREAD_ID,
        "user_id": OWNER,
        "llm_model": None,
        "reasoning_effort": "medium",
        "llm_model_system_only": False,
        "snapshot_taken": True,
    }
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(orig_exec_for_run, return_value=exec_default_model),
    )
    monkeypatch.setattr(
        exec_db,
        "get_running_executions_for_thread",
        create_autospec(orig_get_running, return_value=[exec_default_model]),
    )

    # Server /retry with NULL snapshot model -> None (account default), NOT held_model
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=ChatRequest(workspace_id=WS, messages=[], query_type="wakeup"),
        requested_model=None,
        held_model="owner-thread-model",
        from_service=True,
        is_server_retry=True,
        retry_of_run_id=RUN_ID,
        resolve=resolver_web,
    )
    assert seen[-1] == (None, "medium", False)

    # Web /retry with NULL snapshot model -> None (account default), ignoring composer & held_model
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=ChatRequest(
            workspace_id=WS,
            messages=[],
            llm_model="owner-composer-model",
            reasoning_effort="low",
        ),
        requested_model="owner-composer-model",
        held_model="owner-thread-model",
        from_service=False,
        is_server_retry=False,
        retry_of_run_id=RUN_ID,
        resolve=resolver_web,
    )
    assert seen[-1] == (None, "medium", False)


@pytest.mark.asyncio
async def test_r8_rolling_upgrade_snapshot_taken_false_lazily_backfills_on_first_read_and_executor_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 8 Decision 2 (codex high 2):
    When an old backend inserts an execution row during a rolling upgrade (snapshot_taken=False,
    NULL/NULL/False), both continuation resolution and AutomationExecutor.execute lazily call
    ensure_execution_snapshot on first read and use the hydrated snapshot."""
    from src.dalton.autonomy import preflight as dalton_preflight
    from src.server.database import automation_executions as exec_db
    from src.server.services import account as account_mod
    from src.server.services.llm import thread_model as thread_model_mod

    _install_catalog_and_prefs(monkeypatch, owner_pref={})
    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())
    monkeypatch.setattr(
        thread_model_mod,
        "model_resolves",
        create_autospec(thread_model_mod.model_resolves, return_value=True),
    )
    monkeypatch.setattr(
        atm_mod,
        "model_resolves",
        create_autospec(atm_mod.model_resolves, return_value=True),
    )

    unhydrated_row = {
        "automation_execution_id": EXEC_ID,
        "automation_id": AUTO_ID,
        "status": "running",
        "conversation_thread_id": THREAD_ID,
        "user_id": OWNER,
        "llm_model": None,
        "reasoning_effort": None,
        "llm_model_system_only": False,
        "snapshot_taken": False,
    }
    hydrated_snap = {
        "llm_model": "claude-sonnet-4-6",
        "reasoning_effort": "high",
        "llm_model_system_only": True,
        "snapshot_taken": True,
    }
    ensure_spy = create_autospec(exec_db.ensure_execution_snapshot, return_value=hydrated_snap)
    monkeypatch.setattr(exec_db, "ensure_execution_snapshot", ensure_spy)
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(atm_mod._get_execution_for_run_id, return_value=unhydrated_row),
    )

    seen: list[tuple[str | None, str | None, bool]] = []

    async def _fake_resolve(_base, _uid, model_name, _byok, **kw):
        seen.append(
            (model_name, kw.get("reasoning_effort"), bool(kw.get("ignore_custom_models", False)))
        )
        return SimpleNamespace(llm=SimpleNamespace(name=model_name), llm_client=object())

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, side_effect=_fake_resolve),
    )
    resolver = thread_agent.thread_resolver(
        OWNER,
        THREAD_ID,
        TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
        identity=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        thread={"llm_model": "owner-thread-model", "reasoning_effort": "low"},
        is_byok=False,
    )
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=ChatRequest(workspace_id=WS, messages=[], query_type="wakeup"),
        requested_model=None,
        held_model="owner-thread-model",
        from_service=True,
        is_server_retry=True,
        retry_of_run_id=RUN_ID,
        resolve=resolver,
    )
    ensure_spy.assert_awaited_once_with(EXEC_ID)
    assert seen[-1] == ("claude-sonnet-4-6", "high", True)

    # (b) AutomationExecutor.execute also hydrates if begin_execution_if_not_deleted returns snapshot_taken=False
    ensure_spy.reset_mock()
    monkeypatch.setattr(
        exec_db,
        "begin_execution_if_not_deleted",
        create_autospec(
            exec_db.begin_execution_if_not_deleted,
            return_value={
                "conversation_thread_id": THREAD_ID,
                "conversation_response_id": None,
                "scheduled_at": datetime.now(UTC),
                "wait_reason": None,
                "awaits_computer": False,
                "checked_computer_id": None,
                "llm_model": None,
                "reasoning_effort": None,
                "llm_model_system_only": False,
                "snapshot_taken": False,
            },
        ),
    )
    monkeypatch.setattr(exec_db, "transition_execution", create_autospec(exec_db.transition_execution, return_value=True))
    monkeypatch.setattr(exec_mod, "late_note", create_autospec(exec_mod.late_note, return_value=None))
    monkeypatch.setattr(exec_mod, "_keep_alive", create_autospec(exec_mod._keep_alive, return_value=None))
    monkeypatch.setattr(
        account_mod,
        "read_standing",
        create_autospec(account_mod.read_standing, return_value=SimpleNamespace(refuse_if_suspended=lambda: None)),
    )
    monkeypatch.setattr(exec_mod, "_thread_busy", create_autospec(exec_mod._thread_busy, return_value=False))
    route = TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst")
    monkeypatch.setattr(
        exec_mod,
        "_turn_place",
        create_autospec(exec_mod._turn_place, return_value=SimpleNamespace(workspace={"workspace_id": WS, "user_id": OWNER})),
    )
    monkeypatch.setattr(exec_mod, "turn_route", create_autospec(exec_mod.turn_route, return_value=route))
    monkeypatch.setattr(TurnRoute, "bind", create_autospec(TurnRoute.bind, return_value=route))
    monkeypatch.setattr(exec_mod, "park_for_computer", create_autospec(exec_mod.park_for_computer, return_value=False))
    monkeypatch.setattr(dalton_preflight, "is_dalton_automation", create_autospec(dalton_preflight.is_dalton_automation, return_value=False))
    monkeypatch.setattr(exec_mod, "_credentials", create_autospec(exec_mod._credentials, return_value=(False, False)))
    monkeypatch.setattr(exec_mod, "enforce_credit_limit", create_autospec(exec_mod.enforce_credit_limit, return_value=None))
    monkeypatch.setattr(exec_mod, "_verify_thread_matches_automation", create_autospec(exec_mod._verify_thread_matches_automation, return_value=None))
    monkeypatch.setattr(
        exec_mod.TurnIdentity,
        "of",
        create_autospec(TurnIdentity.of, return_value=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER)),
    )
    dispatched: list[Any] = []

    async def _fake_astream(**kw: Any):
        dispatched.append(kw["request"])
        if False:
            yield None

    monkeypatch.setattr(
        chat_handler_mod,
        "astream_ptc_workflow",
        create_autospec(chat_handler_mod.astream_ptc_workflow, side_effect=_fake_astream),
    )
    executor = exec_mod.AutomationExecutor.__new__(exec_mod.AutomationExecutor)
    executor._resolve_thread = create_autospec(executor._resolve_thread, return_value=THREAD_ID)
    executor._start_delivery = create_autospec(executor._start_delivery, return_value=None)
    executor._note_admission = create_autospec(executor._note_admission, return_value=True)
    executor._hand_to_run = create_autospec(executor._hand_to_run, return_value=True)

    await executor.execute(
        {
            "automation_id": AUTO_ID,
            "user_id": OWNER,
            "name": "rolling-upgrade-auto",
            "trigger_type": "cron",
            "llm_model": "stale-cached",
            "reasoning_effort": "low",
            "instruction": "Check.",
            "additional_context": None,
            "thread_strategy": "continue",
            "conversation_thread_id": THREAD_ID,
            "workspace_id": WS,
            "metadata": {},
        },
        EXEC_ID,
    )
    ensure_spy.assert_awaited_once_with(EXEC_ID)
    assert len(dispatched) == 1
    assert dispatched[0].llm_model == "claude-sonnet-4-6"
    assert dispatched[0].reasoning_effort == "high"
    assert dispatched[0].llm_model_system_only is True


@pytest.mark.asyncio
async def test_r8_executor_run_turn_with_snapshot_skips_history_effort_override_but_clamps_antigravity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 8 Decision 3 (codex medium 3):
    When _Firing carries has_model_snapshot=True, _resolve_turn_reasoning_effort skips querying
    run history (_get_execution_first_turn_settings) so prior turns can never overwrite the
    snapshot's reasoning_effort, while built-in Antigravity models are still clamped to None."""
    from src.dalton.llm.antigravity_series import ANTIGRAVITY_PROVIDER

    history_spy = create_autospec(
        atm_mod._get_execution_first_turn_settings,
        return_value=("claude-sonnet-4-6", True, "low"),
    )
    monkeypatch.setattr(atm_mod, "_get_execution_first_turn_settings", history_spy)

    async def _classify(_uid, model_name, _pref=None, ignore_custom_models: bool = False):
        if model_name == "antigravity-pro":
            return user_models.ModelSource.SYSTEM, {"provider": ANTIGRAVITY_PROVIDER}
        return user_models.ModelSource.SYSTEM, {"provider": "anthropic"}

    monkeypatch.setattr(
        user_models,
        "classify_model",
        create_autospec(user_models.classify_model, side_effect=_classify),
    )

    executor = exec_mod.AutomationExecutor.__new__(exec_mod.AutomationExecutor)
    # Normal model with snapshot -> keeps snapshot effort "high" and never queries history
    effort_normal = await executor._resolve_turn_reasoning_effort(
        execution_id=EXEC_ID,
        thread_id=THREAD_ID,
        user_id=OWNER,
        llm_model="claude-sonnet-4-6",
        stored_effort="high",
        system_only=True,
        has_snapshot=True,
    )
    assert effort_normal == "high"
    history_spy.assert_not_awaited()

    # Built-in Antigravity model with snapshot -> clamped to None, still never queries history
    effort_ag = await executor._resolve_turn_reasoning_effort(
        execution_id=EXEC_ID,
        thread_id=THREAD_ID,
        user_id=OWNER,
        llm_model="antigravity-pro",
        stored_effort="high",
        system_only=True,
        has_snapshot=True,
    )
    assert effort_ag is None
    history_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_r8_legacy_pending_handoff_candidate_resolves_non_system_only_never_borrowing_newer_system_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 8 Decision 5 (codex medium 5):
    A legacy pending handoff marker with only candidate_models (no candidate_sources) must resolve
    as non-system-only (ignore_custom_models=False) even if a newer system-only automation run
    completed on the thread — preventing false not_needed and premature marker clearing."""
    from src.dalton.database import model_switch as dalton_ms_db
    from src.llms import provider_compaction as pc_mod
    from src.server.handlers import thread_maintenance as maint_mod
    from src.server.handlers import thread_model_handoff as handoff_mod
    from src.server.services.llm import thread_model as thread_model_mod
    from src.server.services.thread_mutation import ThreadMutationRunner

    newer_sys_run = {
        "conversation_response_id": "55555555-5555-4555-8555-555555555585",
        "metadata": {"llm_model": "claude-sonnet-4-6", "automation_execution_id": EXEC_ID},
    }
    legacy_marker = {
        "previous_model": "owner-custom-writer",
        "candidate_models": ["owner-custom-writer"],
        "user_id": OWNER,
        "version": "legacy-v1",
    }
    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(runs_lifecycle_db.get_latest_attempt, return_value=newer_sys_run),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_by_id",
        create_autospec(
            atm_mod._get_execution_by_id,
            return_value={
                "automation_execution_id": EXEC_ID,
                "automation_id": AUTO_ID,
                "llm_model": "claude-sonnet-4-6",
                "reasoning_effort": "medium",
                "llm_model_system_only": True,
                "snapshot_taken": True,
            },
        ),
    )
    monkeypatch.setattr(
        thread_model_mod.threads_read,
        "get_thread_auth_meta",
        create_autospec(
            thread_model_mod.threads_read.get_thread_auth_meta,
            return_value={
                "user_id": OWNER,
                "workspace_id": WS,
                "llm_model": "gemini-2.5-pro",
                "metadata": {thread_model_mod.PENDING_MODEL_HANDOFF_KEY: legacy_marker},
            },
        ),
    )
    monkeypatch.setattr(
        thread_model_mod,
        "next_turn_runs_claude_code",
        create_autospec(thread_model_mod.next_turn_runs_claude_code, return_value=False),
    )
    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())
    monkeypatch.setattr(
        maint_mod,
        "thread_route",
        create_autospec(
            maint_mod.thread_route,
            return_value=(
                {"conversation_thread_id": THREAD_ID, "workspace_id": WS, "llm_model": "gemini-2.5-pro"},
                TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
            ),
        ),
    )
    monkeypatch.setattr(
        "src.server.services.account.own_access",
        create_autospec(
            __import__("src.server.services.account", fromlist=["own_access"]).own_access,
            return_value=SimpleNamespace(api_keys=False),
        ),
    )
    monkeypatch.setattr(
        maint_mod.TurnIdentity,
        "of",
        create_autospec(TurnIdentity.of, return_value=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER)),
    )

    summary_state = {"present": True}
    monkeypatch.setattr(
        maint_mod,
        "_summary_to_hand_off",
        create_autospec(
            maint_mod._summary_to_hand_off,
            side_effect=lambda _tid: object() if summary_state["present"] else None,
        ),
    )
    monkeypatch.setattr(
        handoff_mod,
        "_has_pending_interrupt",
        create_autospec(handoff_mod._has_pending_interrupt, return_value=False),
    )
    monkeypatch.setattr(
        pc_mod,
        "replays",
        create_autospec(
            pc_mod.replays,
            side_effect=lambda _s, client: client.model == "owner-custom-writer" and not client.system_only,
        ),
    )

    resolve_log: list[tuple[str | None, bool]] = []

    async def _fake_resolve(_base, _uid, model_name, _byok, **kw):
        sys_only = bool(kw.get("ignore_custom_models", False))
        resolve_log.append((model_name, sys_only))
        if model_name == "owner-custom-writer" and sys_only:
            raise HTTPException(status_code=400, detail={"type": "model_not_found"})
        client = SimpleNamespace(model=model_name, system_only=sys_only)
        return SimpleNamespace(
            llm=SimpleNamespace(name=model_name),
            llm_client=client,
            get_llm_client=lambda c=client: c,
        )

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(llm_config_mod.resolve_llm_config, side_effect=_fake_resolve),
    )

    started_handoff: list[str | None] = []

    async def _fake_start_handoff(_tid, _uid, src_model, *, source_system_only=None):
        started_handoff.append(src_model)
        summary_state["present"] = False
        return True

    monkeypatch.setattr(
        maint_mod,
        "hand_off_on_switch",
        create_autospec(maint_mod.hand_off_on_switch, side_effect=_fake_start_handoff),
    )
    fake_runner = create_autospec(ThreadMutationRunner, instance=True)
    fake_runner.wait_until_idle.return_value = True
    monkeypatch.setattr(ThreadMutationRunner, "get_instance", staticmethod(lambda: fake_runner))
    monkeypatch.setattr(
        dalton_ms_db,
        "clear_thread_metadata_key",
        create_autospec(dalton_ms_db.clear_thread_metadata_key, return_value=True),
    )

    outcome = await handoff_mod.run_pending_model_handoff(THREAD_ID, user_id=OWNER)
    assert outcome == "completed"
    assert started_handoff == ["owner-custom-writer"]
    assert ("owner-custom-writer", False) in resolve_log
    assert ("owner-custom-writer", True) not in resolve_log


@pytest.mark.asyncio
async def test_r9_snapshot_hydration_failure_raises_retryable_error_without_resolving_or_writing_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 9 Decision 1 (codex high 1):
    When an unhydrated rolling-upgrade execution row (snapshot_taken=False) fails during
    ensure_execution_snapshot (e.g. pool timeout), continuation raises retryable 503
    automation_model_unavailable without invoking resolve or writing run metadata."""
    from src.server.database import automation_executions as exec_db

    unhydrated_row = {
        "automation_execution_id": EXEC_ID,
        "automation_id": AUTO_ID,
        "status": "running",
        "conversation_thread_id": THREAD_ID,
        "user_id": OWNER,
        "llm_model": None,
        "reasoning_effort": None,
        "llm_model_system_only": False,
        "snapshot_taken": False,
    }
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(atm_mod._get_execution_for_run_id, return_value=unhydrated_row),
    )
    monkeypatch.setattr(
        exec_db,
        "ensure_execution_snapshot",
        create_autospec(
            exec_db.ensure_execution_snapshot,
            side_effect=TimeoutError("DB pool checkout timed out"),
        ),
    )

    resolve_calls: list[Any] = []

    async def _fail_if_called(model_name: str | None, **_kw: Any):
        resolve_calls.append((model_name, _kw))
        raise AssertionError("resolve must not be called when snapshot hydration fails")

    ctx_meta: dict[str, Any] = {}
    with pytest.raises(HTTPException) as exc_info:
        await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=OWNER,
            thread_id=THREAD_ID,
            request=ChatRequest(
                workspace_id=WS,
                messages=[],
                llm_model="owner-composer-model",
                reasoning_effort="low",
            ),
            requested_model="owner-composer-model",
            held_model="owner-thread-model",
            from_service=False,
            is_server_retry=False,
            retry_of_run_id=RUN_ID,
            resolve=_fail_if_called,
            on_automation_context=ctx_meta.update,
        )
    assert exc_info.value.status_code == 503
    assert isinstance(exc_info.value.detail, dict)
    assert exc_info.value.detail.get("type") == "automation_model_unavailable"
    assert resolve_calls == []
    assert ctx_meta == {}


@pytest.mark.asyncio
async def test_r9_confirmed_execution_pref_timeout_keeps_snapshot_and_removed_model_refuses_or_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 9 Decision 2 (codex high 2):
    1) Web /retry linked to an execution (even llm_model_system_only=False) keeps the execution
       link and snapshot model when the first get_model_preference call times out — never falling
       back to the composer's submitted model.
    2) When the execution snapshot's pinned model is no longer in the catalog (model_resolves=False),
       continuation passes the pinned model through normal resolution (refusal on web /retry or
       existing subscription fallback on eligible server turns) instead of silently switching to
       the account default model."""
    from src.server.services.llm import thread_model as thread_model_mod

    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())

    exec_snap = {
        "automation_execution_id": EXEC_ID,
        "automation_id": AUTO_ID,
        "status": "failed",
        "conversation_thread_id": THREAD_ID,
        "user_id": OWNER,
        "llm_model": "claude-sonnet-4-6",
        "reasoning_effort": "high",
        "llm_model_system_only": False,
        "snapshot_taken": True,
    }
    orig_exec_for_run = atm_mod._get_execution_for_run_id
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(orig_exec_for_run, return_value=exec_snap),
    )

    # (a) First preference lookup times out during web /retry -> still resolves snapshot model
    pref_calls = 0

    async def _flaky_pref(_uid: str):
        nonlocal pref_calls
        pref_calls += 1
        if pref_calls == 1:
            raise TimeoutError("preference lookup timed out")
        return {"preferred_model": "gpt-6.2-sol"}

    orig_get_pref = user_models.get_model_preference
    monkeypatch.setattr(
        user_models,
        "get_model_preference",
        create_autospec(orig_get_pref, side_effect=_flaky_pref),
    )
    monkeypatch.setattr(
        atm_mod.user_models,
        "get_model_preference",
        user_models.get_model_preference,
    )

    seen_a: list[tuple[str | None, str | None, bool]] = []

    async def _fake_resolve_a(_base, _uid, model_name, _byok, **kw):
        seen_a.append(
            (model_name, kw.get("reasoning_effort"), bool(kw.get("ignore_custom_models", False)))
        )
        return SimpleNamespace(llm=SimpleNamespace(name=model_name), llm_client=object())

    orig_resolve_llm = llm_config_mod.resolve_llm_config
    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(orig_resolve_llm, side_effect=_fake_resolve_a),
    )
    resolver_web = thread_agent.thread_resolver(
        OWNER,
        THREAD_ID,
        TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
        identity=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        thread={"llm_model": "owner-thread-model", "reasoning_effort": "low"},
        is_byok=False,
        reasoning_effort="low",
    )
    ctx_a: dict[str, Any] = {}
    await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=ChatRequest(
            workspace_id=WS,
            messages=[],
            llm_model="owner-composer-model",
            reasoning_effort="low",
        ),
        requested_model="owner-composer-model",
        held_model="owner-thread-model",
        from_service=False,
        is_server_retry=False,
        retry_of_run_id=RUN_ID,
        resolve=resolver_web,
        on_automation_context=ctx_a.update,
    )
    assert ctx_a["automation_source_execution_id"] == EXEC_ID
    assert seen_a == [("claude-sonnet-4-6", "high", False)]

    # (b) Snapshot model removed from catalog -> web /retry raises standard model refusal (never account default!)
    exec_removed = {
        **exec_snap,
        "llm_model": "removed-pinned-model",
    }
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(orig_exec_for_run, return_value=exec_removed),
    )
    monkeypatch.setattr(
        user_models,
        "get_model_preference",
        create_autospec(orig_get_pref, return_value={"preferred_model": "gpt-6.2-sol", "fallback_models": ["gpt-6.2-sol"]}),
    )
    monkeypatch.setattr(atm_mod.user_models, "get_model_preference", user_models.get_model_preference)
    orig_model_resolves = atm_mod.model_resolves
    monkeypatch.setattr(
        atm_mod,
        "model_resolves",
        create_autospec(
            orig_model_resolves,
            side_effect=lambda _prefs, name: name != "removed-pinned-model",
        ),
    )
    monkeypatch.setattr(
        thread_model_mod,
        "model_resolves",
        create_autospec(
            thread_model_mod.model_resolves,
            side_effect=lambda _prefs, name: name != "removed-pinned-model",
        ),
    )

    seen_b: list[str | None] = []

    async def _fake_resolve_b(_base, _uid, model_name, _byok, **kw):
        seen_b.append(model_name)
        if model_name == "removed-pinned-model":
            raise HTTPException(
                status_code=400,
                detail={"type": "model_removed", "message": "Model removed from catalog."},
            )
        return SimpleNamespace(llm=SimpleNamespace(name=model_name), llm_client=object())

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(orig_resolve_llm, side_effect=_fake_resolve_b),
    )
    resolver_b = thread_agent.thread_resolver(
        OWNER,
        THREAD_ID,
        TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
        identity=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        thread={"llm_model": "owner-thread-model", "reasoning_effort": "low"},
        is_byok=False,
        reasoning_effort="low",
    )
    with pytest.raises(HTTPException) as exc_b:
        await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=OWNER,
            thread_id=THREAD_ID,
            request=ChatRequest(workspace_id=WS, messages=[], llm_model="owner-composer-model"),
            requested_model="owner-composer-model",
            held_model="owner-thread-model",
            from_service=False,
            is_server_retry=False,
            retry_of_run_id=RUN_ID,
            resolve=resolver_b,
        )
    assert exc_b.value.status_code == 400
    assert exc_b.value.detail["type"] == "model_removed"
    assert seen_b == ["removed-pinned-model"]

    # (c) On eligible server wakeup, subscription error on unavailable snapshot model enters
    #     existing subscription fallback (never silently switches to account default None).
    seen_c: list[str | None] = []

    async def _fake_resolve_c(_base, _uid, model_name, _byok, **kw):
        seen_c.append(model_name)
        if model_name == "removed-pinned-model":
            raise HTTPException(
                status_code=400,
                detail={"type": "oauth_reauth_required", "provider": "claude-oauth"},
            )
        return SimpleNamespace(llm=SimpleNamespace(name=model_name), llm_client=object())

    monkeypatch.setattr(
        llm_config_mod,
        "resolve_llm_config",
        create_autospec(orig_resolve_llm, side_effect=_fake_resolve_c),
    )
    resolver_c = thread_agent.thread_resolver(
        OWNER,
        THREAD_ID,
        TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
        identity=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        thread={"llm_model": "owner-thread-model", "reasoning_effort": "low"},
        is_byok=False,
        reasoning_effort="low",
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_wakeup_creator_run_ids",
        create_autospec(atm_mod._get_wakeup_creator_run_ids, return_value=[RUN_ID]),
    )
    monkeypatch.setattr(
        fb_mod,
        "subscription_route",
        create_autospec(fb_mod.subscription_route, return_value=None),
    )
    fake_mc = SimpleNamespace(
        llm_config=_SYSTEM_CATALOG,
        get_model_config=lambda m: _SYSTEM_CATALOG.get(m),
    )
    monkeypatch.setattr("src.llms.llm.LLM.get_model_config", lambda: fake_mc)
    fb_records: list[dict[str, Any]] = []
    cfg_c = await atm_mod.resolve_turn_config_with_automation_inheritance(
        user_id=OWNER,
        thread_id=THREAD_ID,
        request=ChatRequest(
            workspace_id=WS,
            messages=[ChatMessage(role="user", content="wakeup")],
            query_type="wakeup",
            wakeup={"notices": [{"wakeup_id": WAKEUP_ID}]},
        ),
        requested_model=None,
        held_model="owner-thread-model",
        from_service=True,
        resolve=resolver_c,
        on_fallback=fb_records.append,
    )
    assert seen_c == ["removed-pinned-model", "gpt-6.2-sol"]
    assert cfg_c.llm.name == "gpt-6.2-sol"
    assert len(fb_records) == 1


@pytest.mark.asyncio
async def test_r9_handoff_candidates_dedup_by_model_and_system_only_preserving_legacy_false_and_newer_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 9 Decision 3 (codex medium 3):
    Merging a legacy name-only pending candidate ('gpt-5.4', treated as False) with a newer
    same-name system-only candidate ('gpt-5.4', True) preserves BOTH ('gpt-5.4', False) and
    ('gpt-5.4', True) — both are inspected so handoff never falsely returns not_needed and
    clears the marker."""
    from src.dalton.database import model_switch as dalton_ms_db
    from src.server.handlers import thread_maintenance as maint_mod
    from src.server.handlers import thread_model_handoff as handoff_mod
    from src.server.services.llm import thread_model as thread_model_mod
    from src.server.services.thread_mutation import ThreadMutationRunner

    run_sys = {
        "conversation_response_id": "44444444-4444-4444-8444-444444444493",
        "metadata": {"llm_model": "gpt-5.4", "automation_execution_id": EXEC_ID},
    }
    legacy_existing = {
        "previous_model": "gpt-5.4",
        "candidate_models": ["gpt-5.4"],
        "user_id": OWNER,
        "version": "legacy-same-name",
    }

    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(runs_lifecycle_db.get_latest_attempt, return_value=run_sys),
    )
    monkeypatch.setattr(
        atm_mod,
        "get_run_model_and_system_only",
        create_autospec(atm_mod.get_run_model_and_system_only, return_value=("gpt-5.4", True)),
    )
    orig_get_meta = thread_model_mod.threads_read.get_thread_auth_meta
    monkeypatch.setattr(
        thread_model_mod.threads_read,
        "get_thread_auth_meta",
        create_autospec(
            orig_get_meta,
            return_value={
                "user_id": OWNER,
                "workspace_id": WS,
                "llm_model": "gpt-5.4",
                "metadata": {thread_model_mod.PENDING_MODEL_HANDOFF_KEY: legacy_existing},
            },
        ),
    )
    monkeypatch.setattr(
        thread_model_mod,
        "require_selectable_model",
        create_autospec(thread_model_mod.require_selectable_model, return_value=None),
    )

    plan = await thread_model_mod.prepare_thread_model_switch(
        OWNER,
        THREAD_ID,
        "gemini-2.5-pro",
        running_model="gpt-5.4",
        running_run_id=run_sys["conversation_response_id"],
    )
    assert plan.pending_candidate_sources == (("gpt-5.4", False), ("gpt-5.4", True))

    merged_marker = thread_model_mod.build_pending_model_handoff_marker(
        previous_model=plan.pending_previous_model,
        candidate_models=plan.pending_candidate_models,
        candidate_sources=plan.pending_candidate_sources,
        version="merged-v2",
        user_id=OWNER,
    )
    monkeypatch.setattr(
        thread_model_mod.threads_read,
        "get_thread_auth_meta",
        create_autospec(
            orig_get_meta,
            return_value={
                "user_id": OWNER,
                "workspace_id": WS,
                "llm_model": "gemini-2.5-pro",
                "metadata": {thread_model_mod.PENDING_MODEL_HANDOFF_KEY: merged_marker},
            },
        ),
    )
    monkeypatch.setattr(
        thread_model_mod,
        "next_turn_runs_claude_code",
        create_autospec(thread_model_mod.next_turn_runs_claude_code, return_value=False),
    )

    inspected: list[tuple[str | None, bool | None]] = []

    async def _fake_inspect(_tid, _uid, candidate, *, source_system_only=None):
        inspected.append((candidate, source_system_only))
        # Suppose the newer system-only gpt-5.4 wrote the summary (2nd candidate),
        # so the 1st candidate ('gpt-5.4', False) returns not_needed and 2nd returns ready!
        if (candidate, source_system_only) == ("gpt-5.4", True) and len(inspected) <= 2:
            return "ready", None
        return "not_needed", None

    handed_off: list[tuple[str | None, bool | None]] = []

    async def _fake_hand_off(_tid, _uid, source, *, source_system_only=None):
        handed_off.append((source, source_system_only))
        return True

    monkeypatch.setattr(
        handoff_mod,
        "inspect_hand_off",
        create_autospec(handoff_mod.inspect_hand_off, side_effect=_fake_inspect),
    )
    monkeypatch.setattr(
        maint_mod,
        "hand_off_on_switch",
        create_autospec(maint_mod.hand_off_on_switch, side_effect=_fake_hand_off),
    )
    fake_runner = create_autospec(ThreadMutationRunner, instance=True)
    fake_runner.wait_until_idle.return_value = True
    monkeypatch.setattr(ThreadMutationRunner, "get_instance", staticmethod(lambda: fake_runner))
    monkeypatch.setattr(
        dalton_ms_db,
        "clear_thread_metadata_key",
        create_autospec(dalton_ms_db.clear_thread_metadata_key, return_value=True),
    )

    outcome = await handoff_mod.run_pending_model_handoff(THREAD_ID, user_id=OWNER)
    assert outcome == "completed"
    assert inspected[:2] == [("gpt-5.4", False), ("gpt-5.4", True)]
    assert handed_off == [("gpt-5.4", True)]


@pytest.mark.asyncio
async def test_r9_running_model_reads_flag_by_running_run_id_not_previous_automation_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 9 Decision 4a (codex low 4, grok low 1):
    running_model's system-only flag is resolved strictly by running_run_id (not copied from
    an earlier automation run's prev_system_only); when running_run_id is missing or unreadable,
    running_model gets None so handoff re-reads."""
    from src.server.services.llm import thread_model as thread_model_mod

    auto_run_id = "11111111-1111-4111-8111-111111111194"
    owner_run_id = "22222222-2222-4222-8222-222222222294"
    auto_run = {
        "conversation_response_id": auto_run_id,
        "metadata": {"llm_model": "claude-sonnet-4-6", "automation_execution_id": EXEC_ID},
    }

    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(runs_lifecycle_db.get_latest_attempt, return_value=auto_run),
    )

    async def _run_identity(run_row: dict[str, Any] | None):
        rid = str((run_row or {}).get("conversation_response_id") or "")
        if rid == auto_run_id:
            return "claude-sonnet-4-6", True
        if rid == owner_run_id:
            return None, False
        return None, None

    monkeypatch.setattr(
        atm_mod,
        "get_run_model_and_system_only",
        create_autospec(atm_mod.get_run_model_and_system_only, side_effect=_run_identity),
    )
    monkeypatch.setattr(
        thread_model_mod.threads_read,
        "get_thread_auth_meta",
        create_autospec(
            thread_model_mod.threads_read.get_thread_auth_meta,
            return_value={
                "user_id": OWNER,
                "workspace_id": WS,
                "llm_model": "gpt-6.2-sol",
                "metadata": {},
            },
        ),
    )
    monkeypatch.setattr(
        thread_model_mod,
        "require_selectable_model",
        create_autospec(thread_model_mod.require_selectable_model, return_value=None),
    )

    # (a) With running_run_id belonging to owner run -> running_model gets False (NOT auto_run's True)
    plan_known = await thread_model_mod.prepare_thread_model_switch(
        OWNER,
        THREAD_ID,
        "gemini-2.5-pro",
        running_model="gpt-6.2-sol",
        running_run_id=owner_run_id,
    )
    assert plan_known.pending_candidate_sources == (
        ("claude-sonnet-4-6", True),
        ("gpt-6.2-sol", False),
    )

    # (b) Without running_run_id -> running_model gets None (re-read at handoff), never auto_run's True
    plan_unknown = await thread_model_mod.prepare_thread_model_switch(
        OWNER,
        THREAD_ID,
        "gemini-2.5-pro",
        running_model="gpt-6.2-sol",
    )
    assert plan_unknown.pending_candidate_sources == (
        ("claude-sonnet-4-6", True),
        ("gpt-6.2-sol", None),
    )


@pytest.mark.asyncio
async def test_r9_web_patch_read_failure_returns_none_and_lets_handoff_reread_or_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 9 Decision 4b (codex low 5, grok low 2):
    1) get_run_model_and_system_only returns (model, None) on execution lookup failure instead
       of swallowing the error as False.
    2) crud.update_thread_endpoint only sets prev_run_resolved=True when system_only is not None,
       so a failed read passes no source_system_only (None) to hand_off_on_switch for re-read.
    3) inspect_hand_off with source_system_only=None returns retryable_failure when DB lookup
       still fails (instead of not_needed)."""
    from src.server.app.threads import crud as crud_mod
    from src.server.database.conversation import threads_read, threads_write
    from src.server.handlers import thread_maintenance as maint_mod
    from src.server.handlers import thread_model_handoff as handoff_mod
    from src.server.models.conversation import ThreadUpdateRequest
    from src.server.services.llm import thread_model as thread_model_mod

    run_sys = {
        "conversation_response_id": "33333333-3333-4333-8333-333333333395",
        "metadata": {"llm_model": "gpt-5.4", "automation_execution_id": EXEC_ID},
    }
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_by_id",
        create_autospec(
            atm_mod._get_execution_by_id,
            side_effect=TimeoutError("execution read timed out"),
        ),
    )

    # (1) get_run_model_and_system_only returns (model, None) on failure
    model_name, sys_only = await atm_mod.get_run_model_and_system_only(run_sys)
    assert (model_name, sys_only) == ("gpt-5.4", None)

    # (2) crud.py does NOT pass source_system_only=False when execution lookup failed
    monkeypatch.setattr(
        crud_mod,
        "_authorize_thread_write",
        create_autospec(
            crud_mod._authorize_thread_write,
            return_value=SimpleNamespace(is_shared=False, org_id=None),
        ),
    )
    monkeypatch.setattr(
        crud_mod,
        "require_selectable_model",
        create_autospec(crud_mod.require_selectable_model, return_value=None),
    )
    monkeypatch.setattr(
        crud_mod,
        "get_thread_auth_meta",
        create_autospec(
            threads_read.get_thread_auth_meta,
            return_value={"workspace_id": WS, "llm_model": "claude-sonnet-4-6", "user_id": OWNER},
        ),
    )
    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(runs_lifecycle_db.get_latest_attempt, return_value=run_sys),
    )
    monkeypatch.setattr(
        crud_mod,
        "update_thread_fields",
        create_autospec(
            threads_write.update_thread_fields,
            return_value={
                "conversation_thread_id": THREAD_ID,
                "workspace_id": WS,
                "user_id": OWNER,
                "thread_index": 1,
                "title": "t",
                "current_status": "idle",
                "subagents_allowed": True,
                "msg_type": "ptc",
                "llm_model": "gemini-2.5-pro",
                "is_pinned": False,
                "archived": False,
                "is_shared": False,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            },
        ),
    )
    monkeypatch.setattr(
        thread_model_mod,
        "next_turn_runs_claude_code",
        create_autospec(thread_model_mod.next_turn_runs_claude_code, return_value=False),
    )

    handoff_kwargs: list[dict[str, Any]] = []

    async def _capture_handoff(tid: str, uid: str, prev_model: str | None, **kw: Any) -> bool:
        handoff_kwargs.append({"prev_model": prev_model, **kw})
        return True

    monkeypatch.setattr(
        maint_mod,
        "hand_off_on_switch",
        create_autospec(maint_mod.hand_off_on_switch, side_effect=_capture_handoff),
    )
    await crud_mod.update_thread_endpoint(
        THREAD_ID,
        ThreadUpdateRequest(llm_model="gemini-2.5-pro"),
        x_user_id=OWNER,
    )
    assert handoff_kwargs == [
        {
            "prev_model": "gpt-5.4",
            "strict_system_only": True,
            "source_run_id": run_sys["conversation_response_id"],
        }
    ]
    assert "source_system_only" not in handoff_kwargs[0]

    # (3) inspect_hand_off with source_system_only=None returns retryable_failure when DB read still fails
    monkeypatch.setattr(
        maint_mod,
        "_summary_to_hand_off",
        create_autospec(maint_mod._summary_to_hand_off, return_value=object()),
    )
    monkeypatch.setattr(
        maint_mod,
        "thread_route",
        create_autospec(
            maint_mod.thread_route,
            return_value=(
                {"conversation_thread_id": THREAD_ID, "workspace_id": WS, "llm_model": "gemini-2.5-pro"},
                TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
            ),
        ),
    )
    status, payload = await handoff_mod.inspect_hand_off(THREAD_ID, OWNER, "gpt-5.4")
    assert (status, payload) == ("retryable_failure", None)


# =========================================================================
# Round 10 (narrowed) unit tests
# =========================================================================


@pytest.mark.asyncio
async def test_r10_web_retry_and_explicit_execution_lookup_timeout_fail_closed_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 10 Decision 1 (codex high 1, grok suspect 2):
    1) HTTP POST /api/v1/threads/{id}/retry on a failed run carrying automation_execution_id
       fails closed with HTTP 503 (automation_model_unavailable) when _get_execution_for_run_id
       times out — never starting the workflow or falling back to composer model/effort.
    2) Explicit server turn carrying automation_execution_id fails closed with HTTP 503 when
       _get_execution_by_id times out — never calling resolve or on_automation_context."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.server.app.threads import messaging as msg_mod
    from src.server.dependencies import cli_approval, principal as principal_mod, usage_limits
    from src.server.handlers import chat as chat_handlers, checkpoint_handler
    from src.server.services import turn_runtime
    from src.server.services.runs import pending_input as pending_input_mod
    from src.server.services.shared_workspaces.turn import TurnIdentity as SharedTurnIdentity

    app = FastAPI()
    app.include_router(msg_mod.router)

    async def _fake_limit(uid: str):
        return usage_limits.ChatAuthResult(
            user_id=uid,
            is_byok=False,
            has_oauth=False,
            access_tier=1,
            burst_slot_id="slot-r10",
            own_subscriptions=False,
        )

    app.dependency_overrides[principal_mod._RUN] = lambda: principal_mod.Principal(
        user_id=OWNER, auth="session"
    )
    app.dependency_overrides[usage_limits.chat_limiter] = lambda: _fake_limit

    monkeypatch.setattr("src.config.settings.HOST_MODE", "oss")
    monkeypatch.setattr("src.dalton.auth.is_dalton_supabase_auth_enabled", lambda: False)
    monkeypatch.setattr("src.dalton.settings.get_dalton_auth_mode", lambda: "oss")
    monkeypatch.setattr("src.server.app.setup.agent_config", SimpleNamespace())
    monkeypatch.setattr(
        cli_approval,
        "admit_retry",
        create_autospec(cli_approval.admit_retry, return_value=None),
    )
    monkeypatch.setattr(
        msg_mod,
        "_assert_stream_transport_ready",
        create_autospec(msg_mod._assert_stream_transport_ready, return_value=None),
    )
    monkeypatch.setattr(
        msg_mod,
        "get_thread_auth_meta",
        create_autospec(
            msg_mod.get_thread_auth_meta,
            return_value={"user_id": OWNER, "workspace_id": WS, "llm_model": "claude-sonnet-4-6"},
        ),
    )
    monkeypatch.setattr(
        msg_mod,
        "_settle_against_branch",
        create_autospec(msg_mod._settle_against_branch, side_effect=lambda _t, req, _u: req),
    )
    monkeypatch.setattr(
        "src.dalton.sharing.resolve_colleague_turn_admission",
        create_autospec(
            __import__(
                "src.dalton.sharing", fromlist=["resolve_colleague_turn_admission"]
            ).resolve_colleague_turn_admission,
            return_value=({"workspace_id": WS, "user_id": OWNER, "computer_id": COMP_1}, "web", False),
        ),
    )
    route = TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst")
    monkeypatch.setattr(
        turn_runtime,
        "turn_route",
        create_autospec(turn_runtime.turn_route, return_value=route),
    )
    monkeypatch.setattr(
        TurnRoute,
        "bind",
        create_autospec(TurnRoute.bind, return_value=route),
    )
    monkeypatch.setattr(
        SharedTurnIdentity,
        "of",
        create_autospec(
            SharedTurnIdentity.of,
            return_value=SharedTurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        ),
    )
    monkeypatch.setattr(
        usage_limits,
        "release_burst_slot",
        create_autospec(usage_limits.release_burst_slot, return_value=None),
    )
    monkeypatch.setattr(
        checkpoint_handler,
        "get_retry_checkpoint",
        create_autospec(checkpoint_handler.get_retry_checkpoint, return_value="cp-r10-1"),
    )
    monkeypatch.setattr(
        pending_input_mod,
        "retried_wake_fields",
        create_autospec(pending_input_mod.retried_wake_fields, return_value={}),
    )

    failed_run_id = "99999999-9999-4999-8999-999999999910"
    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(
            runs_lifecycle_db.get_latest_attempt,
            return_value={
                "conversation_response_id": failed_run_id,
                "turn_index": 1,
                "status": "error",
                "error_message": "transient_failure",
                "metadata": {"automation_execution_id": EXEC_ID},
            },
        ),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_for_run_id",
        create_autospec(
            atm_mod._get_execution_for_run_id,
            side_effect=TimeoutError("db pool timeout on execution lookup"),
        ),
    )
    resolve_spy = create_autospec(llm_config_mod.resolve_llm_config)
    monkeypatch.setattr(llm_config_mod, "resolve_llm_config", resolve_spy)
    workflow_spy = create_autospec(chat_handlers.astream_ptc_workflow)
    monkeypatch.setattr(chat_handlers, "astream_ptc_workflow", workflow_spy)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/threads/{THREAD_ID}/retry",
            json={
                "workspace_id": WS,
                "llm_model": "owner-composer-model",
                "reasoning_effort": "high",
            },
        )
    assert resp.status_code == 503
    assert resp.json()["detail"]["type"] == "automation_model_unavailable"
    resolve_spy.assert_not_awaited()
    workflow_spy.assert_not_called()

    # (2) Explicit server turn carrying automation_execution_id also fails closed with 503 on lookup timeout
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_by_id",
        create_autospec(
            atm_mod._get_execution_by_id,
            side_effect=TimeoutError("db timeout reading explicit execution"),
        ),
    )
    resolve_called: list[str | None] = []

    async def _never_resolve(model_name: str | None, **_kw: Any):
        resolve_called.append(model_name)
        return SimpleNamespace()

    ctx_captured: list[dict[str, Any]] = []
    explicit_req = SimpleNamespace(
        workspace_id=WS,
        messages=[],
        query_type="system",
        platform=None,
        hitl_response=None,
        automation_execution_id=EXEC_ID,
    )
    with pytest.raises(HTTPException) as exc_srv:
        await atm_mod.resolve_turn_config_with_automation_inheritance(
            user_id=OWNER,
            thread_id=THREAD_ID,
            request=explicit_req,
            requested_model=None,
            held_model="owner-thread-model",
            from_service=True,
            resolve=_never_resolve,
            on_automation_context=ctx_captured.append,
        )
    assert exc_srv.value.status_code == 503
    assert exc_srv.value.detail["type"] == "automation_model_unavailable"
    assert resolve_called == []
    assert ctx_captured == []


@pytest.mark.asyncio
async def test_r10_deferred_handoff_rereads_exact_candidate_run_id_never_borrowing_newer_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 10 Decision 2 (codex medium 2, grok medium 1):
    When switch-time lookup for system-only run r1 times out, pending marker stores
    (model, system_only=None) with run_id=r1. Even after a newer ordinary run r2 (False)
    is appended to the thread:
    1) Deferred handoff re-reads r1 specifically (never borrowing r2's False) and resolves
       the candidate under system-only catalog (ignore_custom_models=True).
    2) If re-reading r1 still times out, inspect_hand_off returns retryable_failure and
       run_pending_model_handoff keeps the pending marker intact."""
    from src.dalton.database import model_switch as dalton_ms_db
    from src.llms import provider_compaction as pc_mod
    from src.server.handlers import thread_maintenance as maint_mod
    from src.server.handlers import thread_model_handoff as handoff_mod
    from src.server.services import account as account_mod
    from src.server.services import thread_agent as ta_mod
    from src.server.services.llm import thread_model as thread_model_mod

    run1_sys_id = "33333333-3333-4333-8333-3333333333a1"
    run2_normal_id = "33333333-3333-4333-8333-3333333333a2"
    run1_row = {
        "conversation_response_id": run1_sys_id,
        "metadata": {"llm_model": "claude-sonnet-4-6", "automation_execution_id": EXEC_ID},
    }
    run2_row = {
        "conversation_response_id": run2_normal_id,
        "metadata": {"llm_model": "gpt-6.2-sol"},
    }

    orig_get_latest = runs_lifecycle_db.get_latest_attempt
    orig_get_run_sys = atm_mod.get_run_model_and_system_only
    orig_get_meta = thread_model_mod.threads_read.get_thread_auth_meta

    # Switch happens right after r1 while r1's execution lookup times out -> (model, None) + run_id=r1
    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(orig_get_latest, return_value=run1_row),
    )
    monkeypatch.setattr(
        atm_mod,
        "get_run_model_and_system_only",
        create_autospec(
            orig_get_run_sys,
            return_value=("claude-sonnet-4-6", None),
        ),
    )
    monkeypatch.setattr(
        thread_model_mod,
        "require_selectable_model",
        create_autospec(thread_model_mod.require_selectable_model, return_value=None),
    )
    monkeypatch.setattr(
        thread_model_mod.threads_read,
        "get_thread_auth_meta",
        create_autospec(
            orig_get_meta,
            return_value={
                "user_id": OWNER,
                "workspace_id": WS,
                "llm_model": "claude-sonnet-4-6",
                "metadata": {},
            },
        ),
    )
    plan = await thread_model_mod.prepare_thread_model_switch(
        OWNER,
        THREAD_ID,
        "claude-sonnet-4-6",
    )
    assert plan.changed is True
    assert plan.pending_candidate_sources == (("claude-sonnet-4-6", None),)
    assert plan.pending_candidate_source_run_ids == (run1_sys_id,)

    marker = thread_model_mod.build_pending_model_handoff_marker(
        previous_model=plan.pending_previous_model,
        user_id=OWNER,
        version="ver-r10-rid",
        candidate_models=plan.pending_candidate_models,
        candidate_sources=plan.pending_candidate_sources,
        candidate_source_run_ids=plan.pending_candidate_source_run_ids,
    )
    assert marker["candidate_sources"] == [
        {"model": "claude-sonnet-4-6", "system_only": None, "run_id": run1_sys_id}
    ]

    # Later, a newer normal run r2 becomes the thread's latest attempt
    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(orig_get_latest, return_value=run2_row),
    )
    monkeypatch.setattr(
        thread_model_mod.threads_read,
        "get_thread_auth_meta",
        create_autospec(
            orig_get_meta,
            return_value={
                "user_id": OWNER,
                "workspace_id": WS,
                "llm_model": "claude-sonnet-4-6",
                "metadata": {thread_model_mod.PENDING_MODEL_HANDOFF_KEY: marker},
            },
        ),
    )
    monkeypatch.setattr(
        thread_model_mod,
        "next_turn_runs_claude_code",
        create_autospec(thread_model_mod.next_turn_runs_claude_code, return_value=False),
    )
    monkeypatch.setattr(
        maint_mod,
        "_summary_to_hand_off",
        create_autospec(maint_mod._summary_to_hand_off, return_value=object()),
    )
    monkeypatch.setattr(
        maint_mod,
        "thread_route",
        create_autospec(
            maint_mod.thread_route,
            return_value=(
                {"conversation_thread_id": THREAD_ID, "workspace_id": WS, "llm_model": "claude-sonnet-4-6"},
                TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
            ),
        ),
    )
    monkeypatch.setattr(
        maint_mod.TurnIdentity,
        "of",
        create_autospec(
            maint_mod.TurnIdentity.of,
            return_value=TurnIdentity(sender=OWNER, owner=OWNER, recorded_as=OWNER),
        ),
    )
    monkeypatch.setattr(
        account_mod,
        "own_access",
        create_autospec(account_mod.own_access, return_value=SimpleNamespace(api_keys=False)),
    )

    queried_run_ids: list[str] = []

    async def _read_by_run(run_row: dict[str, Any] | None):
        rid = str((run_row or {}).get("conversation_response_id") or "")
        queried_run_ids.append(rid)
        if rid == run1_sys_id:
            return "claude-sonnet-4-6", True
        return "gpt-6.2-sol", False

    monkeypatch.setattr(
        atm_mod,
        "get_run_model_and_system_only",
        create_autospec(orig_get_run_sys, side_effect=_read_by_run),
    )

    resolver_flags: list[bool] = []

    def _fake_thread_resolver(*_a: Any, **kw: Any):
        base_ignore = bool(kw.get("ignore_custom_models", False))
        resolver_flags.append(base_ignore)

        async def _res(model_name: str | None, *, ignore_custom_models_override: bool | None = None):
            eff_ignore = base_ignore if ignore_custom_models_override is None else bool(ignore_custom_models_override)
            client_tag = f"sys:{model_name}" if eff_ignore else f"custom:{model_name}"
            return SimpleNamespace(get_llm_client=lambda _c=client_tag: _c)

        return _res

    monkeypatch.setattr(
        ta_mod,
        "thread_resolver",
        create_autospec(ta_mod.thread_resolver, side_effect=_fake_thread_resolver),
    )
    # Only the system-catalog client can replay the summary; owner's custom same-name client cannot.
    monkeypatch.setattr(
        pc_mod,
        "replays",
        create_autospec(pc_mod.replays, side_effect=lambda _s, client: client == "sys:claude-sonnet-4-6"),
    )

    status_ok, _ = await handoff_mod.inspect_hand_off(
        THREAD_ID, OWNER, "claude-sonnet-4-6", source_run_id=run1_sys_id
    )
    assert status_ok == "ready"
    assert queried_run_ids == [run1_sys_id]
    assert resolver_flags == [True]

    # (2) If re-reading r1 still times out -> retryable_failure and marker is NOT cleared
    monkeypatch.setattr(
        atm_mod,
        "get_run_model_and_system_only",
        create_autospec(
            orig_get_run_sys,
            side_effect=TimeoutError("r1 execution lookup still timing out"),
        ),
    )
    clear_spy = create_autospec(dalton_ms_db.clear_thread_metadata_key, return_value=True)
    monkeypatch.setattr(dalton_ms_db, "clear_thread_metadata_key", clear_spy)

    outcome_fail = await handoff_mod.run_pending_model_handoff(THREAD_ID, user_id=OWNER)
    assert outcome_fail == "retryable_failure"
    clear_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_r10_handoff_strict_system_only_propagates_get_latest_attempt_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 10 Decision 2 (grok suspect 1):
    When source_system_only is None and no source_run_id is recorded, a get_latest_attempt
    timeout under strict_system_only=True MUST NOT be swallowed into False — inspect_hand_off
    returns retryable_failure and run_pending_model_handoff keeps the pending marker."""
    from src.dalton.database import model_switch as dalton_ms_db
    from src.server.handlers import thread_maintenance as maint_mod
    from src.server.handlers import thread_model_handoff as handoff_mod
    from src.server.services.llm import thread_model as thread_model_mod

    marker = thread_model_mod.build_pending_model_handoff_marker(
        previous_model="claude-sonnet-4-6",
        user_id=OWNER,
        version="ver-r10-latest-timeout",
        candidate_models=("claude-sonnet-4-6",),
        candidate_sources=(("claude-sonnet-4-6", None),),
    )
    monkeypatch.setattr(
        thread_model_mod.threads_read,
        "get_thread_auth_meta",
        create_autospec(
            thread_model_mod.threads_read.get_thread_auth_meta,
            return_value={
                "user_id": OWNER,
                "workspace_id": WS,
                "llm_model": "claude-sonnet-4-6",
                "metadata": {thread_model_mod.PENDING_MODEL_HANDOFF_KEY: marker},
            },
        ),
    )
    monkeypatch.setattr(
        thread_model_mod,
        "next_turn_runs_claude_code",
        create_autospec(thread_model_mod.next_turn_runs_claude_code, return_value=False),
    )
    monkeypatch.setattr(
        maint_mod,
        "_summary_to_hand_off",
        create_autospec(maint_mod._summary_to_hand_off, return_value=object()),
    )
    monkeypatch.setattr(
        maint_mod,
        "thread_route",
        create_autospec(
            maint_mod.thread_route,
            return_value=(
                {"conversation_thread_id": THREAD_ID, "workspace_id": WS, "llm_model": "claude-sonnet-4-6"},
                TurnRoute(workspace_id=WS, owner_id=OWNER, role="analyst"),
            ),
        ),
    )
    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(
            runs_lifecycle_db.get_latest_attempt,
            side_effect=TimeoutError("latest attempt read timed out"),
        ),
    )
    clear_spy = create_autospec(dalton_ms_db.clear_thread_metadata_key, return_value=True)
    monkeypatch.setattr(dalton_ms_db, "clear_thread_metadata_key", clear_spy)

    status, payload = await handoff_mod.inspect_hand_off(THREAD_ID, OWNER, "claude-sonnet-4-6")
    assert (status, payload) == ("retryable_failure", None)

    outcome = await handoff_mod.run_pending_model_handoff(THREAD_ID, user_id=OWNER)
    assert outcome == "retryable_failure"
    clear_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_r10_default_short_circuit_does_not_skip_when_prev_system_only_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 10 Decision 3 (grok medium 2):
    When thread llm_model is None, target equals effective_default_model, and reading the
    previous run's execution flag fails (None), prepare_thread_model_switch with
    short_circuit_default=True MUST return changed=True and record (model, None) + run_id."""
    from src.server.services.llm import thread_model as thread_model_mod

    run_id = "33333333-3333-4333-8333-3333333333b1"
    monkeypatch.setattr(
        thread_model_mod,
        "require_selectable_model",
        create_autospec(thread_model_mod.require_selectable_model, return_value=None),
    )
    monkeypatch.setattr(
        thread_model_mod.threads_read,
        "get_thread_auth_meta",
        create_autospec(
            thread_model_mod.threads_read.get_thread_auth_meta,
            return_value={
                "user_id": OWNER,
                "workspace_id": WS,
                "llm_model": None,
                "metadata": {},
            },
        ),
    )
    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(
            runs_lifecycle_db.get_latest_attempt,
            return_value={
                "conversation_response_id": run_id,
                "metadata": {"llm_model": "claude-sonnet-4-6", "automation_execution_id": EXEC_ID},
            },
        ),
    )
    monkeypatch.setattr(
        atm_mod,
        "get_run_model_and_system_only",
        create_autospec(
            atm_mod.get_run_model_and_system_only,
            return_value=("claude-sonnet-4-6", None),
        ),
    )
    monkeypatch.setattr(
        thread_model_mod,
        "effective_default_model",
        create_autospec(
            thread_model_mod.effective_default_model,
            return_value="claude-sonnet-4-6",
        ),
    )

    plan = await thread_model_mod.prepare_thread_model_switch(
        OWNER,
        THREAD_ID,
        "claude-sonnet-4-6",
        model_pref={"preferred_model": "claude-sonnet-4-6"},
        short_circuit_default=True,
    )
    assert plan.changed is True
    assert plan.pending_candidate_sources == (("claude-sonnet-4-6", None),)
    assert plan.pending_candidate_source_run_ids == (run_id,)
    assert plan.source_system_only is None
    assert plan.source_run_id == run_id


@pytest.mark.asyncio
async def test_r10_web_patch_same_name_switch_triggers_strict_handoff_on_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 10 Decision 4 (codex low 3, grok low 1):
    When a thread ran a same-named model under a system-only execution and reading that
    execution's flag times out on web PATCH /threads/{id} with the same model name,
    crud.update_thread_endpoint MUST NOT skip handoff — it calls hand_off_on_switch with
    source_run_id and strict_system_only=True (and also on get_latest_attempt timeout)."""
    from src.server.app.threads import crud as crud_mod
    from src.server.database.conversation import threads_read, threads_write
    from src.server.handlers import thread_maintenance as maint_mod
    from src.server.models.conversation import ThreadUpdateRequest
    from src.server.services.llm import thread_model as thread_model_mod

    run_id = "33333333-3333-4333-8333-3333333333c1"
    run_same_name = {
        "conversation_response_id": run_id,
        "metadata": {"llm_model": "claude-sonnet-4-6", "automation_execution_id": EXEC_ID},
    }
    monkeypatch.setattr(
        crud_mod,
        "_authorize_thread_write",
        create_autospec(
            crud_mod._authorize_thread_write,
            return_value=SimpleNamespace(is_shared=False, org_id=None),
        ),
    )
    monkeypatch.setattr(
        crud_mod,
        "require_selectable_model",
        create_autospec(crud_mod.require_selectable_model, return_value=None),
    )
    monkeypatch.setattr(
        crud_mod,
        "get_thread_auth_meta",
        create_autospec(
            threads_read.get_thread_auth_meta,
            return_value={"workspace_id": WS, "llm_model": "claude-sonnet-4-6", "user_id": OWNER},
        ),
    )
    monkeypatch.setattr(
        crud_mod,
        "update_thread_fields",
        create_autospec(
            threads_write.update_thread_fields,
            return_value={
                "conversation_thread_id": THREAD_ID,
                "workspace_id": WS,
                "user_id": OWNER,
                "thread_index": 1,
                "title": "t",
                "current_status": "idle",
                "subagents_allowed": True,
                "msg_type": "ptc",
                "llm_model": "claude-sonnet-4-6",
                "is_pinned": False,
                "archived": False,
                "is_shared": False,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            },
        ),
    )
    monkeypatch.setattr(
        thread_model_mod,
        "next_turn_runs_claude_code",
        create_autospec(thread_model_mod.next_turn_runs_claude_code, return_value=False),
    )

    orig_get_latest = runs_lifecycle_db.get_latest_attempt
    orig_hand_off = maint_mod.hand_off_on_switch

    # (a) Execution snapshot lookup times out -> (model, None) -> same-name PATCH triggers strict handoff with source_run_id
    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(orig_get_latest, return_value=run_same_name),
    )
    monkeypatch.setattr(
        atm_mod,
        "_get_execution_by_id",
        create_autospec(
            atm_mod._get_execution_by_id,
            side_effect=TimeoutError("execution snapshot lookup timed out"),
        ),
    )
    calls_a: list[dict[str, Any]] = []

    async def _cap_a(tid: str, uid: str, prev_model: str | None, **kw: Any) -> bool:
        calls_a.append({"prev_model": prev_model, **kw})
        return True

    monkeypatch.setattr(
        maint_mod,
        "hand_off_on_switch",
        create_autospec(orig_hand_off, side_effect=_cap_a),
    )
    item_a = await crud_mod.update_thread_endpoint(
        THREAD_ID,
        ThreadUpdateRequest(llm_model="claude-sonnet-4-6"),
        x_user_id=OWNER,
    )
    assert item_a.summary_handoff is True
    assert calls_a == [
        {
            "prev_model": "claude-sonnet-4-6",
            "strict_system_only": True,
            "source_run_id": run_id,
        }
    ]

    # (b) get_latest_attempt itself times out (non-'not initialized' error) -> same-name PATCH still triggers strict handoff
    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(
            orig_get_latest,
            side_effect=TimeoutError("latest attempt timed out"),
        ),
    )
    calls_b: list[dict[str, Any]] = []

    async def _cap_b(tid: str, uid: str, prev_model: str | None, **kw: Any) -> bool:
        calls_b.append({"prev_model": prev_model, **kw})
        return True

    monkeypatch.setattr(
        maint_mod,
        "hand_off_on_switch",
        create_autospec(orig_hand_off, side_effect=_cap_b),
    )
    item_b = await crud_mod.update_thread_endpoint(
        THREAD_ID,
        ThreadUpdateRequest(llm_model="claude-sonnet-4-6"),
        x_user_id=OWNER,
    )
    assert item_b.summary_handoff is True
    assert calls_b == [
        {
            "prev_model": "claude-sonnet-4-6",
            "strict_system_only": True,
        }
    ]
