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
            "metadata": {"llm_model_system_only": True},
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
    )

    ok = await executor._run_turn(EXEC_ID, firing, has_byok=False, deadline=9999999999.0)
    assert ok is True
    call_kw = captured[-1]
    assert call_kw["request"].llm_model_system_only is True
    assert call_kw["run_metadata"]["llm_model_system_only"] is True
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
        run_metadata={"automation_execution_id": EXEC_ID, "llm_model_system_only": True},
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
        "_get_execution_first_turn_run_settings",
        create_autospec(
            atm_mod._get_execution_first_turn_run_settings,
            return_value=("claude-sonnet-4-6", True, "high", True),
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
    assert ctx_meta["llm_model_system_only"] is True
    assert ctx_meta["automation_source_execution_id"] == EXEC_ID


@pytest.mark.asyncio
async def test_thread_maintenance_resolver_forwards_system_only_for_compaction_and_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """grok_r3: thread_maintenance._resolver must pass latest run's llm_model_system_only."""
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

    orig_latest = runs_lifecycle_db.get_latest_attempt
    # 1. Latest attempt has llm_model_system_only=True -> ignore_custom_models=True
    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(
            orig_latest,
            return_value={"metadata": {"llm_model_system_only": True, "subagents_enabled": ["research"]}},
        ),
    )
    resolve_sys = await maint_mod._resolver(OWNER, THREAD_ID, {"llm_model": "claude-sonnet-4-6"}, route)
    await resolve_sys("claude-sonnet-4-6")
    assert seen_flags[-1] is True

    # 2. Normal thread without llm_model_system_only -> ignore_custom_models=False
    monkeypatch.setattr(
        runs_lifecycle_db,
        "get_latest_attempt",
        create_autospec(
            orig_latest,
            return_value={"metadata": {"subagents_enabled": ["research"]}},
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
                    "move_fenced": db_state["move_fenced"],
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
    assert comp_idx < ws_idx
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
        "_get_execution_first_turn_run_settings",
        create_autospec(
            atm_mod._get_execution_first_turn_run_settings,
            return_value=("claude-sonnet-4-6", True, "medium", True),
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

