"""Unit tests for company workspace auto-tracking (`src/dalton/autonomy/tracking.py`, router, and agent tool)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import AsyncConnection

import src.dalton.autonomy.schedule as schedule_mod
import src.dalton.autonomy.tracking as tracking_mod
from src.dalton.coverage import get_user_tier as _real_get_user_tier
from src.dalton.database import coverage_units as _cu_db
from src.dalton.app import dalton_router

_REAL_GET_ACTIVE_UNIT = _cu_db.get_active_unit_by_workspace
from src.dalton.autonomy.schedule import schedule_dalton_automation_helper
from src.dalton.autonomy.tracking import (
    LABEL_DISABLED_ZH,
    LABEL_ENABLED_ZH,
    LABEL_INCOMPLETE_ZH,
    disable_tracking,
    enable_tracking,
    get_tracking_status,
    resync_delta_schedule,
)
from src.dalton.tools.director_tools import build_director_tools
from src.server.database import automation as auto_db
from src.server.database import workspace as ws_db
from src.server.utils.api import get_current_user_id


def _make_tx_lock_conn() -> Any:
    """Build a spec'd ``AsyncConnection`` double with a serialized async ``transaction()`` context manager."""
    conn = create_autospec(AsyncConnection, instance=True)
    tx_lock = asyncio.Lock()

    @asynccontextmanager
    async def _tx_cm(*args: Any, **kwargs: Any):
        async with tx_lock:
            yield conn

    conn.transaction.side_effect = _tx_cm
    return conn


def _install_fake_automation_store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workspace_id: str = "ws-nvda",
    user_id: str = "user-1",
    ticker: str = "NVDA",
    initial_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Wire an in-memory automation store backed by ``create_autospec`` test doubles."""
    store: list[dict[str, Any]] = list(initial_rows or [])
    seq = {"val": len(store) + 1}
    lock_conn = _make_tx_lock_conn()
    observed_conns: dict[str, list[Any]] = {
        "lock": [],
        "list_all": [],
        "create": [],
        "update": [],
    }

    @asynccontextmanager
    async def _fake_get_db_conn():
        yield lock_conn

    mock_get_db_conn = create_autospec(
        schedule_mod.get_db_connection,
        side_effect=_fake_get_db_conn,
    )
    monkeypatch.setattr(schedule_mod, "get_db_connection", mock_get_db_conn)
    # The fake lock connection is not a coverage store: tickers default to an
    # empty roster (watch) unless a test patches its own tiers.
    monkeypatch.setattr(
        tracking_mod,
        "get_user_tier",
        create_autospec(_real_get_user_tier, return_value=None),
    )
    monkeypatch.setattr(
        _cu_db,
        "get_active_unit_by_workspace",
        create_autospec(_REAL_GET_ACTIVE_UNIT, return_value=None),
    )

    async def _fake_lock_user_automations(uid: str, *, conn: Any = None) -> None:
        observed_conns["lock"].append(conn)

    mock_lock_user = create_autospec(
        auto_db.lock_user_automations,
        side_effect=_fake_lock_user_automations,
    )
    monkeypatch.setattr(auto_db, "lock_user_automations", mock_lock_user)

    mock_get_ws = create_autospec(ws_db.get_workspace)

    async def _get_ws(ws_id: str, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        if str(ws_id) == workspace_id:
            return {
                "workspace_id": workspace_id,
                "user_id": user_id,
                "name": ticker,
                "status": "running",
            }
        if str(ws_id) == "ws-googl":
            return {
                "workspace_id": "ws-googl",
                "user_id": user_id,
                "name": "GOOGL",
                "status": "running",
            }
        return None

    mock_get_ws.side_effect = _get_ws
    monkeypatch.setattr(ws_db, "get_workspace", mock_get_ws)

    mock_list = create_autospec(auto_db.list_automations)
    mock_list_all = create_autospec(auto_db.list_all_automations)
    mock_create = create_autospec(auto_db.create_automation)
    mock_update = create_autospec(auto_db.update_automation)
    mock_delete = create_autospec(auto_db.delete_automation)

    async def _list(
        user_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        **kwargs: Any,
    ) -> tuple[list[dict[str, Any]], int]:
        await asyncio.sleep(0)
        rows = [
            dict(r)
            for r in store
            if str(r.get("user_id") or user_id) == str(user_id)
            and (status is None or r.get("status") == status)
        ]
        return rows[offset : offset + limit], len(rows)

    async def _list_all(
        user_id: str,
        *,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        observed_conns["list_all"].append(conn)
        await asyncio.sleep(0)
        return [
            dict(r)
            for r in store
            if str(r.get("user_id") or user_id) == str(user_id)
        ]

    async def _create(
        user_id: str,
        name: str,
        trigger_type: str,
        instruction: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        observed_conns["create"].append(kwargs.get("conn"))
        await asyncio.sleep(0)
        auto_id = f"auto-{seq['val']}"
        seq["val"] += 1
        row = {
            "automation_id": auto_id,
            "user_id": user_id,
            "name": name,
            "trigger_type": trigger_type,
            "instruction": instruction,
            "description": kwargs.get("description"),
            "cron_expression": kwargs.get("cron_expression"),
            "timezone": kwargs.get("timezone") or "America/New_York",
            "trigger_config": kwargs.get("trigger_config"),
            "next_run_at": kwargs.get("next_run_at"),
            "workspace_id": kwargs.get("workspace_id"),
            "status": kwargs.get("status") or "active",
            "metadata": dict(kwargs.get("metadata") or {}),
        }
        store.append(row)
        return dict(row)

    async def _update(
        automation_id: str,
        user_id: str,
        *,
        conn: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        observed_conns["update"].append(conn)
        for r in store:
            if str(r.get("automation_id")) == str(automation_id):
                for k, v in kwargs.items():
                    if k == "metadata" and isinstance(v, dict):
                        r["metadata"] = dict(v)
                    elif v is not None or k in {"next_run_at", "description"}:
                        r[k] = v
                return dict(r)
        return None

    mock_list.side_effect = _list
    mock_list_all.side_effect = _list_all
    mock_create.side_effect = _create
    mock_update.side_effect = _update

    monkeypatch.setattr(auto_db, "list_automations", mock_list)
    monkeypatch.setattr(auto_db, "list_all_automations", mock_list_all)
    monkeypatch.setattr(auto_db, "create_automation", mock_create)
    monkeypatch.setattr(auto_db, "update_automation", mock_update)
    monkeypatch.setattr(auto_db, "delete_automation", mock_delete)
    from src.server.database import automation_executions as exec_db

    mock_skip_execs = create_autospec(
        exec_db.skip_unstarted_executions_for_automations,
        return_value=0,
    )
    mock_get_db_now = create_autospec(
        exec_db.get_db_now,
        return_value=datetime.now(UTC),
    )
    monkeypatch.setattr(exec_db, "skip_unstarted_executions_for_automations", mock_skip_execs)
    monkeypatch.setattr(exec_db, "get_db_now", mock_get_db_now)
    from src.dalton.database import coverage as cov_db
    from src.dalton.watchlist import policies as wl_policies_mod

    monkeypatch.setattr(
        cov_db,
        "get_coverage_row",
        create_autospec(
            cov_db.get_coverage_row,
            return_value={"ticker": str(ticker).upper(), "tier": "core", "is_private": False},
        ),
    )
    monkeypatch.setattr(
        wl_policies_mod,
        "get_user_tier_policy",
        create_autospec(
            wl_policies_mod.get_user_tier_policy,
            return_value=wl_policies_mod.DEFAULT_TIER_POLICIES["core"],
        ),
    )

    return {
        "store": store,
        "lock_conn": lock_conn,
        "observed_conns": observed_conns,
        "mock_get_db_conn": mock_get_db_conn,
        "mock_lock_user": mock_lock_user,
        "mock_get_ws": mock_get_ws,
        "mock_list": mock_list,
        "mock_list_all": mock_list_all,
        "mock_create": mock_create,
        "mock_update": mock_update,
        "mock_delete": mock_delete,
        "mock_skip_execs": mock_skip_execs,
        "mock_get_db_now": mock_get_db_now,
    }


@pytest.mark.asyncio
async def test_enable_tracking_idempotent_and_cron_next_run_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabling twice is idempotent (no duplicate tasks) and cron tasks always carry `next_run_at`."""
    ctx = _install_fake_automation_store(monkeypatch, workspace_id="ws-nvda", ticker="NVDA")
    store = ctx["store"]

    res1 = await enable_tracking("user-1", "ws-nvda")
    assert res1["status"] == "enabled"
    assert res1["enabled"] is True
    assert res1["label"] == LABEL_ENABLED_ZH
    assert len(store) == 4
    assert ctx["mock_create"].await_count == 4

    cron_rows = [r for r in store if r["trigger_type"] == "cron"]
    assert len(cron_rows) == 2
    for c_row in cron_rows:
        assert isinstance(c_row["next_run_at"], datetime)
        assert c_row["next_run_at"].tzinfo is not None

    # Second enable call updates in-place and creates 0 new rows
    res2 = await enable_tracking("user-1", "ws-nvda")
    assert res2["status"] == "enabled"
    assert len(store) == 4
    assert ctx["mock_create"].await_count == 4
    assert ctx["mock_update"].await_count == 4


