"""Targeted unit tests covering Personal Watchlist r3 review fixes (Items 5, 9, 10, 11, 12, 15)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.dalton.app.router import router as dalton_router
from src.dalton.autonomy import preflight as preflight_mod
from src.dalton.autonomy import tracking as tracking_mod
from src.dalton.coverage import service as cov_svc
from src.dalton.tools import build_dalton_tools
from src.dalton.tools import director_tools as director_tools_mod
from src.dalton.watchlist import policies as policies_mod
from src.dalton.watchlist import privacy as privacy_mod
from src.dalton.watchlist import reconciler as reconciler_mod
from src.dalton.watchlist import service as service_mod
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
from src.server.utils.api import get_current_user_id

REPO_ROOT = Path(__file__).resolve().parents[4]


def _load_script_module(rel_path: str, name: str):
    path = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Item 5: Legacy entry convergence onto WatchlistMutationService / Outbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_entries_converge_to_watchlist_mutation_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    async def _fake_exec(self, user_id: str, **kwargs):
        calls.append({"user_id": user_id, **kwargs})
        act = kwargs["action"]
        if act == "remove":
            return {
                "ok": True,
                "removed": list(kwargs.get("tickers") or []),
                "removed_tickers": list(kwargs.get("tickers") or []),
                "not_found": [],
                "workspaces_preserved": ["NVDA"],
                "notice": "NVDA 已有公司 workspace，workspace 和历史文档不会被删除，关联受管自动任务将同步暂停。",
                "workspace_notice": "NVDA 已有公司 workspace，workspace 和历史文档不会被删除，关联受管自动任务将同步暂停。",
                "job_id": "job-rem",
                "home_synced": True,
            }
        if act == "set_tier":
            return {
                "ok": True,
                "updated": list(kwargs.get("tickers") or []),
                "updated_tickers": list(kwargs.get("tickers") or []),
                "tier": kwargs.get("tier"),
                "job_id": "job-tier",
                "home_synced": True,
            }
        return {
            "ok": True,
            "added": [],
            "added_tickers": list(kwargs.get("tickers") or []),
            "job_id": f"job-{act}",
            "home_synced": True,
        }

    monkeypatch.setattr(
        service_mod.WatchlistMutationService,
        "execute_mutation",
        create_autospec(service_mod.WatchlistMutationService.execute_mutation, side_effect=_fake_exec),
    )

    # 1. Legacy REST POST /coverage (add / remove / set_tier)
    app = FastAPI()
    app.dependency_overrides[get_current_user_id] = lambda: "u-legacy"
    app.include_router(dalton_router)
    client = TestClient(app)

    r_add = client.post("/api/v1/dalton/coverage", json={"action": "add", "tickers": ["NVDA"], "tier": "radar"})
    assert r_add.status_code == 200
    assert r_add.json()["job_id"] == "job-add"

    r_rem = client.post("/api/v1/dalton/coverage", json={"action": "remove", "tickers": ["NVDA"]})
    assert r_rem.status_code == 200
    assert "关联受管自动任务将同步暂停" in r_rem.json()["notice"]

    r_tier = client.post("/api/v1/dalton/coverage", json={"action": "set_tier", "tickers": ["NVDA"], "tier": "core"})
    assert r_tier.status_code == 200
    assert r_tier.json()["job_id"] == "job-tier"

    # 2. Direct Python legacy functions
    await tracking_mod.set_tier_and_resync("u-legacy", ["NVDA"], tier="watch")
    await cov_svc.remove_user_coverage_tickers("u-legacy", ["NVDA"])
    await cov_svc.add_coverage_group("u-legacy", label="AI", criteria={"tickers": ["NVDA"]}, default_tier="radar")
    await cov_svc.remove_coverage_group("u-legacy", "grp-1")

    actions_called = [c["action"] for c in calls]
    assert actions_called == ["add", "remove", "set_tier", "set_tier", "remove", "add_group", "remove_group"]


# ---------------------------------------------------------------------------
# Item 9: calendar_state.py live tier priority & list --tier radar/Radar
# ---------------------------------------------------------------------------


def test_calendar_state_live_tier_priority_and_radar_cli_choices(tmp_path: Path, capsys) -> None:
    cal = _load_script_module(
        "plugins/dalton/skills/dalton-earnings-calendar/scripts/calendar_state.py",
        "calendar_state_r3_test",
    )
    cov_path = tmp_path / "coverage.yaml"
    state_path = tmp_path / "earnings-calendar.json"

    # Coverage has NVDA downgraded to radar & AAPL upgraded to core
    cov_path.write_text(
        """
