"""Tests for dalton_schedule_automation max_failures (tool + REST) and docstring contract."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import HTTPException
from psycopg import AsyncConnection
from pydantic import ValidationError

import src.dalton.app.router as dalton_router
import src.dalton.autonomy.schedule as schedule_mod
from src.dalton import settings as dalton_settings
from src.dalton.tools.director_tools import build_director_tools
from src.server.database import automation as auto_db
from src.server.database import workspace as ws_db
from src.server.services.automations import lifecycle

USER = "user-sched-1"
WS = "11111111-1111-4111-8111-111111111111"
AUTO_ID = "22222222-2222-4222-8222-222222222222"


def _install_schedule_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: list[dict[str, Any]],
) -> None:
    lock_conn = create_autospec(AsyncConnection, instance=True)

    @asynccontextmanager
    async def _tx(*_a: Any, **_kw: Any):
        yield lock_conn

    lock_conn.transaction.side_effect = _tx

    @asynccontextmanager
    async def _get_conn():
        yield lock_conn

    monkeypatch.setattr(
        lifecycle.user_models,
        "get_model_preference",
        create_autospec(lifecycle.user_models.get_model_preference, return_value={}),
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
    monkeypatch.setattr(
        dalton_settings,
        "is_coverage_units_enabled",
        create_autospec(dalton_settings.is_coverage_units_enabled, return_value=False),
    )


@pytest.mark.asyncio
async def test_schedule_automation_tool_sets_updates_and_lists_max_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: list[dict[str, Any]] = []
    _install_schedule_env(monkeypatch, store=store)
    sched = {t.name: t for t in build_director_tools(role="chief_of_staff")}[
        "dalton_schedule_automation"
    ]
    cfg = {"configurable": {"user_id": USER, "workspace_id": WS}}

    created = json.loads(
        await sched.ainvoke(
            {
                "action": "upsert",
                "name": "dalton-nvda-watch",
                "trigger_type": "cron",
                "cron_expression": "0 9 * * 1-5",
                "instruction": "Check NVDA.",
                "workspace_id": WS,
                "max_failures": 5,
            },
            config=cfg,
        )
    )
    assert created["ok"] is True
    assert store[0]["max_failures"] == 5

    listed = json.loads(
        await sched.ainvoke({"action": "list", "workspace_id": WS}, config=cfg)
    )
    assert listed["automations"][0]["max_failures"] == 5

    updated = json.loads(
        await sched.ainvoke(
            {
                "action": "upsert",
                "name": "dalton-nvda-watch",
                "workspace_id": WS,
                "max_failures": 12,
            },
            config=cfg,
        )
    )
    assert updated["ok"] is True
    assert store[0]["max_failures"] == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", [0, -1, 101])
async def test_schedule_automation_tool_rejects_out_of_range_max_failures(
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
@pytest.mark.parametrize("bad_value", [True, False, "5", "abc", 1.5])
async def test_schedule_automation_tool_rejects_bool_and_non_int_max_failures(
    monkeypatch: pytest.MonkeyPatch, bad_value: Any
) -> None:
    store: list[dict[str, Any]] = []
    _install_schedule_env(monkeypatch, store=store)
    sched = {t.name: t for t in build_director_tools(role="chief_of_staff")}[
        "dalton_schedule_automation"
    ]
    cfg = {"configurable": {"user_id": USER, "workspace_id": WS}}

    with pytest.raises(ValidationError):
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
    assert store == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_value", [True, False, "5", "abc", 1.5, 0, -1, 101]
)
async def test_put_dalton_automation_rest_returns_422_for_invalid_max_failures(
    monkeypatch: pytest.MonkeyPatch, bad_value: Any
) -> None:
    store: list[dict[str, Any]] = []
    _install_schedule_env(monkeypatch, store=store)
    monkeypatch.setattr(
        dalton_router,
        "_require_dalton_enabled",
        create_autospec(dalton_router._require_dalton_enabled, return_value=None),
    )
    monkeypatch.setattr(
        dalton_router,
        "_verify_workspace_access",
        create_autospec(
            dalton_router._verify_workspace_access,
            return_value={"workspace_id": WS, "user_id": USER},
        ),
    )

    body = dalton_router.DaltonAutomationWriteRequest(
        workspace_id=WS,
        action="upsert",
        trigger_type="cron",
        cron_expression="0 9 * * 1-5",
        instruction="Check.",
        max_failures=bad_value,
    )
    with pytest.raises(HTTPException) as exc_info:
        await dalton_router.put_dalton_automation_endpoint(
            name="dalton-nvda-watch",
            body=body,
            user_id=USER,
        )
    assert 400 <= exc_info.value.status_code < 500
    assert exc_info.value.status_code == 422
    assert store == []


@pytest.mark.asyncio
async def test_put_dalton_automation_rest_saves_valid_max_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store: list[dict[str, Any]] = []
    _install_schedule_env(monkeypatch, store=store)
    monkeypatch.setattr(
        dalton_router,
        "_require_dalton_enabled",
        create_autospec(dalton_router._require_dalton_enabled, return_value=None),
    )
    monkeypatch.setattr(
        dalton_router,
        "_verify_workspace_access",
        create_autospec(
            dalton_router._verify_workspace_access,
            return_value={"workspace_id": WS, "user_id": USER},
        ),
    )

    body = dalton_router.DaltonAutomationWriteRequest(
        workspace_id=WS,
        entry={
            "action": "upsert",
            "trigger_type": "cron",
            "cron_expression": "0 9 * * 1-5",
            "instruction": "Check.",
            "max_failures": 7,
        },
    )
    res = await dalton_router.put_dalton_automation_endpoint(
        name="dalton-nvda-watch",
        body=body,
        user_id=USER,
    )
    assert res["ok"] is True
    assert store[0]["max_failures"] == 7


@pytest.mark.asyncio
async def test_schedule_automation_rejects_analyst_targeting_home_with_max_failures(
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
                "max_failures": 5,
            },
            config=cfg,
        )
    )
    assert refused["ok"] is False
    assert refused["error"] == "forbidden_scope"
    assert store == []


def test_dalton_schedule_automation_docstring_matches_contract() -> None:
    sched = {t.name: t for t in build_director_tools(role="chief_of_staff")}[
        "dalton_schedule_automation"
    ]
    desc = sched.description or ""
    assert "llm_model" not in desc
    assert "llm_model" not in (sched.args_schema.model_fields if sched.args_schema else {})
    assert 'trigger_type: "cron" or "once"' in desc
    assert '"price"' not in desc
    assert "dalton_coverage_tracking" in desc
    assert "trigger_config" in desc
    assert "Asia/Shanghai" not in desc
    assert "deployment default" in desc
    assert "keep the existing timezone" in desc
    assert "does not move" in desc
    assert "max_failures:" in desc
    assert "1–100" in desc
    assert "Built-in Antigravity effort is determined by the task's model ID" in desc
    assert "automation's settings" in desc