@pytest.mark.asyncio
async def test_disable_tracking_pauses_only_and_writes_paused_by_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling tracking pauses all `dalton-<t>-*` tasks (including `-earn-*`), never deletes, and sets `paused_by_user`."""
    ctx = _install_fake_automation_store(monkeypatch, workspace_id="ws-nvda", ticker="NVDA")
    store = ctx["store"]

    await enable_tracking("user-1", "ws-nvda")
    # Also add an earnings one-shot task for NVDA
    store.append(
        {
            "automation_id": "auto-earn-1",
            "user_id": "user-1",
            "name": "dalton-nvda-earn-2026q4-t0",
            "trigger_type": "once",
            "instruction": "earnings flash",
            "workspace_id": "ws-nvda",
            "status": "active",
            "next_run_at": datetime(2026, 11, 20, 21, 30, tzinfo=UTC),
            "metadata": {"dalton_managed": True, "ticker": "NVDA"},
        }
    )

    res = await disable_tracking("user-1", "ws-nvda")
    assert res["status"] == "disabled"
    assert res["enabled"] is False
    assert res["paused_by_user"] is True
    assert res["label"] == LABEL_DISABLED_ZH
    assert ctx["mock_delete"].await_count == 0
    assert len(store) == 5

    for r in store:
        assert r["status"] == "paused"
        assert r["metadata"].get("paused_by_user") is True


@pytest.mark.asyncio
async def test_coverage_completion_does_not_reopen_after_user_pause_while_conversation_enable_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disable -> coverage_finish does NOT reopen; Disable -> conversational enable (`source='user'`) DOES reopen."""
    ctx = _install_fake_automation_store(monkeypatch, workspace_id="ws-nvda", ticker="NVDA")
    store = ctx["store"]

    await enable_tracking("user-1", "ws-nvda")
    await disable_tracking("user-1", "ws-nvda")
    assert all(r["status"] == "paused" for r in store)

    # 1. Coverage completion calls enable_tracking (or analyst calls dalton_coverage_tracking with source="coverage_finish")
    res_auto = await enable_tracking("user-1", "ws-nvda")
    assert res_auto["status"] == "disabled"
    assert res_auto["enabled"] is False
    assert res_auto["paused_by_user"] is True
    assert res_auto.get("skipped") is True
    assert all(r["status"] == "paused" for r in store)

    analyst_tools = {
        t.name: t for t in build_director_tools(role="analyst")
    }
    tool_cov_finish = json.loads(
        await analyst_tools["dalton_coverage_tracking"].ainvoke(
            {"action": "enable", "source": "coverage_finish"},
            config={"configurable": {"user_id": "user-1", "workspace_id": "ws-nvda"}},
        )
    )
    assert tool_cov_finish["status"] == "disabled"
    assert tool_cov_finish["paused_by_user"] is True
    assert all(r["status"] == "paused" for r in store)

    # 2. Conversational enable without version authorization refuses to override manual pause (r4 Decision 3);
    #    with explicit `override_pause_versions` matching each row's `config_version`, it re-enables and clears `paused_by_user`.
    tool_user_no_ver = json.loads(
        await analyst_tools["dalton_coverage_tracking"].ainvoke(
            {"action": "enable"},
            config={"configurable": {"user_id": "user-1", "workspace_id": "ws-nvda"}},
        )
    )
    assert tool_user_no_ver["status"] == "disabled"
    assert tool_user_no_ver["paused_by_user"] is True
    assert all(r["status"] == "paused" for r in store)

    override_vers = {str(r["automation_id"]): int(r.get("config_version") or 1) for r in store}
    tool_user_enable = json.loads(
        await analyst_tools["dalton_coverage_tracking"].ainvoke(
            {"action": "enable", "override_pause_versions": override_vers},
            config={"configurable": {"user_id": "user-1", "workspace_id": "ws-nvda"}},
        )
    )
    assert tool_user_enable["status"] == "enabled"
    assert tool_user_enable["paused_by_user"] is False
    assert all(r["status"] == "active" for r in store)
    assert all("paused_by_user" not in (r.get("metadata") or {}) for r in store)


@pytest.mark.asyncio
async def test_tracking_status_derivation_three_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify status derivation across `disabled`, `enabled`, and `incomplete` (missing or partially paused)."""
    ctx = _install_fake_automation_store(monkeypatch, workspace_id="ws-nvda", ticker="NVDA")
    store = ctx["store"]

    # 1. Empty store -> disabled
    st_empty = await get_tracking_status("user-1", "ws-nvda")
    assert st_empty["status"] == "disabled"
    assert st_empty["label"] == LABEL_DISABLED_ZH

    # 2. All 4 active -> enabled
    await enable_tracking("user-1", "ws-nvda")
    st_enabled = await get_tracking_status("user-1", "ws-nvda")
    assert st_enabled["status"] == "enabled"
    assert st_enabled["label"] == LABEL_ENABLED_ZH

    # 3. Partially paused (1 of 4 paused) -> incomplete
    store[0]["status"] = "paused"
    st_partial_pause = await get_tracking_status("user-1", "ws-nvda")
    assert st_partial_pause["status"] == "incomplete"
    assert st_partial_pause["label"] == LABEL_INCOMPLETE_ZH
    assert "dalton-nvda-watch" in st_partial_pause["paused_tasks"]

    # 4. Partially missing (only 3 of 4 exist, all active) -> incomplete
    store[0]["status"] = "active"
    removed = store.pop()
    st_partial_missing = await get_tracking_status("user-1", "ws-nvda")
    assert st_partial_missing["status"] == "incomplete"
    assert removed["name"] in st_partial_missing["missing_tasks"]

    # 5. All 4 paused -> disabled
    store.append(removed)
    for r in store:
        r["status"] = "paused"
    st_all_paused = await get_tracking_status("user-1", "ws-nvda")
    assert st_all_paused["status"] == "disabled"
    assert st_all_paused["label"] == LABEL_DISABLED_ZH


@pytest.mark.asyncio
async def test_agent_tool_role_permissions_analyst_vs_chief_of_staff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Analyst is rejected when targeting another workspace/ticker, substring ticker, or when ticker resolution fails."""
    _install_fake_automation_store(monkeypatch, workspace_id="ws-nvda", ticker="NVDA")
    import src.dalton.tools.director_tools as dt_mod

    mock_resolve_by_ticker = create_autospec(
        dt_mod.resolve_workspace_id_by_ticker,
        side_effect=lambda t, uid: "ws-googl" if t.upper() == "GOOGL" else ("ws-nvda" if t.upper() == "NVDA" else None),
    )
    monkeypatch.setattr(dt_mod, "resolve_workspace_id_by_ticker", mock_resolve_by_ticker)

    analyst_tool = {t.name: t for t in build_director_tools(role="analyst")}["dalton_coverage_tracking"]
    cos_tool = {t.name: t for t in build_director_tools(role="chief_of_staff")}["dalton_coverage_tracking"]

    # Analyst targeting another workspace_id -> rejected
    res_other_ws = json.loads(
        await analyst_tool.ainvoke(
            {"action": "enable", "workspace_id": "ws-googl"},
            config={"configurable": {"user_id": "user-1", "workspace_id": "ws-nvda"}},
        )
    )
    assert res_other_ws["ok"] is False
    assert res_other_ws["error"] == "forbidden_workspace"

    # Analyst targeting another ticker -> rejected
    res_other_ticker = json.loads(
        await analyst_tool.ainvoke(
            {"action": "disable", "ticker": "GOOGL"},
            config={"configurable": {"user_id": "user-1", "workspace_id": "ws-nvda"}},
        )
    )
    assert res_other_ticker["ok"] is False
    assert res_other_ticker["error"] == "forbidden_workspace"

    # Analyst targeting a substring of workspace_id/ticker (`NVD` in `ws-nvda`) -> rejected (no substring match)
    res_substring_ticker = json.loads(
        await analyst_tool.ainvoke(
            {"action": "status", "ticker": "NVD"},
            config={"configurable": {"user_id": "user-1", "workspace_id": "ws-nvda"}},
        )
    )
    assert res_substring_ticker["ok"] is False
    assert res_substring_ticker["error"] == "forbidden_workspace"

    # Analyst operating on own workspace -> allowed
    res_own = json.loads(
        await analyst_tool.ainvoke(
            {"action": "enable", "ticker": "NVDA"},
            config={"configurable": {"user_id": "user-1", "workspace_id": "ws-nvda"}},
        )
    )
    assert res_own["ok"] is True
    assert res_own["status"] == "enabled"

    # When current workspace ticker resolution fails -> analyst is strictly rejected (exception not swallowed)
    res_unresolvable_ws = json.loads(
        await analyst_tool.ainvoke(
            {"action": "status", "ticker": "UNKNOWN"},
            config={"configurable": {"user_id": "user-1", "workspace_id": "ws-unknown-999"}},
        )
    )
    assert res_unresolvable_ws["ok"] is False
    assert res_unresolvable_ws["error"] == "forbidden_workspace"

    # Chief of Staff operating on GOOGL by ticker -> allowed
    res_cos = json.loads(
        await cos_tool.ainvoke(
            {"action": "enable", "ticker": "GOOGL"},
            config={"configurable": {"user_id": "user-1", "workspace_id": "home"}},
        )
    )
    assert res_cos["ok"] is True
    assert res_cos["workspace_id"] == "ws-googl"
    assert res_cos["ticker"] == "GOOGL"
    assert res_cos["status"] == "enabled"