schema_version: 1
companies:
  - ticker: NVDA
    name: NVIDIA
    tier: radar
    workspace:
      workspace_id: ws-nvda
      status: active
  - ticker: AAPL
    name: Apple
    tier: core
    workspace:
      workspace_id: ws-aapl
      status: active
""",
        encoding="utf-8",
    )
    # Stale ledger still thinks NVDA is Core (with 3 automations) and AAPL is Watch (only t0)
    state_path.write_text(
        json.dumps(
            {
                "updated": "2026-10-01",
                "entries": {
                    "NVDA": {
                        "name": "NVIDIA",
                        "tier": "Core",
                        "workspace_id": "ws-nvda",
                        "status": "confirmed",
                        "date_status": "confirmed",
                        "quarter": "FY26Q3",
                        "date": "2026-11-20",
                        "automations": {
                            "tm3": "dalton-nvda-earn-fy26q3-tm3.json",
                            "t0": "dalton-nvda-earn-fy26q3-t0.json",
                            "tp1": "dalton-nvda-earn-fy26q3-tp1.json",
                        },
                    },
                    "AAPL": {
                        "name": "Apple",
                        "tier": "Watch",
                        "workspace_id": "ws-aapl",
                        "status": "confirmed",
                        "date_status": "confirmed",
                        "quarter": "FY26Q4",
                        "date": "2026-11-05",
                        "automations": {"t0": "dalton-aapl-earn-fy26q4-t0.json"},
                    },
                },
                "reported": [],
            }
        ),
        encoding="utf-8",
    )

    parser = cal.build_parser()

    # list --tier radar & Radar both accepted and show NVDA using LIVE tier
    for flag in ("radar", "Radar"):
        args = parser.parse_args(["--json", "--state", str(state_path), "--coverage", str(cov_path), "list", "--tier", flag])
        assert args.fn(args) == 0
        out = json.loads(capsys.readouterr().out)
        assert [c["ticker"] for c in out["companies"]] == ["NVDA"]
        assert out["companies"][0]["tier"] == "Radar"

    # check on AAPL (live core needs tm3, t0, tp1; ledger only has t0 -> recorded=False)
    args_chk = parser.parse_args(["--json", "--state", str(state_path), "--coverage", str(cov_path), "check", "--ticker", "AAPL", "--date", "2026-11-05"])
    assert args_chk.fn(args_chk) == 0
    chk_out = json.loads(capsys.readouterr().out)
    assert chk_out["recorded"] is False

    # mark on NVDA (live radar -> automations rebuilt to {})
    args_mark = parser.parse_args(
        ["--json", "--state", str(state_path), "--coverage", str(cov_path), "mark", "--ticker", "NVDA", "--status", "confirmed", "--quarter", "FY26Q3", "--date", "2026-11-20"]
    )
    assert args_mark.fn(args_mark) == 0
    capsys.readouterr()
    updated_ledger = json.loads(state_path.read_text(encoding="utf-8"))
    assert updated_ledger["entries"]["NVDA"]["tier"] == "Radar"
    assert updated_ledger["entries"]["NVDA"]["automations"] == {}


# ---------------------------------------------------------------------------
# Item 10: enable_tracking radar/empty specs pauses active tasks; CoS respects pauses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_tracking_radar_pauses_active_and_cos_respects_user_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = [
        {
            "automation_id": "a-watch",
            "user_id": "u1",
            "workspace_id": "ws-net",
            "name": "dalton-net-watch",
            "trigger_type": "cron",
            "status": "active",
            "metadata": {"ticker": "NET", "tier": "core"},
        },
        {
            "automation_id": "a-earn",
            "user_id": "u1",
            "workspace_id": "ws-net",
            "name": "dalton-net-earn-fy26q3-t0",
            "trigger_type": "once",
            "status": "paused",
            "metadata": {"ticker": "NET", "paused_by_user": True},
        },
    ]
    skipped_calls: list[list[str]] = []

    @asynccontextmanager
    async def _lock(uid: str, *, conn=None):
        _ = (uid, conn)
        yield "fake-conn"

    async def _update(auto_id: str, uid: str, **kwargs):
        _ = uid
        for r in store:
            if r["automation_id"] == auto_id:
                r.update({k: v for k, v in kwargs.items() if k != "conn"})
                return r
        return None

    async def _skip(ids, **kwargs):
        _ = kwargs
        skipped_calls.append(list(ids))
        return len(ids)

    monkeypatch.setattr(tracking_mod, "_resolve_workspace_ticker", create_autospec(tracking_mod._resolve_workspace_ticker, return_value="NET"))
    monkeypatch.setattr(tracking_mod, "resolve_default_tier", create_autospec(tracking_mod.resolve_default_tier, return_value="radar"))
    monkeypatch.setattr(tracking_mod, "user_automations_lock", create_autospec(tracking_mod.user_automations_lock, side_effect=_lock))
    monkeypatch.setattr(tracking_mod, "list_all_user_automations", create_autospec(tracking_mod.list_all_user_automations, side_effect=lambda uid, *, name_prefix=None, conn=None: [dict(r) for r in store]))
    monkeypatch.setattr(auto_db, "update_automation", create_autospec(auto_db.update_automation, side_effect=_update))
    monkeypatch.setattr(exec_db, "skip_unstarted_executions_for_automations", create_autospec(exec_db.skip_unstarted_executions_for_automations, side_effect=_skip))
    monkeypatch.setattr(tracking_mod, "schedule_dalton_automation_helper", create_autospec(tracking_mod.schedule_dalton_automation_helper, side_effect=AssertionError("must never resume on radar")))

    # Even with force=True, radar must NOT resume any task and MUST pause active watch by policy
    res = await tracking_mod.enable_tracking("u1", "ws-net", force=True)
    assert res["skipped"] is True
    assert res["skip_reason"] == "empty_tier_policy"
    assert store[0]["status"] == "paused"
    assert store[0]["next_run_at"] is None
    assert store[0]["metadata"]["paused_by_policy"] is True
    assert store[1]["status"] == "paused"
    assert skipped_calls == [["a-watch"]]

    # CoS dalton_coverage_tracking passes force=False, respect_user_pause=True even when source="user"
    captured: list[dict] = []

    async def _spy_enable(user_id, workspace_id, **kwargs):
        captured.append({"user_id": user_id, "workspace_id": workspace_id, **kwargs})
        return {"ok": True, "status": "disabled", "skipped": True, "skip_reason": "paused_by_user"}

    monkeypatch.setattr(director_tools_mod, "_verify_tracking_workspace_owner", create_autospec(director_tools_mod._verify_tracking_workspace_owner, return_value="u1"))
    monkeypatch.setattr(director_tools_mod, "enable_tracking", create_autospec(tracking_mod.enable_tracking, side_effect=_spy_enable))
    cos_tool = {t.name: t for t in director_tools_mod.build_director_tools(role="chief_of_staff")}["dalton_coverage_tracking"]
    await cos_tool.ainvoke(
        {"action": "enable", "workspace_id": "ws-net", "source": "user"},
        config={"configurable": {"user_id": "u1", "workspace_id": "home"}},
    )
    assert len(captured) == 1
    assert captured[0]["force"] is False
    assert captured[0]["respect_user_pause"] is True


# ---------------------------------------------------------------------------
# Item 11: Personal company-ticker preflight drains outbox & startup scan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_personal_company_preflight_drains_outbox_and_startup_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drained: list[tuple[str, bool]] = []

    async def _fake_drain(self, user_id: str, *, sync_home: bool = True):
        drained.append((user_id, sync_home))
        return {"ok": True, "processed_jobs": 1, "results": []}

    monkeypatch.setattr(
        reconciler_mod.TierPolicyReconciler,
        "reconcile_pending_jobs",
        create_autospec(reconciler_mod.TierPolicyReconciler.reconcile_pending_jobs, side_effect=_fake_drain),
    )

    # Company-ticker personal preflight (not Home!) still drains personal outbox jobs
    out = await preflight_mod._sync_home_coverage_for_preflight(
        user_id="alice",
        kind="watch",
        ticker="NVDA",
        persist=True,
    )
    assert out is None
    assert drained == [("alice", True)]

    # One-shot startup scan claims and reconciles unfinished jobs across users
    claimed = [
        {"job_id": "j-1", "user_id": "alice", "pending_tickers": ["NVDA"], "yaml_synced": False},
    ]
    reconciled_ids: list[str] = []

    async def _fake_rec_job(self, uid: str, jid: str, *, sync_home: bool = True, _preclaimed_job=None):
        _ = (self, uid, sync_home, _preclaimed_job)
        reconciled_ids.append(jid)
        return {"ok": True, "job_id": jid, "status": "completed"}

    monkeypatch.setattr(
        reconciler_mod,
        "claim_unfinished_reconcile_jobs_for_startup",
        create_autospec(policies_mod.claim_unfinished_reconcile_jobs_for_startup, return_value=claimed),
    )
    monkeypatch.setattr(
        reconciler_mod.TierPolicyReconciler,
        "reconcile_job",
        create_autospec(reconciler_mod.TierPolicyReconciler.reconcile_job, side_effect=_fake_rec_job),
    )
    scan_res = await reconciler_mod.run_startup_reconcile_scan()
    assert scan_res["ok"] is True
    assert scan_res["processed_jobs"] == 1
    assert reconciled_ids == ["j-1"]


# ---------------------------------------------------------------------------
# Item 12: make_price_triggers.py tier policy & PriceMonitorService reload
# ---------------------------------------------------------------------------


def test_make_price_triggers_respects_radar_unknown_tier_and_is_private(tmp_path: Path, monkeypatch, capsys) -> None:
    mpt = _load_script_module(
        "plugins/dalton/skills/dalton-stock-move/scripts/make_price_triggers.py",
        "make_price_triggers_r3_test",
    )
    cov_path = tmp_path / "coverage.yaml"
    out_dir = tmp_path / "out"
    cov_path.write_text(
        """
