"""Delta rows marked ``replaced_by_tracker`` stay out of the standard suite.

The marker is written by operations. These tests only check that tracking
honors a non-empty string and leaves every unmarked company unchanged.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import create_autospec

import pytest

import src.dalton.autonomy.tracking as tracking_mod
from src.dalton.autonomy.tracking import (
    LABEL_ENABLED_ZH,
    LABEL_INCOMPLETE_ZH,
    disable_tracking,
    enable_tracking,
    resync_delta_schedule,
    set_tier_and_resync,
)
from src.dalton.coverage import set_user_coverage_tier
from tests.unit.dalton.test_tracking import _install_fake_automation_store

TRACKER_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
USER = "user-1"
WORKSPACE = "ws-nvda"
TICKER = "NVDA"


def _row(
    name: str,
    *,
    status: str = "active",
    marker: Any = None,
    paused_by_user: bool = False,
    cron: str = "30 21 * * 1-5",
    automation_id: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"ticker": TICKER, "tier": "watch"}
    if paused_by_user:
        metadata["paused_by_user"] = True
    if marker is not None:
        metadata["replaced_by_tracker"] = marker
    return {
        "automation_id": automation_id or f"id-{name}",
        "user_id": USER,
        "name": name,
        "status": status,
        "workspace_id": WORKSPACE,
        "trigger_type": "cron",
        "instruction": "seed",
        "cron_expression": cron,
        "timezone": "America/New_York",
        "metadata": metadata,
    }


def _suite(
    *,
    delta_status: str = "active",
    marker: Any = None,
    delta_paused_by_user: bool = False,
) -> list[dict[str, Any]]:
    names = (
        "dalton-nvda-watch",
        "dalton-nvda-delta",
        "dalton-nvda-move-up",
        "dalton-nvda-move-down",
    )
    rows: list[dict[str, Any]] = []
    for name in names:
        if name.endswith("-delta"):
            rows.append(
                _row(
                    name,
                    status=delta_status,
                    marker=marker,
                    paused_by_user=delta_paused_by_user,
                )
            )
        else:
            rows.append(_row(name))
    return rows


def _derive(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return tracking_mod._derive_status_and_details(
        workspace_id=WORKSPACE,
        ticker=TICKER,
        tier="watch",
        company_rows=rows,
    )


def _by_name(store: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row["name"]): row for row in store}


def test_marked_delta_does_not_make_the_suite_incomplete() -> None:
    marked = _derive(_suite(delta_status="paused", marker=TRACKER_ID, delta_paused_by_user=True))
    assert marked["status"] == "enabled"
    assert marked["label"] == LABEL_ENABLED_ZH
    assert marked["paused_by_user"] is False
    assert marked["paused_tasks"] == []
    assert marked["missing_tasks"] == []
    assert marked["delta_replaced_by_tracker"] == TRACKER_ID
    delta = next(task for task in marked["tasks"] if task["name"] == "dalton-nvda-delta")
    assert delta["status"] == "paused"
    assert delta["exists"] is True

    plain = _derive(_suite(delta_status="paused", delta_paused_by_user=True))
    assert plain["status"] == "incomplete"
    assert plain["label"] == LABEL_INCOMPLETE_ZH
    assert plain["paused_by_user"] is True
    assert plain["paused_tasks"] == ["dalton-nvda-delta"]
    assert "delta_replaced_by_tracker" not in plain


@pytest.mark.parametrize("marker", ["", "   ", 7, True])
def test_blank_or_non_string_marker_is_unmarked(marker: Any) -> None:
    details = _derive(_suite(delta_status="paused", marker=marker, delta_paused_by_user=True))
    assert details["status"] == "incomplete"
    assert details["paused_tasks"] == ["dalton-nvda-delta"]
    assert details["paused_by_user"] is True
    assert "delta_replaced_by_tracker" not in details
    assert tracking_mod._replaced_by_tracker(_suite(marker=marker)[1]) is None


@pytest.mark.asyncio
async def test_enable_leaves_a_marked_delta_unchanged_and_still_activates_watch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delta = _row(
        "dalton-nvda-delta",
        status="paused",
        marker=TRACKER_ID,
        paused_by_user=True,
        automation_id="auto-delta",
    )
    kept = _row(
        "dalton-nvda-earn-kept",
        status="paused",
        marker=TRACKER_ID,
        automation_id="auto-kept",
    )
    opened = _row(
        "dalton-nvda-earn-open",
        status="paused",
        automation_id="auto-open",
    )
    before_delta = deepcopy(delta)
    before_kept = deepcopy(kept)
    ctx = _install_fake_automation_store(
        monkeypatch,
        workspace_id=WORKSPACE,
        user_id=USER,
        ticker=TICKER,
        initial_rows=[delta, kept, opened],
    )
    result = await enable_tracking(USER, WORKSPACE)
    assert result.get("skipped") is not True
    assert result["status"] == "enabled"
    assert result["delta_replaced_by_tracker"] == TRACKER_ID
    assert result["paused_by_user"] is False
    store = _by_name(ctx["store"])
    assert store["dalton-nvda-delta"]["status"] == before_delta["status"]
    assert store["dalton-nvda-delta"]["cron_expression"] == before_delta["cron_expression"]
    assert store["dalton-nvda-delta"]["timezone"] == before_delta["timezone"]
    assert store["dalton-nvda-delta"]["metadata"] == before_delta["metadata"]
    assert store["dalton-nvda-earn-kept"]["status"] == before_kept["status"]
    assert store["dalton-nvda-earn-kept"]["metadata"] == before_kept["metadata"]
    assert store["dalton-nvda-earn-open"]["status"] == "active"
    assert store["dalton-nvda-watch"]["status"] == "active"
    listed = next(task for task in result["tasks"] if task["name"] == "dalton-nvda-delta")
    assert listed["status"] == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["   ", 7])
async def test_blank_marker_still_reenables_the_delta(
    monkeypatch: pytest.MonkeyPatch,
    marker: Any,
) -> None:
    delta = _row(
        "dalton-nvda-delta",
        status="paused",
        marker=marker,
        paused_by_user=True,
        automation_id="auto-delta",
    )
    ctx = _install_fake_automation_store(
        monkeypatch,
        workspace_id=WORKSPACE,
        user_id=USER,
        ticker=TICKER,
        initial_rows=[delta],
    )
    result = await enable_tracking(USER, WORKSPACE, force=True)
    assert result["status"] == "enabled"
    assert "delta_replaced_by_tracker" not in result
    stored = _by_name(ctx["store"])["dalton-nvda-delta"]
    assert stored["status"] == "active"
    assert tracking_mod._replaced_by_tracker(stored) is None


@pytest.mark.asyncio
async def test_unmarked_enable_and_resync_keep_the_ordinary_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _install_fake_automation_store(
        monkeypatch,
        workspace_id=WORKSPACE,
        user_id=USER,
        ticker=TICKER,
    )
    enabled = await enable_tracking(USER, WORKSPACE)
    assert enabled["status"] == "enabled"
    assert "delta_replaced_by_tracker" not in enabled
    store = _by_name(ctx["store"])
    assert store["dalton-nvda-delta"]["status"] == "active"
    assert store["dalton-nvda-delta"]["cron_expression"] == "0 11 * * 6"
    assert "replaced_by_tracker" not in store["dalton-nvda-delta"]["metadata"]
    resynced = await resync_delta_schedule(USER, TICKER, tier="core")
    assert resynced["updated"] is True
    assert "reason" not in resynced
    assert resynced["new_cron_expression"] == "0 11 * * 2-6"
    assert _by_name(ctx["store"])["dalton-nvda-delta"]["cron_expression"] == "0 11 * * 2-6"


@pytest.mark.asyncio
async def test_resync_skips_a_marked_delta_and_updates_only_unmarked_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _install_fake_automation_store(
        monkeypatch,
        workspace_id=WORKSPACE,
        user_id=USER,
        ticker=TICKER,
    )
    await enable_tracking(USER, WORKSPACE)
    store = ctx["store"]
    delta = _by_name(store)["dalton-nvda-delta"]
    delta["metadata"]["replaced_by_tracker"] = TRACKER_ID
    frozen_cron = delta["cron_expression"]
    updates_before = ctx["mock_update"].await_count
    skipped = await resync_delta_schedule(USER, TICKER, tier="core")
    assert skipped == {
        "ok": True,
        "updated": False,
        "reason": "replaced_by_tracker",
        "user_id": USER,
        "ticker": TICKER,
        "tier": "core",
        "name": "dalton-nvda-delta",
    }
    assert delta["cron_expression"] == frozen_cron
    assert delta["metadata"]["replaced_by_tracker"] == TRACKER_ID
    assert ctx["mock_update"].await_count == updates_before

    extra = _row(
        "dalton-nvda-delta",
        status="paused",
        marker=TRACKER_ID,
        cron="15 3 * * 1",
        automation_id="auto-marked-copy",
    )
    delta["metadata"].pop("replaced_by_tracker")
    store.append(extra)
    mixed = await resync_delta_schedule(USER, TICKER, tier="core")
    assert mixed["updated"] is True
    assert mixed.get("reason") != "replaced_by_tracker"
    assert delta["cron_expression"] == "0 11 * * 2-6"
    assert extra["cron_expression"] == "15 3 * * 1"
    assert extra["status"] == "paused"
    assert extra["metadata"]["replaced_by_tracker"] == TRACKER_ID


@pytest.mark.asyncio
async def test_set_tier_resync_skips_a_marked_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _install_fake_automation_store(
        monkeypatch,
        workspace_id=WORKSPACE,
        user_id=USER,
        ticker=TICKER,
    )
    await enable_tracking(USER, WORKSPACE)
    delta = _by_name(ctx["store"])["dalton-nvda-delta"]
    delta["metadata"]["replaced_by_tracker"] = TRACKER_ID
    frozen = deepcopy(delta)
    updates_before = ctx["mock_update"].await_count

    def _fake_set(user_id: str, tickers: Any, tier: str) -> dict[str, Any]:
        assert user_id == USER
        assert list(tickers) == [TICKER]
        return {"ok": True, "updated_tickers": [TICKER], "tier": tier}

    monkeypatch.setattr(
        tracking_mod,
        "set_user_coverage_tier",
        create_autospec(set_user_coverage_tier, side_effect=_fake_set),
    )
    result = await set_tier_and_resync(USER, [TICKER], "core")
    assert result["delta_schedule"] == [
        {
            "ok": True,
            "updated": False,
            "reason": "replaced_by_tracker",
            "user_id": USER,
            "ticker": TICKER,
            "tier": "core",
            "name": "dalton-nvda-delta",
        }
    ]
    assert delta["status"] == frozen["status"]
    assert delta["cron_expression"] == frozen["cron_expression"]
    assert delta["metadata"] == frozen["metadata"]
    assert ctx["mock_update"].await_count == updates_before


@pytest.mark.asyncio
async def test_disable_still_pauses_a_marked_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _install_fake_automation_store(
        monkeypatch,
        workspace_id=WORKSPACE,
        user_id=USER,
        ticker=TICKER,
    )
    await enable_tracking(USER, WORKSPACE)
    delta = _by_name(ctx["store"])["dalton-nvda-delta"]
    delta["metadata"]["replaced_by_tracker"] = TRACKER_ID
    assert delta["status"] == "active"
    await disable_tracking(USER, WORKSPACE)
    stored = _by_name(ctx["store"])["dalton-nvda-delta"]
    assert stored["status"] == "paused"
    assert stored["metadata"]["paused_by_user"] is True
    assert stored["metadata"]["replaced_by_tracker"] == TRACKER_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("marked_index", [0, 1])
async def test_any_duplicate_delta_marker_blocks_reenable(
    monkeypatch: pytest.MonkeyPatch,
    marked_index: int,
) -> None:
    rows = [
        _row(
            "dalton-nvda-delta",
            status="paused",
            marker=TRACKER_ID if index == marked_index else None,
            paused_by_user=True,
            cron="30 21 * * 1-5" if index == 0 else "15 3 * * 1",
            automation_id=f"auto-delta-{index}",
        )
        for index in (0, 1)
    ]
    before = [deepcopy(row) for row in rows]
    ctx = _install_fake_automation_store(
        monkeypatch,
        workspace_id=WORKSPACE,
        user_id=USER,
        ticker=TICKER,
        initial_rows=rows,
    )
    result = await enable_tracking(USER, WORKSPACE)
    assert result.get("skipped") is not True
    assert result["status"] == "enabled"
    assert result["delta_replaced_by_tracker"] == TRACKER_ID
    stored = [row for row in ctx["store"] if row["name"] == "dalton-nvda-delta"]
    assert [row["automation_id"] for row in stored] == ["auto-delta-0", "auto-delta-1"]
    for original, current in zip(before, stored):
        assert current["status"] == original["status"]
        assert current["cron_expression"] == original["cron_expression"]
        assert current["timezone"] == original["timezone"]
        assert current["metadata"] == original["metadata"]
    assert ctx["mock_update"].await_count == 0
    assert _by_name(ctx["store"])["dalton-nvda-watch"]["status"] == "active"


@pytest.mark.asyncio
async def test_unmarked_duplicate_delta_still_updates_the_first_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _row("dalton-nvda-delta", status="paused", automation_id="auto-delta-0"),
        _row(
            "dalton-nvda-delta",
            status="paused",
            cron="15 3 * * 1",
            automation_id="auto-delta-1",
        ),
    ]
    ctx = _install_fake_automation_store(
        monkeypatch,
        workspace_id=WORKSPACE,
        user_id=USER,
        ticker=TICKER,
        initial_rows=rows,
    )
    result = await enable_tracking(USER, WORKSPACE)
    assert result.get("skipped") is not True
    assert "delta_replaced_by_tracker" not in result
    stored = [row for row in ctx["store"] if row["name"] == "dalton-nvda-delta"]
    assert stored[0]["automation_id"] == "auto-delta-0"
    assert stored[0]["status"] == "active"
    assert stored[1]["automation_id"] == "auto-delta-1"
    assert stored[1]["status"] == "paused"
    assert stored[1]["cron_expression"] == "15 3 * * 1"
    assert "replaced_by_tracker" not in stored[1]["metadata"]