@pytest.mark.asyncio
async def test_over_200_automations_status_disable_and_paused_by_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 1: When user has >200 automations, status, disable, and `paused_by_user` work without a 200-row cap."""
    filler_rows = [
        {
            "automation_id": f"filler-{i}",
            "user_id": "user-1",
            "name": f"dalton-co{i:03d}-watch",
            "trigger_type": "cron",
            "cron_expression": "30 7 * * 1-5",
            "timezone": "America/New_York",
            "instruction": "watch",
            "workspace_id": f"ws-co{i:03d}",
            "status": "active",
            "metadata": {"dalton_managed": True, "ticker": f"CO{i:03d}"},
        }
        for i in range(230)
    ]
    ctx = _install_fake_automation_store(
        monkeypatch,
        workspace_id="ws-nvda",
        user_id="user-1",
        ticker="NVDA",
        initial_rows=filler_rows,
    )
    store = ctx["store"]

    # Create NVDA's 4 tasks at the end of the store (indices 230..233, well past 200)
    res_en = await enable_tracking("user-1", "ws-nvda")
    assert res_en["status"] == "enabled"
    assert len(store) == 234

    st = await get_tracking_status("user-1", "ws-nvda")
    assert st["status"] == "enabled"
    assert len(st["active_tasks"]) == 4

    # Disable NVDA when its rows are past row 200
    res_dis = await disable_tracking("user-1", "ws-nvda")
    assert res_dis["status"] == "disabled"
    assert res_dis["paused_by_user"] is True

    # Auto-enable (coverage_finish) still sees paused_by_user beyond row 200
    res_auto = await enable_tracking("user-1", "ws-nvda", force=False, respect_user_pause=True)
    assert res_auto["status"] == "disabled"
    assert res_auto["paused_by_user"] is True
    assert res_auto.get("skipped") is True


@pytest.mark.asyncio
async def test_schedule_helper_no_implicit_resume_and_merges_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 3 & 5: Upsert without explicit `status` does not implicitly resume a paused task and merges metadata; any single task with `paused_by_user` marks the suite paused by user."""
    ctx = _install_fake_automation_store(monkeypatch, workspace_id="ws-nvda", ticker="NVDA")
    store = ctx["store"]

    await enable_tracking("user-1", "ws-nvda")
    await disable_tracking("user-1", "ws-nvda")

    # Upsert `dalton-nvda-watch` without passing `status` and with extra metadata
    await schedule_dalton_automation_helper(
        "dalton-nvda-watch",
        {
            "name": "dalton-nvda-watch",
            "trigger_type": "cron",
            "cron_expression": "*/30 6-20 * * 1-5",
            "timezone": "America/New_York",
            "instruction": "updated watch instruction",
            "workspace_id": "ws-nvda",
            "metadata": {"custom_tag": "v2"},
        },
        user_id="user-1",
        workspace_id="ws-nvda",
        ticker="NVDA",
        action="upsert",
    )
    watch_row = next(r for r in store if r["name"] == "dalton-nvda-watch")
    # Must remain paused and preserve `paused_by_user` alongside merged `custom_tag`
    assert watch_row["status"] == "paused"
    assert watch_row["metadata"].get("paused_by_user") is True
    assert watch_row["metadata"].get("custom_tag") == "v2"

    # Item 5: Even if 3 tasks are active and only 1 standard task carries `paused_by_user=True`,
    # `paused_by_user` is detected as True and coverage_finish auto-enable does not reopen.
    for r in store:
        if r["name"] != "dalton-nvda-watch":
            r["status"] = "active"
            r["metadata"].pop("paused_by_user", None)

    st = await get_tracking_status("user-1", "ws-nvda")
    assert st["status"] == "incomplete"
    assert st["paused_by_user"] is True

    res_auto = await enable_tracking("user-1", "ws-nvda", force=False, respect_user_pause=True)
    assert res_auto["paused_by_user"] is True
    assert res_auto.get("skipped") is True


@pytest.mark.asyncio
async def test_concurrent_enable_tracking_creates_single_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 4 & R3 Item 2: Two concurrent `enable_tracking` calls are serialized by `user_automations_lock`, share the same lock `conn` across read/write, and abort on lock failure."""
    ctx = _install_fake_automation_store(monkeypatch, workspace_id="ws-nvda", ticker="NVDA")
    store = ctx["store"]
    lock_conn = ctx["lock_conn"]
    observed = ctx["observed_conns"]

    r1, r2 = await asyncio.gather(
        enable_tracking("user-1", "ws-nvda"),
        enable_tracking("user-1", "ws-nvda"),
    )
    assert r1["status"] == "enabled"
    assert r2["status"] == "enabled"
    assert len(store) == 4
    assert ctx["mock_create"].await_count == 4

    # Verify lock_user_automations, list_all_automations (inside lock), create_automation, and update_automation all received the same lock_conn
    assert observed["lock"] and all(c is lock_conn for c in observed["lock"])
    assert observed["create"] and all(c is lock_conn for c in observed["create"])
    assert observed["update"] and all(c is lock_conn for c in observed["update"])
    assert any(c is lock_conn for c in observed["list_all"])

    # When advisory lock acquisition fails, the write operation aborts immediately without mutating rows
    create_before = ctx["mock_create"].await_count
    update_before = ctx["mock_update"].await_count

    async def _lock_boom(uid: str, *, conn: Any = None) -> None:
        raise RuntimeError("pg_advisory_xact_lock failed")

    ctx["mock_lock_user"].side_effect = _lock_boom
    with pytest.raises(RuntimeError, match="pg_advisory_xact_lock failed"):
        await disable_tracking("user-1", "ws-nvda")
    assert ctx["mock_create"].await_count == create_before
    assert ctx["mock_update"].await_count == update_before
    assert all(r["status"] == "active" for r in store)


@pytest.mark.asyncio
async def test_dalton_schedule_automation_rejects_analyst_and_non_pm_on_home_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 Item 4: Analyst and non-PM Chief of Staff are rejected when attempting to create/modify Home tasks via `dalton_schedule_automation`."""
    monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
    monkeypatch.setenv("DALTON_PM_USER_IDS", "pm-user")
    ctx = _install_fake_automation_store(
        monkeypatch,
        workspace_id="ws-nvda",
        user_id="pm-user",
        ticker="NVDA",
    )
    store = ctx["store"]

    async def _fake_resolve_home(uid: str) -> str:
        return "ws-home"

    monkeypatch.setattr(
        schedule_mod,
        "resolve_caller_home_workspace_id",
        create_autospec(schedule_mod.resolve_caller_home_workspace_id, side_effect=_fake_resolve_home),
    )

    analyst_sched = {t.name: t for t in build_director_tools(role="analyst")}["dalton_schedule_automation"]
    cos_sched = {t.name: t for t in build_director_tools(role="chief_of_staff")}["dalton_schedule_automation"]

    # 1. Analyst calling dalton_schedule_automation without workspace_id or with workspace_id="home" on a Home job -> rejected (ok=False)
    res_analyst_home_name = json.loads(
        await analyst_sched.ainvoke(
            {
                "action": "upsert",
                "name": "dalton-director-daily",
                "trigger_type": "cron",
                "cron_expression": "0 8 * * 1-5",
                "instruction": "tamper home job",
            },
            config={"configurable": {"user_id": "pm-user", "workspace_id": "ws-nvda"}},
        )
    )
    assert res_analyst_home_name["ok"] is False
    assert res_analyst_home_name["error"] == "forbidden_scope"

    res_analyst_home_ws = json.loads(
        await analyst_sched.ainvoke(
            {
                "action": "upsert",
                "name": "dalton-nvda-watch",
                "trigger_type": "cron",
                "cron_expression": "*/30 6-20 * * 1-5",
                "instruction": "attempt home target",
                "workspace_id": "home",
                "ticker": "NVDA",
            },
            config={"configurable": {"user_id": "pm-user", "workspace_id": "ws-nvda"}},
        )
    )
    assert res_analyst_home_ws["ok"] is False
    assert res_analyst_home_ws["error"] == "forbidden_scope"

    # 2. Analyst calling dalton_schedule_automation on another company's ticker -> rejected (ok=False)
    res_analyst_cross_co = json.loads(
        await analyst_sched.ainvoke(
            {
                "action": "upsert",
                "name": "dalton-googl-watch",
                "trigger_type": "cron",
                "cron_expression": "*/30 6-20 * * 1-5",
                "instruction": "cross company",
                "ticker": "GOOGL",
            },
            config={"configurable": {"user_id": "pm-user", "workspace_id": "ws-nvda"}},
        )
    )
    assert res_analyst_cross_co["ok"] is False
    assert res_analyst_cross_co["error"] == "forbidden_workspace"

    # 2b. R4 Item 4: When _resolve_workspace_ticker raises, analyst is rejected with forbidden_workspace even if caller passed ticker
    import src.dalton.autonomy.tracking as tracking_mod

    with monkeypatch.context() as mp:
        mp.setattr(
            tracking_mod,
            "_resolve_workspace_ticker",
            create_autospec(
                tracking_mod._resolve_workspace_ticker,
                side_effect=RuntimeError("ticker lookup db error"),
            ),
        )
        res_ticker_err = json.loads(
            await analyst_sched.ainvoke(
                {
                    "action": "upsert",
                    "name": "dalton-googl-watch",
                    "trigger_type": "cron",
                    "cron_expression": "*/30 6-20 * * 1-5",
                    "instruction": "fallback ticker bypass attempt",
                    "ticker": "GOOGL",
                },
                config={"configurable": {"user_id": "pm-user", "workspace_id": "ws-nvda"}},
            )
        )
        assert res_ticker_err["ok"] is False
        assert res_ticker_err["error"] == "forbidden_workspace"
        assert len(store) == 0

    # 3. Non-PM Chief of Staff calling dalton_schedule_automation to modify/create Home task -> rejected (ok=False)
    res_non_pm_cos = json.loads(
        await cos_sched.ainvoke(
            {
                "action": "upsert",
                "name": "dalton-director-daily",
                "trigger_type": "cron",
                "cron_expression": "0 8 * * 1-5",
                "instruction": "non-pm home job",
            },
            config={"configurable": {"user_id": "non-pm-user", "workspace_id": "home"}},
        )
    )
    assert res_non_pm_cos["ok"] is False
    assert res_non_pm_cos["error"] == "forbidden_pm"
    assert len(store) == 0

    # 4. PM Chief of Staff can schedule Home task
    out_pm = json.loads(
        await cos_sched.ainvoke(
            {
                "action": "upsert",
                "name": "dalton-director-daily",
                "trigger_type": "cron",
                "cron_expression": "0 8 * * 1-5",
                "instruction": "pm home job",
            },
            config={"configurable": {"user_id": "pm-user", "workspace_id": "home"}},
        )
    )
    assert out_pm["ok"] is True
    assert out_pm["action"] == "created"
    assert len(store) == 1
    assert store[0]["name"] == "dalton-director-daily"