schema_version: 1
tier_policies:
  core:
    move_enabled: true
    move_threshold_pct: 3.5
  radar:
    move_enabled: false
    move_threshold_pct: 6.0
companies:
  - ticker: NVDA
    tier: core
    market: US
  - ticker: OPENAI
    tier: core
    market: US
    is_private: true
  - ticker: PRIVX
    tier: core
    market: US
    is_private: true
  - ticker: NET
    tier: radar
    market: US
  - ticker: MYST
    tier: mystery_tier
    market: US
""",
        encoding="utf-8",
    )

    # Radar tier -> 0 generated, skipped with tier_policy_move_disabled
    monkeypatch.setattr(
        "sys.argv",
        ["make_price_triggers.py", "--coverage", str(cov_path), "--tier", "radar", "--out-dir", str(out_dir)],
    )
    assert mpt.main() == 0
    res_radar = json.loads(capsys.readouterr().out)
    assert res_radar["generated_companies"] == 0
    assert res_radar["skipped"] == [{"ticker": "NET", "reason": "tier_policy_move_disabled"}]

    # Unknown tier -> 0 generated, skipped with tier_policy_move_disabled
    monkeypatch.setattr(
        "sys.argv",
        ["make_price_triggers.py", "--coverage", str(cov_path), "--tier", "mystery_tier", "--out-dir", str(out_dir)],
    )
    assert mpt.main() == 0
    res_unk = json.loads(capsys.readouterr().out)
    assert res_unk["generated_companies"] == 0
    assert res_unk["skipped"] == [{"ticker": "MYST", "reason": "tier_policy_move_disabled"}]

    # Core tier -> only NVDA generated (threshold 3.5 from tier_policies); OPENAI & PRIVX skipped as private
    monkeypatch.setattr(
        "sys.argv",
        ["make_price_triggers.py", "--coverage", str(cov_path), "--tier", "core", "--out-dir", str(out_dir)],
    )
    assert mpt.main() == 0
    res_core = json.loads(capsys.readouterr().out)
    assert [g["ticker"] for g in res_core["generated"]] == ["NVDA"]
    assert res_core["generated"][0]["threshold_pct"] == 3.5
    assert {s["ticker"] for s in res_core["skipped"]} == {"OPENAI", "PRIVX"}


@pytest.mark.asyncio
async def test_price_monitor_cross_process_automations_reload_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.server.services import price_monitor as pm_mod

    pm = pm_mod.PriceMonitorService()
    load_autos_spy = create_autospec(pm._load_automations, return_value=None)
    load_targets_spy = create_autospec(pm._load_targets, return_value=None)
    monkeypatch.setattr(pm, "_load_automations", load_autos_spy)
    monkeypatch.setattr(pm, "_load_targets", load_targets_spy)
    monkeypatch.setattr(pm_mod, "_TARGET_RELOAD_DEBOUNCE", 0.01)

    task = asyncio.create_task(pm._target_reload_loop())
    try:
        pm.request_automations_reload()
        await asyncio.sleep(0.05)
        assert load_autos_spy.await_count == 1
        assert load_targets_spy.await_count == 0
    finally:
        pm._shutdown_event.set()
        pm._targets_reload_requested.set()
        await asyncio.wait_for(task, timeout=1.0)


# ---------------------------------------------------------------------------
# Item 15: Shared thread / foreign workspace privacy boundary
# ---------------------------------------------------------------------------


def test_shared_and_foreign_turns_strip_watchlist_context_and_withhold_tool() -> None:
    items = [
        SimpleNamespace(type="text", content="<watchlist_context>\nPersonal watchlist snapshot (filter=all, count=2):\n</watchlist_context>"),
        SimpleNamespace(type="widget", widget_type="personal.portfolioWatchlist", data={"tab": "coverage", "rows": []}),
        SimpleNamespace(type="widget", widget_type="personal.portfolioWatchlist", data={"tab": "watchlist", "rows": []}),
        SimpleNamespace(type="text", content="Normal user note"),
    ]

    # Own private turn keeps everything
    kept = privacy_mod.strip_watchlist_context_if_shared(
        items, is_shared=False, user_id="alice", workspace_owner_id="alice"
    )
    assert kept is not None and len(kept) == 4

    # Shared thread strips personal watchlist text and coverage widget, keeping normal items
    shared_stripped = privacy_mod.strip_watchlist_context_if_shared(
        items, is_shared=True, user_id="alice", workspace_owner_id="alice"
    )
    assert shared_stripped is not None
    assert len(shared_stripped) == 2
    assert all(not privacy_mod.is_personal_watchlist_context_item(x) for x in shared_stripped)

    # Foreign colleague workspace also strips personal watchlist context
    colleague_stripped = privacy_mod.strip_watchlist_context_if_shared(
        items, is_shared=False, user_id="alice", workspace_owner_id="bob"
    )
    assert colleague_stripped is not None and len(colleague_stripped) == 2

    # Tool gating: allow_personal_watchlist=False withholds dalton_coverage
    tools_allowed = build_dalton_tools("chief_of_staff", enabled=True, allow_personal_watchlist=True)
    tools_blocked = build_dalton_tools("chief_of_staff", enabled=True, allow_personal_watchlist=False)
    assert any(t.name == "dalton_coverage" for t in tools_allowed)
    assert not any(t.name == "dalton_coverage" for t in tools_blocked)