@pytest.mark.asyncio
async def test_disable_before_tasks_exist_creates_paused_suite_with_paused_by_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 6: Disabling tracking before any tasks exist creates the 4 standard tasks in `paused` state with `paused_by_user=True`."""
    ctx = _install_fake_automation_store(monkeypatch, workspace_id="ws-nvda", ticker="NVDA")
    store = ctx["store"]
    assert len(store) == 0

    res_dis = await disable_tracking("user-1", "ws-nvda")
    assert res_dis["status"] == "disabled"
    assert res_dis["paused_by_user"] is True
    assert len(store) == 4
    for r in store:
        assert r["status"] == "paused"
        assert r["metadata"].get("paused_by_user") is True

    # Subsequent coverage finish must respect that pre-coverage user disable
    res_cov = await enable_tracking("user-1", "ws-nvda", force=False, respect_user_pause=True)
    assert res_cov["status"] == "disabled"
    assert res_cov["paused_by_user"] is True
    assert res_cov.get("skipped") is True
    assert all(r["status"] == "paused" for r in store)


def test_tracking_router_permissions_owner_pm_and_intruder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only workspace owner (and PM) may POST to `/api/v1/dalton/tracking/{workspace_id}`."""
    monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
    monkeypatch.setenv("DALTON_PM_USER_IDS", "local-dev-user")
    _install_fake_automation_store(
        monkeypatch,
        workspace_id="00000000-0000-0000-0000-000000000001",
        user_id="owner-user",
        ticker="NVDA",
    )

    current_user = {"id": "intruder-user"}
    app = FastAPI()
    app.dependency_overrides[get_current_user_id] = lambda: current_user["id"]
    app.include_router(dalton_router)
    client = TestClient(app, raise_server_exceptions=False)

    ws_id = "00000000-0000-0000-0000-000000000001"

    # 1. Non-owner, non-PM intruder -> 403 on GET and POST
    r_get_intruder = client.get(f"/api/v1/dalton/tracking/{ws_id}")
    assert r_get_intruder.status_code == 403
    r_post_intruder = client.post(f"/api/v1/dalton/tracking/{ws_id}", json={"enabled": True})
    assert r_post_intruder.status_code == 403

    # 2. Workspace owner -> 200 on POST and GET
    current_user["id"] = "owner-user"
    r_post_owner = client.post(f"/api/v1/dalton/tracking/{ws_id}", json={"enabled": True})
    assert r_post_owner.status_code == 200
    assert r_post_owner.json()["status"] == "enabled"
    assert r_post_owner.json()["can_edit"] is True

    r_get_owner = client.get(f"/api/v1/dalton/tracking/{ws_id}")
    assert r_get_owner.status_code == 200
    assert r_get_owner.json()["status"] == "enabled"

    # 3. PM user (`local-dev-user` is in default pm_user_ids) -> 200 on POST even when not owner
    current_user["id"] = "local-dev-user"
    r_post_pm = client.post(f"/api/v1/dalton/tracking/{ws_id}", json={"enabled": False})
    assert r_post_pm.status_code == 200
    assert r_post_pm.json()["status"] == "disabled"
    assert r_post_pm.json()["paused_by_user"] is True


@pytest.mark.asyncio
async def test_enable_and_reenable_tracking_delta_schedule_by_tier_core_and_watch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Addendum §§ 1-2 & 5: enable_tracking and re-enable set delta timezone=Asia/Shanghai and tier-based cron (Core: 0 11 * * 2-6, Watch: 0 11 * * 6) while watch tasks stay unchanged."""
    ctx = _install_fake_automation_store(
        monkeypatch,
        workspace_id="ws-echo",
        user_id="user-1",
        ticker="ECHO",
    )
    store = ctx["store"]
    tiers = {"GOOGL": "core", "ECHO": "watch"}
    monkeypatch.setattr(
        tracking_mod,
        "get_user_tier",
        create_autospec(_real_get_user_tier, side_effect=lambda _uid, t, conn=None: tiers.get(t)),
    )

    # 1. Core tier (GOOGL is core in user-1's coverage, registered on ws-googl)
    res_core = await enable_tracking("user-1", "ws-googl")
    assert res_core["status"] == "enabled"
    assert res_core["tier"] == "core"
    by_name = {r["name"]: r for r in store}
    assert by_name["dalton-googl-watch"]["cron_expression"] == "*/30 6-20 * * 1-5"
    assert by_name["dalton-googl-watch"]["timezone"] == "America/New_York"
    assert by_name["dalton-googl-delta"]["cron_expression"] == "0 11 * * 2-6"
    assert by_name["dalton-googl-delta"]["timezone"] == "Asia/Shanghai"
    assert by_name["dalton-googl-delta"]["metadata"]["tier"] == "core"

    # Simulate legacy/paused row with old 30 21 * * 1-5 (America/New_York) and re-enable
    by_name["dalton-googl-delta"]["cron_expression"] = "30 21 * * 1-5"
    by_name["dalton-googl-delta"]["timezone"] = "America/New_York"
    by_name["dalton-googl-delta"]["status"] = "paused"
    await enable_tracking("user-1", "ws-googl", force=True)
    by_name = {r["name"]: r for r in store}
    assert by_name["dalton-googl-delta"]["status"] == "active"
    assert by_name["dalton-googl-delta"]["cron_expression"] == "0 11 * * 2-6"
    assert by_name["dalton-googl-delta"]["timezone"] == "Asia/Shanghai"
    assert by_name["dalton-googl-delta"]["metadata"]["tier"] == "core"

    # 2. Watch tier (ECHO is watch in user-1's coverage, registered on ws-echo)
    res_watch = await enable_tracking("user-1", "ws-echo")
    assert res_watch["status"] == "enabled"
    assert res_watch["tier"] == "watch"
    by_name = {r["name"]: r for r in store}
    assert by_name["dalton-echo-watch"]["cron_expression"] == "30 7 * * 1-5"
    assert by_name["dalton-echo-watch"]["timezone"] == "America/New_York"
    assert by_name["dalton-echo-delta"]["cron_expression"] == "0 11 * * 6"
    assert by_name["dalton-echo-delta"]["timezone"] == "Asia/Shanghai"
    assert by_name["dalton-echo-delta"]["metadata"]["tier"] == "watch"

    # Simulate legacy/paused row with old 30 21 * * 1-5 (America/New_York) and re-enable
    by_name["dalton-echo-delta"]["cron_expression"] = "30 21 * * 1-5"
    by_name["dalton-echo-delta"]["timezone"] = "America/New_York"
    by_name["dalton-echo-delta"]["status"] = "paused"
    await enable_tracking("user-1", "ws-echo", force=True)
    by_name = {r["name"]: r for r in store}
    assert by_name["dalton-echo-delta"]["status"] == "active"
    assert by_name["dalton-echo-delta"]["cron_expression"] == "0 11 * * 6"
    assert by_name["dalton-echo-delta"]["timezone"] == "Asia/Shanghai"
    assert by_name["dalton-echo-delta"]["metadata"]["tier"] == "watch"


@pytest.mark.asyncio
async def test_resync_delta_schedule_watch_to_core_no_tracking_and_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Addendum § 6: resync_delta_schedule does nothing when not tracked, updates Watch->Core schedule, and preserves paused status/paused_by_user."""
    ctx = _install_fake_automation_store(
        monkeypatch,
        workspace_id="ws-echo",
        user_id="user-1",
        ticker="ECHO",
    )
    store = ctx["store"]

    # 1. Not tracked (no delta task) -> does nothing
    res_none = await resync_delta_schedule("user-1", "ECHO")
    assert res_none["ok"] is True
    assert res_none["updated"] is False
    assert res_none["reason"] == "no_delta_task"
    assert len(store) == 0

    # 2. Enable tracking as Watch (0 11 * * 6, Asia/Shanghai), then promote Watch -> Core
    await enable_tracking("user-1", "ws-echo")
    by_name = {r["name"]: r for r in store}
    assert by_name["dalton-echo-delta"]["cron_expression"] == "0 11 * * 6"
    assert by_name["dalton-echo-delta"]["timezone"] == "Asia/Shanghai"
    assert by_name["dalton-echo-delta"]["status"] == "active"

    tier_state = {"ECHO": "core"}
    monkeypatch.setattr(
        tracking_mod,
        "get_user_tier",
        create_autospec(_real_get_user_tier, side_effect=lambda _uid, t, conn=None: tier_state.get(t)),
    )

    res_promoted = await resync_delta_schedule("user-1", "ECHO")
    assert res_promoted["ok"] is True
    assert res_promoted["updated"] is True
    assert res_promoted["tier"] == "core"
    assert res_promoted["old_cron_expression"] == "0 11 * * 6"
    assert res_promoted["new_cron_expression"] == "0 11 * * 2-6"
    assert res_promoted["new_timezone"] == "Asia/Shanghai"
    assert res_promoted["new_next_run_at"] is not None
    assert res_promoted["status"] == "active"

    by_name = {r["name"]: r for r in store}
    assert by_name["dalton-echo-delta"]["cron_expression"] == "0 11 * * 2-6"
    assert by_name["dalton-echo-delta"]["timezone"] == "Asia/Shanghai"
    assert by_name["dalton-echo-delta"]["next_run_at"] is not None
    assert by_name["dalton-echo-delta"]["metadata"]["tier"] == "core"
    assert by_name["dalton-echo-delta"]["status"] == "active"

    # 3. User manually pauses tracking (paused_by_user=True), then tier changes -> only schedule changes, status stays paused
    await disable_tracking("user-1", "ws-echo")
    by_name = {r["name"]: r for r in store}
    assert by_name["dalton-echo-delta"]["status"] == "paused"
    assert by_name["dalton-echo-delta"]["metadata"]["paused_by_user"] is True

    tier_state["ECHO"] = "watch"
    res_demoted_paused = await resync_delta_schedule("user-1", "ECHO")
    assert res_demoted_paused["ok"] is True
    assert res_demoted_paused["updated"] is True
    assert res_demoted_paused["tier"] == "watch"
    assert res_demoted_paused["old_cron_expression"] == "0 11 * * 2-6"
    assert res_demoted_paused["new_cron_expression"] == "0 11 * * 6"
    assert res_demoted_paused["new_timezone"] == "Asia/Shanghai"
    assert res_demoted_paused["new_next_run_at"] is None
    assert res_demoted_paused["status"] == "paused"
    assert res_demoted_paused["paused_by_user"] is True

    by_name = {r["name"]: r for r in store}
    assert by_name["dalton-echo-delta"]["status"] == "paused"
    assert by_name["dalton-echo-delta"]["metadata"]["paused_by_user"] is True
    assert by_name["dalton-echo-delta"]["cron_expression"] == "0 11 * * 6"
    assert by_name["dalton-echo-delta"]["timezone"] == "Asia/Shanghai"
    assert by_name["dalton-echo-delta"]["next_run_at"] is None
    assert by_name["dalton-echo-delta"]["metadata"]["tier"] == "watch"


@pytest.mark.asyncio
async def test_phase1_row7_tracking_owner_resolution_ticker_priority_and_auth_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§3.2 Row 7, C2, G3:
    - resolve_workspace_id_by_ticker prioritizes active unit led by user_id.
    - resolve_workspace_tracking_owner returns COALESCE(unit.lead_analyst_user_id, ws.user_id).
    - router._resolve_tracking_workspace_owner and director_tools._verify_tracking_workspace_owner
      allow current unit lead with web='write' and PM, reject ex-lead owner/non-lead colleague,
      and fail closed when auth is disabled.
    """
    from contextlib import asynccontextmanager
    from unittest.mock import create_autospec
    from fastapi import HTTPException
    from psycopg import AsyncConnection, AsyncCursor
    from src.dalton import settings as dalton_settings
    from src.dalton import sharing as dalton_sharing
    from src.dalton.app import router as dalton_router
    from src.dalton.autonomy import tracking as trk_mod
    from src.dalton.database import coverage_units as cu_db
    from src.dalton.tools import director_tools as dt_mod
    from src.server.database import workspace as ws_db

    ws_id = "11111111-1111-1111-1111-111111111111"
    ws_row = {"workspace_id": ws_id, "user_id": "owner-a", "status": "active"}
    unit_row = {
        "unit_id": "u-nvda",
        "workspace_id": ws_id,
        "owner_user_id": "owner-a",
        "lead_analyst_user_id": "lead-b",
        "status": "active",
        "kind": "company",
    }

    monkeypatch.setattr(dalton_settings, "is_dalton_enabled", lambda: True)
    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(
        dalton_router,
        "get_dalton_settings",
        lambda: dalton_settings.DaltonConfig(enabled=True, pm_user_ids=["pm-user"]),
    )
    monkeypatch.setattr(
        dalton_settings,
        "get_dalton_settings",
        lambda: dalton_settings.DaltonConfig(enabled=True, pm_user_ids=["pm-user"]),
    )
    monkeypatch.setattr(ws_db, "get_workspace", create_autospec(ws_db.get_workspace, return_value=ws_row))
    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", create_autospec(cu_db.get_active_unit_by_workspace, return_value=unit_row))

    # resolve_workspace_tracking_owner uses unit.lead_analyst_user_id
    assert await trk_mod.resolve_workspace_tracking_owner(ws_id, ws_row) == "lead-b"

    # resolve_workspace_id_by_ticker queries active unit led by user_id first
    cur = create_autospec(AsyncCursor, instance=True)
    executed_sql: list[str] = []

    async def _exec_tkr(sql, params=None):
        executed_sql.append(str(sql))

    cur.execute.side_effect = _exec_tkr
    cur.fetchone.side_effect = [(ws_id,)]

    @asynccontextmanager
    async def _cursor(**_kw):
        yield cur

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.side_effect = _cursor

    @asynccontextmanager
    async def _pool_conn(c=None):
        yield conn

    monkeypatch.setattr(trk_mod, "get_db_connection", _pool_conn)
    assert await trk_mod.resolve_workspace_id_by_ticker("NVDA", "lead-b") == ws_id
    assert "dalton_coverage_units" in executed_sql[0]

    # Auth enabled: lead-b with web='write' succeeds; owner-a (transferred away) denied; pm-user allowed
    monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: True)
    from src.dalton import access_tiers

    monkeypatch.setattr(
        access_tiers,
        "classify_owner_tier",
        create_autospec(access_tiers.classify_owner_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "require_workspace_reader",
        create_autospec(dalton_sharing.require_workspace_reader, return_value=ws_row),
    )

    async def _req_ws(wid, uid, *, channel="web", need="read", workspace=None, conn=None):
        if uid in ("lead-b", "owner-a", "pm-user"):
            return ws_row
        raise HTTPException(status_code=403, detail="denied")

    monkeypatch.setattr(
        dalton_sharing,
        "require_workspace_access",
        create_autospec(dalton_sharing.require_workspace_access, side_effect=_req_ws),
    )

    owner_lead, can_edit_lead = await dalton_router._resolve_tracking_workspace_owner(
        ws_id, "lead-b", require_write=True
    )
    assert (owner_lead, can_edit_lead) == ("lead-b", True)

    owner_read_ex, can_edit_ex = await dalton_router._resolve_tracking_workspace_owner(
        ws_id, "owner-a", require_write=False
    )
    assert (owner_read_ex, can_edit_ex) == ("lead-b", False)
    with pytest.raises(HTTPException) as exc_ex_write:
        await dalton_router._resolve_tracking_workspace_owner(
            ws_id, "owner-a", require_write=True
        )
    assert exc_ex_write.value.status_code == 403

    owner_pm, can_edit_pm = await dalton_router._resolve_tracking_workspace_owner(
        ws_id, "pm-user", require_write=True
    )
    assert (owner_pm, can_edit_pm) == ("lead-b", True)

    # director_tools._verify_tracking_workspace_owner aligns cleanly
    assert await dt_mod._verify_tracking_workspace_owner(ws_id, "lead-b") == "lead-b"
    assert await dt_mod._verify_tracking_workspace_owner(ws_id, "pm-user") == "lead-b"
    with pytest.raises(HTTPException) as exc_dt_ex:
        await dt_mod._verify_tracking_workspace_owner(ws_id, "owner-a")
    assert exc_dt_ex.value.status_code == 403

    # G3: auth disabled -> non-owner lead-b fails closed with 403
    monkeypatch.setattr(dalton_settings, "is_dalton_supabase_auth_enabled", lambda: False)
    with pytest.raises(HTTPException) as exc_g3:
        await dalton_router._resolve_tracking_workspace_owner(
            ws_id, "lead-b", require_write=True
        )
    assert exc_g3.value.status_code == 403
    with pytest.raises(HTTPException) as exc_g3_dt:
        await dt_mod._verify_tracking_workspace_owner(ws_id, "lead-b")
    assert exc_g3_dt.value.status_code == 403


@pytest.mark.asyncio
async def test_round9_disable_tracking_pauses_all_unit_managed_and_skips_executions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 9 Item 4: disable_tracking pauses ticker-only standard, earn-*, and renamed unit_id
    tasks, clears non-once next_run_at, and skips unstarted executions."""
    from datetime import timedelta
    from src.dalton import settings as dalton_settings

    ws_nvda = "11111111-1111-4111-8111-111111111111"
    unit_id = "22222222-2222-4222-8222-222222222222"
    future_dt = datetime.now(UTC) + timedelta(days=5)
    initial_rows = [
        {
            "automation_id": "a-ticker-only",
            "user_id": "user-1",
            "name": "dalton-nvda-watch",
            "trigger_type": "cron",
            "cron_expression": "*/30 * * * *",
            "timezone": "America/New_York",
            "next_run_at": future_dt,
            "workspace_id": ws_nvda,
            "status": "active",
            "metadata": {"dalton_kind": "company_watch", "ticker": "NVDA"},
        },
        {
            "automation_id": "a-earn-future",
            "user_id": "user-1",
            "name": "dalton-nvda-earn-2026q4-t0",
            "trigger_type": "once",
            "timezone": "America/New_York",
            "next_run_at": future_dt,
            "workspace_id": ws_nvda,
            "status": "active",
            "metadata": {"dalton_kind": "earnings_t0", "ticker": "NVDA"},
        },
        {
            "automation_id": "a-renamed-managed",
            "user_id": "user-1",
            "name": "custom-nvda-focus",
            "trigger_type": "cron",
            "cron_expression": "0 9 * * *",
            "timezone": "America/New_York",
            "next_run_at": future_dt,
            "workspace_id": ws_nvda,
            "status": "active",
            "metadata": {"dalton_kind": "company_watch", "ticker": "NVDA", "unit_id": unit_id},
        },
        {
            "automation_id": "a-personal-lookalike",
            "user_id": "user-1",
            "name": "my-personal-nvda-note",
            "trigger_type": "cron",
            "workspace_id": ws_nvda,
            "status": "active",
            "metadata": {},
        },
    ]
    ctx = _install_fake_automation_store(
        monkeypatch,
        workspace_id=ws_nvda,
        user_id="user-1",
        ticker="NVDA",
        initial_rows=initial_rows,
    )
    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(
        _cu_db,
        "get_active_unit_by_workspace",
        create_autospec(
            _REAL_GET_ACTIVE_UNIT,
            return_value={
                "unit_id": unit_id,
                "kind": "company",
                "slug": "nvda",
                "ticker": "NVDA",
                "ticker_key": "NVDA",
                "workspace_id": ws_nvda,
                "lead_analyst_user_id": "user-1",
                "owner_user_id": "user-1",
                "status": "active",
            },
        ),
    )
    ctx["mock_skip_execs"].return_value = 2

    res = await disable_tracking("user-1", ws_nvda)
    assert res["status"] == "disabled"
    assert res["paused_by_user"] is True
    assert res["skipped_executions"] == 2

    by_id = {r["automation_id"]: r for r in ctx["store"]}
    assert by_id["a-ticker-only"]["status"] == "paused"
    assert by_id["a-ticker-only"]["next_run_at"] is None
    assert by_id["a-renamed-managed"]["status"] == "paused"
    assert by_id["a-renamed-managed"]["next_run_at"] is None
    assert by_id["a-earn-future"]["status"] == "paused"
    assert by_id["a-earn-future"]["next_run_at"] == future_dt
    assert by_id["a-personal-lookalike"]["status"] == "active"

    ctx["mock_skip_execs"].assert_awaited_once()
    skipped_arg_ids = set(ctx["mock_skip_execs"].await_args.args[0])
    assert {"a-ticker-only", "a-earn-future", "a-renamed-managed"}.issubset(skipped_arg_ids)
    assert "a-personal-lookalike" not in skipped_arg_ids


@pytest.mark.asyncio
async def test_round9_enable_tracking_earnings_resume_refuses_expired_once_and_resumes_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 9 Item 4 & Codex M2: enable_tracking resumes paused earnings tasks via schedule helper,
    refusing expired one-shot rows while activating future ones and standard tasks."""
    from datetime import timedelta

    future_dt = datetime.now(UTC) + timedelta(days=3)
    past_dt = datetime.now(UTC) - timedelta(hours=2)
    initial_rows = [
        {
            "automation_id": "a-earn-expired",
            "user_id": "user-1",
            "name": "dalton-nvda-earn-2025q1-t0",
            "trigger_type": "once",
            "timezone": "UTC",
            "next_run_at": past_dt,
            "workspace_id": "ws-nvda",
            "status": "paused",
            "metadata": {"dalton_kind": "earnings_t0", "ticker": "NVDA", "paused_by_user": True},
        },
        {
            "automation_id": "a-earn-future",
            "user_id": "user-1",
            "name": "dalton-nvda-earn-2026q4-t0",
            "trigger_type": "once",
            "timezone": "UTC",
            "next_run_at": future_dt,
            "workspace_id": "ws-nvda",
            "status": "paused",
            "metadata": {"dalton_kind": "earnings_t0", "ticker": "NVDA", "paused_by_user": True},
        },
    ]
    ctx = _install_fake_automation_store(
        monkeypatch,
        workspace_id="ws-nvda",
        user_id="user-1",
        ticker="NVDA",
        initial_rows=initial_rows,
    )

    res = await enable_tracking(
        "user-1",
        "ws-nvda",
        force=True,
        override_pause_versions={
            str(r["automation_id"]): int(r.get("config_version") or 1) for r in initial_rows
        },
    )
    assert res["status"] == "enabled"
    assert res["paused_by_user"] is False
    refusals = res.get("resume_refusals") or []
    assert len(refusals) == 1
    assert refusals[0]["name"] == "dalton-nvda-earn-2025q1-t0"
    assert "scheduled time has passed" in refusals[0]["message"]

    by_id = {r["automation_id"]: r for r in ctx["store"]}
    assert by_id["a-earn-expired"]["status"] == "paused"
    assert by_id["a-earn-future"]["status"] == "active"
    assert "paused_by_user" not in by_id["a-earn-future"]["metadata"]


@pytest.mark.asyncio
async def test_round10_disable_tracking_passes_tx_cutoff_to_skip_only_preexisting_executions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 10 Item 3 (Codex M2):
    disable_tracking captures DB clock timestamp before releasing user_automations_lock and
    forwards `created_at_lte` to `skip_unstarted_executions_for_automations`, generating
    `AND created_at <= %s` so post-re-enable executions are never skipped.
    """
    from datetime import timedelta
    from src.server.database import automation_executions as exec_db

    ws_nvda = "11111111-1111-4111-8111-111111111111"
    cutoff_dt = datetime(2026, 11, 20, 12, 0, 5, tzinfo=UTC)
    ctx = _install_fake_automation_store(
        monkeypatch,
        workspace_id=ws_nvda,
        user_id="user-1",
        ticker="NVDA",
        initial_rows=[
            {
                "automation_id": "a-watch",
                "user_id": "user-1",
                "name": "dalton-nvda-watch",
                "trigger_type": "cron",
                "cron_expression": "*/30 * * * *",
                "timezone": "America/New_York",
                "workspace_id": ws_nvda,
                "status": "active",
                "metadata": {"dalton_kind": "company_watch", "ticker": "NVDA"},
            }
        ],
    )
    events: list[str] = []

    async def _get_now(*, conn: Any = None) -> datetime:
        assert conn is ctx["lock_conn"]
        events.append("get_db_now_in_tx")
        return cutoff_dt

    async def _skip_execs(
        automation_ids: list[str] | tuple[str, ...],
        *,
        reason: str = "unit_handoff",
        error_message: str = "",
        created_at_lte: datetime | None = None,
        conn: Any = None,
    ) -> int:
        events.append("skip_execs_after_tx")
        assert conn is None
        assert created_at_lte == cutoff_dt
        assert "a-watch" in automation_ids
        return 1

    ctx["mock_get_db_now"].side_effect = _get_now
    ctx["mock_skip_execs"].side_effect = _skip_execs

    res = await disable_tracking("user-1", ws_nvda)
    assert res["skipped_executions"] == 1
    assert events == ["get_db_now_in_tx", "skip_execs_after_tx"]

    # Also test real SQL emitted by exec_db.skip_unstarted_executions_for_automations(created_at_lte=cutoff_dt)
    monkeypatch.undo()
    exec_rows = [
        {"id": "e-before", "automation_id": "a-watch", "status": "pending", "created_at": cutoff_dt - timedelta(seconds=2)},
        {"id": "e-exact", "automation_id": "a-watch", "status": "waiting", "created_at": cutoff_dt},
        {"id": "e-after", "automation_id": "a-watch", "status": "pending", "created_at": cutoff_dt + timedelta(seconds=2)},
    ]
    recorded_sql: list[tuple[str, Any]] = []

    class _FakeExecConn:
        @asynccontextmanager
        async def transaction(self):
            yield self

        @asynccontextmanager
        async def cursor(self):
            class _C:
                rowcount = 0

                async def execute(self_c, sql, params=None):
                    q = " ".join(str(sql).split())
                    recorded_sql.append((q, params))
                    if "UPDATE automation_executions" in q:
                        cutoff = params[3]
                        matched = [
                            er
                            for er in exec_rows
                            if er["automation_id"] in params[2]
                            and er["status"] in ("pending", "waiting")
                            and er["created_at"] <= cutoff
                        ]
                        for er in matched:
                            er["status"] = "skipped"
                        self_c.rowcount = len(matched)

            yield _C()

    @asynccontextmanager
    async def _fake_exec_pool():
        yield _FakeExecConn()

    monkeypatch.setattr(exec_db, "get_db_connection", _fake_exec_pool)
    skipped = await exec_db.skip_unstarted_executions_for_automations(
        ["a-watch"], reason="disabled", created_at_lte=cutoff_dt
    )
    assert skipped == 2
    assert "AND created_at <= %s" in recorded_sql[0][0]
    assert recorded_sql[0][1][3] == cutoff_dt
    by_eid = {r["id"]: r["status"] for r in exec_rows}
    assert by_eid["e-before"] == "skipped"
    assert by_eid["e-exact"] == "skipped"
    assert by_eid["e-after"] == "pending"


@pytest.mark.asyncio
async def test_round10_enable_tracking_resumes_brk_b_dotted_earnings_and_renamed_unit_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 10 Item 4 (Codex M3 & GLM L3):
    For unit ticker `BRK-B`, `enable_tracking` discovers and resumes unit-managed rows
    including dotted `dalton-brk.b-earn-*` and renamed `unit_id`-carrying custom tasks.
    """
    from datetime import timedelta
    from src.dalton import settings as dalton_settings

    ws_brk = "11111111-1111-4111-8111-111111111111"
    unit_id = "22222222-2222-4222-8222-222222222222"
    future_dt = datetime.now(UTC) + timedelta(days=4)
    initial_rows = [
        {
            "automation_id": "a-brk-earn",
            "user_id": "user-1",
            "name": "dalton-brk.b-earn-2026q4-t0",
            "trigger_type": "once",
            "timezone": "UTC",
            "next_run_at": future_dt,
            "workspace_id": ws_brk,
            "status": "paused",
            "metadata": {
                "dalton_kind": "earnings_t0",
                "ticker": "BRK.B",
                "unit_id": unit_id,
                "paused_by_user": True,
            },
        },
        {
            "automation_id": "a-brk-renamed",
            "user_id": "user-1",
            "name": "custom-brk-focus",
            "trigger_type": "cron",
            "cron_expression": "0 9 * * 1-5",
            "timezone": "America/New_York",
            "workspace_id": ws_brk,
            "status": "paused",
            "metadata": {
                "dalton_kind": "company_watch",
                "ticker": "BRK-B",
                "unit_id": unit_id,
                "paused_by_user": True,
            },
        },
    ]
    ctx = _install_fake_automation_store(
        monkeypatch,
        workspace_id=ws_brk,
        user_id="user-1",
        ticker="BRK-B",
        initial_rows=initial_rows,
    )
    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", lambda: True)
    brk_unit = {
        "unit_id": unit_id,
        "kind": "company",
        "slug": "brk-b",
        "ticker": "BRK-B",
        "ticker_key": "BRK.B",
        "workspace_id": ws_brk,
        "lead_analyst_user_id": "user-1",
        "owner_user_id": "user-1",
        "status": "active",
    }
    monkeypatch.setattr(
        _cu_db,
        "get_active_unit_by_workspace",
        create_autospec(_REAL_GET_ACTIVE_UNIT, return_value=brk_unit),
    )

    res = await enable_tracking(
        "user-1",
        ws_brk,
        force=True,
        override_pause_versions={
            str(r["automation_id"]): int(r.get("config_version") or 1) for r in initial_rows
        },
    )
    assert res["status"] == "enabled"
    assert not res.get("resume_refusals")
    by_id = {r["automation_id"]: r for r in ctx["store"]}
    assert by_id["a-brk-earn"]["status"] == "active"
    assert "paused_by_user" not in by_id["a-brk-earn"]["metadata"]
    assert by_id["a-brk-renamed"]["status"] == "active"
    assert "paused_by_user" not in by_id["a-brk-renamed"]["metadata"]
    returned_names = {t["name"] for t in res["tasks"]}
    assert "dalton-brk.b-earn-2026q4-t0" in returned_names
    assert "custom-brk-focus" in returned_names


@pytest.mark.asyncio
async def test_round10_resume_once_automation_never_readds_removed_paused_by_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 10 Item 5 (Codex M4):
    Resuming an unexpired one-shot earnings task removes `paused_by_user` and
    `handoff_paused_reason` before saving `once_schedule_at` in `auto_db.update_automation`.
    """
    from datetime import timedelta
    from src.dalton.database import company_automations as co_auto_db
    from src.server.database import automation as real_auto_db

    ws_nvda = "11111111-1111-4111-8111-111111111111"
    future_dt = datetime.now(UTC) + timedelta(days=2)
    stored_row: dict[str, Any] = {
        "automation_id": "11111111-2222-4333-8444-555555555555",
        "user_id": "user-1",
        "name": "dalton-nvda-earn-2026q4-t0",
        "description": "earnings",
        "trigger_type": "once",
        "cron_expression": None,
        "timezone": "UTC",
        "trigger_config": None,
        "next_run_at": future_dt,
        "workspace_id": ws_nvda,
        "thread_id": None,
        "instruction": "check earnings",
        "status": "paused",
        "disable_reason": None,
        "failure_count": 0,
        "config_version": 1,
        "active_version": 1,
        "updated_by": "user-1",
        "audit_history": [],
        "metadata": {
            "dalton_kind": "earnings_t0",
            "ticker": "NVDA",
            "paused_by_user": True,
            "handoff_paused_reason": "paused",
        },
        "model": None,
    }
    captured_meta: list[dict[str, Any]] = []

    class _RealLikeCur:
        def __init__(self) -> None:
            self._rows: list[Any] = []

        async def __aenter__(self) -> _RealLikeCur:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def execute(self, sql: str, params: Any = None) -> None:
            q = " ".join(str(sql).split())
            if "FROM automations" in q and "FOR UPDATE" in q:
                self._rows = [dict(stored_row)]
            elif "WITH updated AS (UPDATE automations SET" in q:
                for p in params or ():
                    raw_val = getattr(p, "obj", p)
                    if isinstance(raw_val, dict) and "once_schedule_at" in raw_val:
                        captured_meta.append(dict(raw_val))
                        stored_row["metadata"] = dict(raw_val)
                    elif isinstance(raw_val, str) and '"once_schedule_at"' in raw_val:
                        parsed = json.loads(raw_val)
                        captured_meta.append(parsed)
                        stored_row["metadata"] = parsed
                stored_row["status"] = "active"
                self._rows = [{**stored_row, "last_execution": None}]
            else:
                self._rows = []

        async def fetchone(self) -> Any:
            return self._rows[0] if self._rows else None

    class _RealLikeConn:
        @asynccontextmanager
        async def transaction(self):
            yield self

        @asynccontextmanager
        async def cursor(self, **_kw):
            yield _RealLikeCur()

    real_conn = _RealLikeConn()

    @asynccontextmanager
    async def _db_cm(conn_arg=None):
        yield conn_arg if conn_arg is not None else real_conn

    monkeypatch.setattr(schedule_mod, "get_db_connection", _db_cm)
    monkeypatch.setattr(real_auto_db, "get_db_connection", _db_cm)
    monkeypatch.setattr(
        real_auto_db,
        "lock_user_automations",
        create_autospec(real_auto_db.lock_user_automations, return_value=None),
    )
    monkeypatch.setattr(
        real_auto_db,
        "list_all_automations",
        create_autospec(real_auto_db.list_all_automations, return_value=[dict(stored_row)]),
    )
    monkeypatch.setattr(
        co_auto_db,
        "lock_and_fetch_user_preferences_for_snapshot",
        create_autospec(
            co_auto_db.lock_and_fetch_user_preferences_for_snapshot,
            return_value={},
        ),
    )
    monkeypatch.setattr(
        co_auto_db,
        "prepare_update_history_columns",
        create_autospec(
            co_auto_db.prepare_update_history_columns,
            return_value=({}, False),
        ),
    )
    monkeypatch.setattr(
        schedule_mod,
        "verify_workspace_access",
        create_autospec(
            schedule_mod.verify_workspace_access,
            return_value={
                "workspace_id": ws_nvda,
                "user_id": "user-1",
                "name": "NVDA",
                "status": "active",
            },
        ),
    )
    monkeypatch.setattr(
        _cu_db,
        "get_active_unit_by_workspace",
        create_autospec(_REAL_GET_ACTIVE_UNIT, return_value=None),
    )
    from src.dalton.database import coverage as cov_db_mod
    from src.dalton.watchlist import policies as wl_policies_mod

    monkeypatch.setattr(
        cov_db_mod,
        "get_coverage_row",
        create_autospec(
            cov_db_mod.get_coverage_row,
            return_value={"ticker": "NVDA", "tier": "core", "is_private": False},
        ),
    )
    monkeypatch.setattr(
        wl_policies_mod,
        "get_user_tier_policy",
        create_autospec(
            wl_policies_mod.get_user_tier_policy,
            return_value=wl_policies_mod.DEFAULT_TIER_POLICIES["core"],
        ),
    )

    res = await schedule_dalton_automation_helper(
        "dalton-nvda-earn-2026q4-t0",
        {"action": "resume"},
        user_id="user-1",
        workspace_id=ws_nvda,
        ticker="NVDA",
        role="chief_of_staff",
        action="resume",
    )
    assert res["ok"] is True
    assert captured_meta
    saved_meta = captured_meta[-1]
    assert "once_schedule_at" in saved_meta
    assert "paused_by_user" not in saved_meta
    assert "handoff_paused_reason" not in saved_meta


@pytest.mark.asyncio
async def test_round11_pause_resume_by_automation_id_enforces_workspace_and_uses_task_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 11 Item 1:
    1) Calling pause/resume with workspace A and an automation_id belonging to
       workspace B is refused as not found (both helper and REST PUT endpoint).
    2) Resolving the task uses the task's actual workspace for coverage-unit lead checks.
    """
    ws_a = "11111111-1111-4111-8111-111111111111"
    ws_b = "22222222-2222-4222-8222-222222222222"
    auto_b_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    home_ws = "99999999-9999-4999-8999-999999999999"

    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "1")
    fx = _install_fake_automation_store(
        monkeypatch,
        workspace_id=ws_a,
        user_id="user-1",
        ticker="NVDA",
        initial_rows=[
            {
                "automation_id": auto_b_id,
                "user_id": "user-1",
                "name": "custom-ws-b-task",
                "workspace_id": ws_b,
                "status": "active",
                "trigger_type": "cron",
                "cron_expression": "0 8 * * 1-5",
                "timezone": "America/New_York",
                "instruction": "monitor ws_b",
                "metadata": {"dalton_kind": "earnings_t0", "ticker": "MSFT", "unit_id": "unit-b"},
            }
        ],
    )

    ws_map = {
        ws_a: {"workspace_id": ws_a, "user_id": "user-1", "name": "NVDA", "status": "running"},
        ws_b: {"workspace_id": ws_b, "user_id": "user-1", "name": "MSFT", "status": "running"},
        home_ws: {"workspace_id": home_ws, "user_id": "user-1", "name": "Home", "status": "running"},
    }
    monkeypatch.setattr(
        schedule_mod,
        "verify_workspace_access",
        create_autospec(
            schedule_mod.verify_workspace_access,
            side_effect=lambda wid, uid, **_kw: ws_map[str(wid)],
        ),
    )

    async def _unit_by_ws(wid: str, **_kw: Any) -> dict[str, Any] | None:
        if str(wid) == ws_b:
            return {
                "unit_id": "unit-b",
                "kind": "company",
                "ticker": "MSFT",
                "workspace_id": ws_b,
                "owner_user_id": "user-1",
                "lead_analyst_user_id": "lead-other",
                "status": "active",
            }
        return None

    monkeypatch.setattr(
        _cu_db,
        "get_active_unit_by_workspace",
        create_autospec(_REAL_GET_ACTIVE_UNIT, side_effect=_unit_by_ws),
    )

    for act in ("pause", "resume"):
        res = await schedule_dalton_automation_helper(
            "dalton-nvda-ignored",
            {"action": act, "automation_id": auto_b_id},
            user_id="user-1",
            workspace_id=ws_a,
            ticker="NVDA",
            action=act,
        )
        assert res["ok"] is False
        assert "未找到任务" in str(res.get("error") or "")

    assert fx["store"][0]["status"] == "active"
    fx["mock_update"].assert_not_awaited()

    import src.dalton.app.router as router_mod

    monkeypatch.setattr(
        router_mod,
        "_require_dalton_enabled",
        create_autospec(router_mod._require_dalton_enabled, return_value=None),
    )
    monkeypatch.setattr(
        router_mod,
        "_verify_workspace_access",
        create_autospec(
            router_mod._verify_workspace_access,
            side_effect=lambda wid, uid, **_kw: ws_map[str(wid)],
        ),
    )
    app = FastAPI()
    app.include_router(dalton_router)
    app.dependency_overrides[get_current_user_id] = lambda: "user-1"
    client = TestClient(app)

    for act in ("pause", "resume"):
        resp = client.put(
            "/api/v1/dalton/automations/custom-ws-b-task",
            json={"workspace_id": ws_a, "automation_id": auto_b_id, "action": act},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "未找到任务" in body["error"]

    assert fx["store"][0]["status"] == "active"
    fx["mock_update"].assert_not_awaited()

    # Chief-of-staff call with unresolved home workspace matches the ws_b row,
    # then checks unit leadership against ws_b (not the empty requested workspace_id).
    monkeypatch.setenv("DALTON_PM_USER_IDS", "user-1")
    monkeypatch.setattr(
        schedule_mod,
        "resolve_caller_home_workspace_id",
        create_autospec(schedule_mod.resolve_caller_home_workspace_id, return_value=""),
    )
    lead_refused = await schedule_dalton_automation_helper(
        "custom-ws-b-task",
        {"action": "pause", "automation_id": auto_b_id},
        user_id="user-1",
        workspace_id="",
        role="chief_of_staff",
        action="pause",
    )
    assert lead_refused["ok"] is False
    assert lead_refused["error"] == "not_unit_lead_analyst"
    assert fx["store"][0]["status"] == "active"
    fx["mock_update"].assert_not_awaited()



