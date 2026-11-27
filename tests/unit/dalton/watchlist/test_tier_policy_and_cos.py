"""Unit tests for Batch-2 tier policy preview/commit, DST cron checks, REST endpoints, and CoS confirmation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import json
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.dalton.app import watchlist as watchlist_router_mod
from src.dalton.autonomy import schedule as schedule_mod
from src.dalton.database import coverage as coverage_db
from src.dalton.tools.coverage_tools import build_coverage_tools
from src.dalton.watchlist import policies as policies_mod
from src.dalton.watchlist import reconciler as reconciler_mod
from src.dalton.watchlist import service as service_mod
from src.ptc_agent.agent.prompts import loader as prompt_loader_mod
from src.server.handlers import checkpoint_handler
from src.server.utils.api import get_current_user_id
from src.tools.secretary import approvals as approvals_mod
from src.tools.secretary._commands import hitl_confirm as real_hitl_confirm


def _fake_db_ctx() -> Any:
    class _Tx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            return False

    class _Conn:
        def transaction(self) -> _Tx:
            return _Tx()

    @asynccontextmanager
    async def _ctx() -> Any:
        yield _Conn()

    return _ctx


def test_tier_policy_dst_cron_and_range_validation() -> None:
    """Validate DST-aware watch/delta cron intervals, threshold bounds, and earnings stages."""
    # Valid watch crons
    assert policies_mod.validate_watch_cron("*/30 6-20 * * 1-5") == "*/30 6-20 * * 1-5"
    assert policies_mod.validate_watch_cron("0 6-20 * * 1-5") == "0 6-20 * * 1-5"
    assert policies_mod.validate_watch_cron("30 7 * * 1-5") == "30 7 * * 1-5"

    # Rejects < 30 min watch cron & non-5-field
    with pytest.raises(ValueError, match="30 分钟"):
        policies_mod.validate_watch_cron("*/15 6-20 * * 1-5")
    with pytest.raises(ValueError, match="5 段"):
        policies_mod.validate_watch_cron("0 9 * *")

    # Valid delta cron across US DST spring-forward (23h gap is permitted!)
    cron_us, tz_us = policies_mod.validate_delta_cron("0 9 * * 1-5", "America/New_York")
    assert cron_us == "0 9 * * 1-5"
    assert tz_us == "America/New_York"

    # Rejects > 1 run per local day
    with pytest.raises(ValueError, match="每日最多触发 1 次"):
        policies_mod.validate_delta_cron("0 9,15 * * 1-5", "Asia/Shanghai")

    # Rejects < 23h consecutive interval across weekdays (23:00 Mon -> 01:00 Tue = 2h)
    with pytest.raises(ValueError, match="每日最多触发 1 次|23 小时"):
        policies_mod.validate_delta_cron("0 1,23 * * 1-5", "America/New_York")

    # Threshold bounds (including non-finite NaN/Inf) & earnings stage ordering
    assert policies_mod.validate_move_threshold_pct(1.0) == 1.0
    assert policies_mod.validate_move_threshold_pct(30.0) == 30.0
    for bad_pct in (0.5, 31.0, True, "NaN", float("nan"), float("inf"), "-inf"):
        with pytest.raises(ValueError):
            policies_mod.validate_move_threshold_pct(bad_pct)

    assert policies_mod.validate_earnings_stages(["tp1", "tm3"]) == ("tm3", "tp1")
    assert policies_mod.validate_earnings_stages([]) == ()
    with pytest.raises(ValueError):
        policies_mod.validate_earnings_stages(["t99"])


def test_cron_validation_injectable_clock_dst_spring_fall_and_sparse() -> None:
    """Cron scan starts at injectable UTC clock for 370 days; covers DST spring-forward, fall-back, and sparse 23h."""
    clock_2026 = lambda: datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    clock_2028 = lambda: datetime(2028, 1, 1, 0, 0, tzinfo=UTC)

    # 1. DST spring-forward (2026-03-08 in America/New_York):
    # Daily & sparse weekend / 2-day expressions have consecutive UTC fires exactly 23 hours apart -> valid!
    assert policies_mod.validate_delta_cron("0 9 * * *", "America/New_York", clock=clock_2026) == (
        "0 9 * * *",
        "America/New_York",
    )
    assert policies_mod.validate_delta_cron("0 9 * * 0,6", "America/New_York", clock=clock_2026) == (
        "0 9 * * 0,6",
        "America/New_York",
    )
    assert policies_mod.validate_delta_cron("0 9 7,8 3 *", "America/New_York", clock=clock_2026) == (
        "0 9 7,8 3 *",
        "America/New_York",
    )

    # 2. DST fall-back (2026-11-01 in America/New_York):
    # 01:30 local fires twice on 2026-11-01 (EDT + EST, 1h apart) -> rejected for delta_cron!
    with pytest.raises(ValueError, match="每日最多触发 1 次|23 小时"):
        policies_mod.validate_delta_cron("30 1 * * *", "America/New_York", clock=clock_2026)
    # Every-30-min watch cron keeps 30m UTC spacing across fall-back -> valid!
    assert (
        policies_mod.validate_watch_cron("*/30 0-3 * * *", timezones=("America/New_York",), clock=clock_2026)
        == "*/30 0-3 * * *"
    )

    # 3. Injectable clock genuinely moves the 370-day window (not hardcoded to 2026-01-01):
    # Feb 29 has zero fires in 2026..2027 (rejected under 2026 clock),
    # whereas 2028 is a leap year with a fire inside the 370-day window (accepted under 2028 clock).
    with pytest.raises(ValueError, match="无触发时间"):
        policies_mod.validate_delta_cron("0 9 29 2 *", "America/New_York", clock=clock_2026)
    assert policies_mod.validate_delta_cron("0 9 29 2 *", "America/New_York", clock=clock_2028) == (
        "0 9 29 2 *",
        "America/New_York",
    )


@pytest.mark.asyncio
async def test_tier_policy_preview_commit_and_scope_changed_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preview issues server snapshot; execute verifies drift, updates policy, and runs outbox reconciler."""
    uid = "user-policy-1"
    rows = [
        {"user_id": uid, "ticker": "NVDA", "name": "NVIDIA", "tier": "core", "is_private": False},
        {"user_id": uid, "ticker": "AMD", "name": "AMD", "tier": "watch", "is_private": False},
    ]
    current_policies = dict(policies_mod.DEFAULT_TIER_POLICIES)
    roster_state = {"roster_revision": 3, "last_yaml_synced_revision": 3}
    snapshots: dict[str, dict[str, Any]] = {}
    outbox_jobs: list[dict[str, Any]] = []

    monkeypatch.setattr(coverage_db, "get_db_connection", _fake_db_ctx())
    monkeypatch.setattr(
        coverage_db,
        "acquire_coverage_advisory_lock",
        create_autospec(coverage_db.acquire_coverage_advisory_lock, return_value=None),
    )
    monkeypatch.setattr(
        coverage_db,
        "list_coverage_rows",
        create_autospec(coverage_db.list_coverage_rows, side_effect=lambda *_a, **_kw: list(rows)),
    )
    monkeypatch.setattr(
        coverage_db,
        "count_coverage_rows",
        create_autospec(coverage_db.count_coverage_rows, return_value=len(rows)),
    )
    monkeypatch.setattr(
        service_mod,
        "_user_workspace_tickers",
        create_autospec(service_mod._user_workspace_tickers, return_value={"NVDA": "ws-nvda"}),
    )
    monkeypatch.setattr(
        service_mod,
        "resolve_company_workspace_id",
        create_autospec(
            service_mod.resolve_company_workspace_id,
            side_effect=lambda _u, sym, **_kw: "ws-nvda" if sym == "NVDA" else None,
        ),
    )
    monkeypatch.setattr(
        service_mod,
        "resolve_workspace_tracking_owner",
        create_autospec(service_mod.resolve_workspace_tracking_owner, return_value=uid),
    )
    monkeypatch.setattr(
        service_mod,
        "list_all_user_automations",
        create_autospec(
            service_mod.list_all_user_automations,
            return_value=[
                {
                    "automation_id": "auto-watch",
                    "workspace_id": "ws-nvda",
                    "name": "dalton-nvda-watch",
                    "trigger_type": "cron",
                    "cron_expression": "*/30 6-20 * * 1-5",
                    "timezone": "America/New_York",
                    "status": "active",
                    "metadata": {"tier": "core"},
                },
                {
                    "automation_id": "auto-move-up",
                    "workspace_id": "ws-nvda",
                    "name": "dalton-nvda-move-up",
                    "trigger_type": "price",
                    "trigger_config": {"conditions": [{"value": 4.0}]},
                    "status": "active",
                    "metadata": {"tier": "core"},
                },
            ],
        ),
    )
    monkeypatch.setattr(
        service_mod,
        "get_user_tier_policies",
        create_autospec(
            policies_mod.get_user_tier_policies,
            side_effect=lambda *_a, **_kw: dict(current_policies),
        ),
    )
    monkeypatch.setattr(
        service_mod,
        "get_user_roster_state",
        create_autospec(
            policies_mod.get_user_roster_state,
            side_effect=lambda *_a, **_kw: dict(roster_state),
        ),
    )

    async def _fake_insert_snap(_u: str, payload: dict[str, Any], **_kw: Any) -> str:
        sid = f"00000000-0000-0000-0000-00000000000{len(snapshots) + 1}"
        snapshots[sid] = {"snapshot_id": sid, "payload": dict(payload), "consumed_at": None, "is_expired": False}
        return sid

    async def _fake_get_snap(_u: str, sid: str, **_kw: Any) -> dict[str, Any] | None:
        return snapshots.get(sid)

    async def _fake_consume_snap(_u: str, sid: str, **_kw: Any) -> bool:
        s = snapshots.get(sid)
        if not s or s["consumed_at"] is not None:
            return False
        s["consumed_at"] = "now"
        return True

    async def _fake_upsert_policy(_u: str, tier: str, *, policy: Any, **_kw: Any) -> policies_mod.TierPolicy:
        prev = current_policies[tier]
        # Pass ``policy`` directly without pre-filtering so any stray metadata keys fail immediately!
        merged = policies_mod.validate_tier_policy_update(prev, policy)
        updated = policies_mod.TierPolicy(
            **{**merged.to_dict(), "earnings_stages": tuple(merged.earnings_stages), "policy_version": prev.policy_version + 1}
        )
        current_policies[tier] = updated
        return updated

    async def _fake_bump(_u: str, **_kw: Any) -> int:
        roster_state["roster_revision"] += 1
        return roster_state["roster_revision"]

    async def _fake_insert_job(_u: str, **kw: Any) -> dict[str, Any]:
        job = {"job_id": f"job-{len(outbox_jobs) + 1}", **kw}
        outbox_jobs.append(job)
        return job

    monkeypatch.setattr(service_mod, "insert_confirm_snapshot", create_autospec(policies_mod.insert_confirm_snapshot, side_effect=_fake_insert_snap))
    monkeypatch.setattr(service_mod, "get_confirm_snapshot", create_autospec(policies_mod.get_confirm_snapshot, side_effect=_fake_get_snap))
    monkeypatch.setattr(service_mod, "consume_confirm_snapshot", create_autospec(policies_mod.consume_confirm_snapshot, side_effect=_fake_consume_snap))
    monkeypatch.setattr(service_mod, "upsert_tier_policy", create_autospec(policies_mod.upsert_tier_policy, side_effect=_fake_upsert_policy))
    monkeypatch.setattr(service_mod, "bump_user_roster_revision", create_autospec(policies_mod.bump_user_roster_revision, side_effect=_fake_bump))
    monkeypatch.setattr(service_mod, "insert_reconcile_job", create_autospec(policies_mod.insert_reconcile_job, side_effect=_fake_insert_job))
    monkeypatch.setattr(
        service_mod,
        "ensure_user_earnings_calendar_automation",
        create_autospec(service_mod.ensure_user_earnings_calendar_automation, return_value={"action": "exists"}),
    )

    fake_reconciler = create_autospec(reconciler_mod.TierPolicyReconciler, instance=True)
    fake_reconciler.reconcile_job.return_value = {"status": "completed", "yaml_synced": True, "failed_items": []}
    svc = service_mod.WatchlistMutationService(reconciler=fake_reconciler)

    # 1. Preview set_tier_policy for core (hourly watch + ±6% move)
    prev = await svc.preview_mutation(
        uid,
        action="set_tier_policy",
        tier="core",
        policy={"watch_cron": "0 6-20 * * 1-5", "move_threshold_pct": 6.0},
    )
    assert prev["ok"] is True
    assert prev["requires_confirmation"] is True
    assert prev["action"] == "watchlist_tier_policy"
    assert prev["snapshot_id"] is not None
    assert prev["expected_policy_version"] == 1
    assert set(prev["policy_diff"]["changed_fields"]) == {"watch_cron", "move_threshold_pct"}
    assert prev["target_tickers"] == ["NVDA"]
    assert prev["task_summary"]["update_count"] >= 2  # watch cron + move-up threshold

    # 2. Drift check: bumping policy_version before commit triggers WatchlistScopeChangedError with zero write
    current_policies["core"] = policies_mod.TierPolicy(
        **{**current_policies["core"].to_dict(), "earnings_stages": tuple(current_policies["core"].earnings_stages), "policy_version": 9}
    )
    with pytest.raises(service_mod.WatchlistScopeChangedError):
        await svc.execute_mutation(
            uid,
            action="set_tier_policy",
            tier="core",
            snapshot_id=prev["snapshot_id"],
            expected_policy_version=1,
            expected_roster_hash=prev["expected_roster_hash"],
        )
    assert len(outbox_jobs) == 0

    # 3. Fresh preview + commit succeeds, increments policy_version to 10, enqueues NVDA in outbox
    prev2 = await svc.preview_mutation(
        uid,
        action="set_tier_policy",
        tier="core",
        policy={"watch_cron": "0 6-20 * * 1-5", "move_threshold_pct": 6.0},
    )
    res = await svc.execute_mutation(
        uid,
        action="set_tier_policy",
        tier="core",
        snapshot_id=prev2["snapshot_id"],
        expected_policy_version=prev2["expected_policy_version"],
        expected_roster_hash=prev2["expected_roster_hash"],
    )
    assert res["ok"] is True
    assert res["policy"]["policy_version"] == 10
    assert res["policy"]["watch_cron"] == "0 6-20 * * 1-5"
    assert res["policy"]["move_threshold_pct"] == 6.0
    assert res["reconcile_status"] == "completed"
    assert outbox_jobs[-1]["pending_tickers"] == ["NVDA"]
    assert outbox_jobs[-1]["target_policy_versions"]["core"] == 10

    # 4. Reset tier policy returns to defaults while strictly bumping policy_version (10 -> 11)
    prev_reset = await svc.preview_mutation(uid, action="reset_tier_policy", tier="core")
    assert prev_reset["after_policy"]["watch_cron"] == policies_mod.WATCH_CRON_CORE_DEFAULT
    res_reset = await svc.execute_mutation(
        uid,
        action="reset_tier_policy",
        tier="core",
        snapshot_id=prev_reset["snapshot_id"],
    )
    assert res_reset["ok"] is True
    assert res_reset["policy"]["watch_cron"] == policies_mod.WATCH_CRON_CORE_DEFAULT
    assert res_reset["policy"]["policy_version"] == 11


@pytest.mark.asyncio
async def test_cos_tool_and_middleware_snapshot_for_tier_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CoS `dalton_coverage` supports get/set/reset_tier_policy with StandingApprovalMiddleware snapshot & checkpoint check."""
    uid = "user-cos-policy"
    fake_preview = {
        "ok": True,
        "action": "watchlist_tier_policy",
        "raw_action": "set_tier_policy",
        "tool_call_id": "tc-pol-1",
        "snapshot_id": "00000000-0000-0000-0000-000000000099",
        "requires_confirmation": True,
        "owner_user_id": uid,
        "target_tickers": ["NVDA"],
        "target_tier": "core",
        "expected_roster_hash": "hash-abc",
        "expected_policy_versions": {"core": 1},
        "affected_tickers": [{"ticker": "NVDA", "old_tier": "core", "new_tier": "core"}],
        "affected_workspaces": [{"ticker": "NVDA", "workspace_id": "ws-1", "task_changes": []}],
        "task_summary": {"update_count": 1},
        "policy_diff": {
            "tier": "core",
            "before": policies_mod.DEFAULT_TIER_POLICIES["core"].to_dict(),
            "after": {**policies_mod.DEFAULT_TIER_POLICIES["core"].to_dict(), "move_threshold_pct": 6.0},
            "changed_fields": ["move_threshold_pct"],
        },
        "card_title": "确认修改 核心 (Core) 监控策略",
        "card_description": "更新核心策略",
    }

    preview_mock = create_autospec(service_mod.WatchlistMutationService.preview_mutation, return_value=fake_preview)
    execute_mock = create_autospec(
        service_mod.WatchlistMutationService.execute_mutation,
        return_value={"ok": True, "tier": "core", "policy": fake_preview["policy_diff"]["after"], "reconcile_status": "completed"},
    )
    monkeypatch.setattr(service_mod.WatchlistMutationService, "preview_mutation", preview_mock)
    monkeypatch.setattr(service_mod.WatchlistMutationService, "execute_mutation", execute_mock)

    # 1. Middleware builds and freezes snapshot for set_tier_policy
    snap = await approvals_mod.build_watchlist_confirmation_snapshot(
        uid,
        {
            "id": "tc-pol-1",
            "name": "dalton_coverage",
            "args": {"action": "set_tier_policy", "tier": "core", "policy": {"move_threshold_pct": 6.0}},
        },
    )
    assert snap is not None
    assert snap["action"] == "watchlist_tier_policy"
    assert snap["policy_diff"]["changed_fields"] == ["move_threshold_pct"]

    # 2. checkpoint_handler verifies snapshot matches card payload & rejects tampered policy_diff
    card_req = {
        "type": "watchlist_tier_policy",
        "tool_call_id": "tc-pol-1",
        "snapshot_id": snap["snapshot_id"],
        "owner_user_id": uid,
        "expected_roster_hash": "hash-abc",
        "expected_policy_versions": {"core": 1},
        "target_tickers": ["NVDA"],
        "target_tier": "core",
        "affected_workspaces": snap["affected_workspaces"],
        "policy_diff": snap["policy_diff"],
    }
    assert checkpoint_handler._snapshot_matches_action(snap, card_req) is True
    tampered = {**card_req, "policy_diff": {"changed_fields": ["watch_enabled"]}}
    assert checkpoint_handler._snapshot_matches_action(snap, tampered) is False

    # 3. Tool invocation: get_tier_policy returns without interrupt
    monkeypatch.setattr(
        "src.dalton.tools.coverage_tools.get_user_tier_policies",
        create_autospec(policies_mod.get_user_tier_policies, return_value=dict(policies_mod.DEFAULT_TIER_POLICIES)),
    )
    cov_tool = build_coverage_tools(role="chief_of_staff")[0]
    res_get = await cov_tool.coroutine(
        action="get_tier_policy",
        config={"configurable": {"user_id": uid}},
        state={},
        tool_call_id="tc-get-1",
    )
    assert '"policies"' in res_get.update["messages"][0].content
    assert '"presets"' in res_get.update["messages"][0].content

    # 4. Tool invocation: set_tier_policy uses frozen snapshot & hitl_confirm -> executes on approve
    hitl_calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_hitl(action_type: str, payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        hitl_calls.append((action_type, payload))
        return True, {"decisions": [{"type": "approve"}]}

    monkeypatch.setattr(
        "src.dalton.tools.coverage_tools.hitl_confirm",
        create_autospec(real_hitl_confirm, side_effect=_fake_hitl),
    )
    res_set = await cov_tool.coroutine(
        action="set_tier_policy",
        tier="core",
        policy={"move_threshold_pct": 6.0},
        config={"configurable": {"user_id": uid}},
        state={"approval_snapshots": {"tc-pol-1": snap}},
        tool_call_id="tc-pol-1",
    )
    assert len(hitl_calls) == 1
    assert hitl_calls[0][0] == "watchlist_tier_policy"
    assert hitl_calls[0][1]["policy_diff"]["changed_fields"] == ["move_threshold_pct"]
    assert '"ok": true' in res_set.update["messages"][0].content


def test_tier_policy_rest_endpoints_validation_and_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """FastAPI tier-policy routes return 400 on invalid DST cron and 409 on scope_changed."""
    app = FastAPI()
    app.include_router(watchlist_router_mod.router)
    app.dependency_overrides[get_current_user_id] = lambda: "u-rest-1"
    monkeypatch.setattr(
        watchlist_router_mod,
        "get_dalton_settings",
        lambda: type("_S", (), {"enabled": True})(),
    )
    monkeypatch.setattr(
        watchlist_router_mod,
        "get_user_tier_policies",
        create_autospec(
            policies_mod.get_user_tier_policies,
            return_value=dict(policies_mod.DEFAULT_TIER_POLICIES),
        ),
    )

    client = TestClient(app)

    # GET /tier-policies returns policies, defaults, presets
    r_get = client.get("/api/v1/dalton/watchlist/tier-policies")
    assert r_get.status_code == 200
    body = r_get.json()
    assert set(body["policies"].keys()) == {"core", "watch", "radar"}
    assert "presets" in body and "defaults" in body

    # POST /tier-policies/preview returns 400 on DST < 30m violation
    monkeypatch.setattr(
        service_mod.WatchlistMutationService,
        "preview_mutation",
        create_autospec(
            service_mod.WatchlistMutationService.preview_mutation,
            side_effect=ValueError("盘中盯盘相邻触发间隔不得小于 30 分钟"),
        ),
    )
    r_bad = client.post(
        "/api/v1/dalton/watchlist/tier-policies/preview",
        json={"tier": "core", "watch_cron": "*/10 6-20 * * 1-5"},
    )
    assert r_bad.status_code == 400
    assert "30 分钟" in r_bad.json()["detail"]

    # PUT /tier-policies/{tier} returns 409 on scope_changed
    monkeypatch.setattr(
        service_mod.WatchlistMutationService,
        "execute_mutation",
        create_autospec(
            service_mod.WatchlistMutationService.execute_mutation,
            side_effect=service_mod.WatchlistScopeChangedError(),
        ),
    )
    r_conflict = client.put(
        "/api/v1/dalton/watchlist/tier-policies/core",
        json={"snapshot_id": "00000000-0000-0000-0000-000000000001", "expected_policy_version": 1},
    )
    assert r_conflict.status_code == 409
    assert r_conflict.json()["detail"]["code"] == "scope_changed"


def test_cos_prompt_mentions_tier_policies_cleanly_in_both_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CoS prompt documents personal watchlist & tier policies with Cron Chinese explanation in both profiles."""
    for env_val in ("dalton", "core"):
        monkeypatch.setenv("APP_ENV", env_val)
        rendered = prompt_loader_mod.PromptLoader().render("components/chief_of_staff.md.j2")
        assert "get_tier_policy" in rendered
        assert "set_tier_policy" in rendered
        assert "reset_tier_policy" in rendered
        assert "Cron 定时时间表达式" in rendered
    _ = schedule_mod  # keep module referenced cleanly


@pytest.mark.asyncio
async def test_tier_policy_commit_uses_real_upsert_validator_across_all_three_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settings confirm, CoS confirm, and Reset ALL invoke the REAL `upsert_tier_policy` + `validate_tier_policy_update`."""
    uid = "u-real-upsert-1"
    rows = [
        {"user_id": uid, "ticker": "NVDA", "name": "NVIDIA", "tier": "core", "is_private": False},
    ]
    stored_policies: dict[str, policies_mod.TierPolicy] = dict(policies_mod.DEFAULT_TIER_POLICIES)
    roster_state = {"roster_revision": 1, "last_yaml_synced_revision": 1}
    snapshots: dict[str, dict[str, Any]] = {}
    outbox_jobs: list[dict[str, Any]] = []

    class _FakeCursor:
        def __init__(self) -> None:
            self._last_row: dict[str, Any] | None = None

        async def __aenter__(self) -> _FakeCursor:
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            return False

        async def execute(self, sql_text: str, params: tuple[Any, ...]) -> None:
            assert "INSERT INTO dalton_watchlist_tier_policies" in sql_text
            (
                p_uid,
                p_tier,
                watch_enabled,
                watch_cron,
                delta_enabled,
                delta_cron,
                delta_tz,
                move_enabled,
                move_pct,
                stages_json,
            ) = params
            prev = stored_policies[p_tier]
            next_ver = 2 if prev.policy_version == 1 else prev.policy_version + 1
            self._last_row = {
                "user_id": p_uid,
                "tier": p_tier,
                "watch_enabled": watch_enabled,
                "watch_cron": watch_cron,
                "delta_enabled": delta_enabled,
                "delta_cron": delta_cron,
                "delta_timezone": delta_tz,
                "move_enabled": move_enabled,
                "move_threshold_pct": move_pct,
                "earnings_stages": json.loads(stages_json),
                "policy_version": next_ver,
            }
            stored_policies[p_tier] = policies_mod.policy_from_mapping(p_tier, self._last_row)

        async def fetchone(self) -> dict[str, Any] | None:
            return self._last_row

    class _Tx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            return False

    class _Conn:
        def transaction(self) -> _Tx:
            return _Tx()

        def cursor(self, **_kw: Any) -> _FakeCursor:
            return _FakeCursor()

    @asynccontextmanager
    async def _db_ctx(conn: Any = None) -> Any:
        yield conn if conn is not None else _Conn()

    # NOTE: Do NOT replace `service_mod.upsert_tier_policy` or `policies_mod.upsert_tier_policy`!
    monkeypatch.setattr(coverage_db, "get_db_connection", _db_ctx)
    monkeypatch.setattr(
        coverage_db,
        "acquire_coverage_advisory_lock",
        create_autospec(coverage_db.acquire_coverage_advisory_lock, return_value=None),
    )
    monkeypatch.setattr(
        coverage_db,
        "list_coverage_rows",
        create_autospec(coverage_db.list_coverage_rows, side_effect=lambda *_a, **_kw: list(rows)),
    )
    monkeypatch.setattr(
        coverage_db,
        "count_coverage_rows",
        create_autospec(coverage_db.count_coverage_rows, return_value=len(rows)),
    )
    monkeypatch.setattr(
        service_mod,
        "_user_workspace_tickers",
        create_autospec(service_mod._user_workspace_tickers, return_value={}),
    )
    monkeypatch.setattr(
        service_mod,
        "resolve_company_workspace_id",
        create_autospec(service_mod.resolve_company_workspace_id, return_value=None),
    )
    monkeypatch.setattr(
        service_mod,
        "resolve_workspace_tracking_owner",
        create_autospec(service_mod.resolve_workspace_tracking_owner, return_value=uid),
    )
    monkeypatch.setattr(
        service_mod,
        "list_all_user_automations",
        create_autospec(service_mod.list_all_user_automations, return_value=[]),
    )
    monkeypatch.setattr(
        service_mod,
        "get_user_tier_policies",
        create_autospec(
            policies_mod.get_user_tier_policies,
            side_effect=lambda *_a, **_kw: dict(stored_policies),
        ),
    )
    monkeypatch.setattr(
        service_mod,
        "get_user_roster_state",
        create_autospec(
            policies_mod.get_user_roster_state,
            side_effect=lambda *_a, **_kw: dict(roster_state),
        ),
    )

    async def _ins_snap(_u: str, payload: dict[str, Any], **_kw: Any) -> str:
        sid = f"00000000-0000-0000-0000-00000000010{len(snapshots) + 1}"
        snapshots[sid] = {"snapshot_id": sid, "payload": dict(payload), "consumed_at": None, "is_expired": False}
        return sid

    async def _get_snap(_u: str, sid: str, **_kw: Any) -> dict[str, Any] | None:
        return snapshots.get(sid)

    async def _consume_snap(_u: str, sid: str, **_kw: Any) -> bool:
        s = snapshots.get(sid)
        if not s or s["consumed_at"] is not None:
            return False
        s["consumed_at"] = "now"
        return True

    async def _bump(_u: str, **_kw: Any) -> int:
        roster_state["roster_revision"] += 1
        return roster_state["roster_revision"]

    async def _ins_job(_u: str, **kw: Any) -> dict[str, Any]:
        job = {"job_id": f"job-real-{len(outbox_jobs) + 1}", **kw}
        outbox_jobs.append(job)
        return job

    monkeypatch.setattr(service_mod, "insert_confirm_snapshot", create_autospec(policies_mod.insert_confirm_snapshot, side_effect=_ins_snap))
    monkeypatch.setattr(service_mod, "get_confirm_snapshot", create_autospec(policies_mod.get_confirm_snapshot, side_effect=_get_snap))
    monkeypatch.setattr(service_mod, "consume_confirm_snapshot", create_autospec(policies_mod.consume_confirm_snapshot, side_effect=_consume_snap))
    monkeypatch.setattr(service_mod, "bump_user_roster_revision", create_autospec(policies_mod.bump_user_roster_revision, side_effect=_bump))
    monkeypatch.setattr(service_mod, "insert_reconcile_job", create_autospec(policies_mod.insert_reconcile_job, side_effect=_ins_job))
    monkeypatch.setattr(
        service_mod,
        "ensure_user_earnings_calendar_automation",
        create_autospec(service_mod.ensure_user_earnings_calendar_automation, return_value={"action": "exists"}),
    )
    monkeypatch.setattr(
        reconciler_mod.TierPolicyReconciler,
        "reconcile_job",
        create_autospec(
            reconciler_mod.TierPolicyReconciler.reconcile_job,
            return_value={"status": "completed", "yaml_synced": True, "failed_items": []},
        ),
    )

    # Path 1: Settings page REST endpoints (preview -> PUT /tier-policies/core)
    app = FastAPI()
    app.include_router(watchlist_router_mod.router)
    app.dependency_overrides[get_current_user_id] = lambda: uid
    monkeypatch.setattr(
        watchlist_router_mod,
        "get_dalton_settings",
        lambda: type("_S", (), {"enabled": True})(),
    )
    client = TestClient(app)

    r_prev = client.post(
        "/api/v1/dalton/watchlist/tier-policies/preview",
        json={"tier": "core", "watch_cron": "0 6-20 * * 1-5", "move_threshold_pct": 6.0},
    )
    assert r_prev.status_code == 200
    prev_body = r_prev.json()
    assert set(snapshots[prev_body["snapshot_id"]]["payload"]["proposed_policy"].keys()) == set(
        policies_mod.TIER_POLICY_MUTABLE_FIELDS
    )

    r_put = client.put(
        "/api/v1/dalton/watchlist/tier-policies/core",
        json={
            "snapshot_id": prev_body["snapshot_id"],
            "expected_policy_version": prev_body["expected_policy_version"],
            "expected_roster_hash": prev_body["expected_roster_hash"],
        },
    )
    assert r_put.status_code == 200, r_put.text
    assert r_put.json()["policy"]["policy_version"] == 2
    assert r_put.json()["policy"]["watch_cron"] == "0 6-20 * * 1-5"
    assert r_put.json()["policy"]["move_threshold_pct"] == 6.0

    # Path 2: Chief of Staff `dalton_coverage` tool confirm (real middleware snapshot -> real execute -> real upsert)
    snap_cos = await approvals_mod.build_watchlist_confirmation_snapshot(
        uid,
        {
            "id": "tc-real-cos-1",
            "name": "dalton_coverage",
            "args": {"action": "set_tier_policy", "tier": "core", "policy": {"move_threshold_pct": 8.0}},
        },
    )
    assert snap_cos is not None
    monkeypatch.setattr(
        "src.dalton.tools.coverage_tools.hitl_confirm",
        create_autospec(real_hitl_confirm, return_value=(True, {"decisions": [{"type": "approve"}]})),
    )
    cov_tool = build_coverage_tools(role="chief_of_staff")[0]
    res_cos = await cov_tool.coroutine(
        action="set_tier_policy",
        tier="core",
        policy={"move_threshold_pct": 8.0},
        config={"configurable": {"user_id": uid}},
        state={"approval_snapshots": {"tc-real-cos-1": snap_cos}},
        tool_call_id="tc-real-cos-1",
    )
    cos_payload = json.loads(res_cos.update["messages"][0].content)
    assert cos_payload["ok"] is True
    assert cos_payload["policy"]["policy_version"] == 3
    assert cos_payload["policy"]["move_threshold_pct"] == 8.0

    # Path 3: Reset endpoint (preview reset -> POST /tier-policies/core/reset -> real upsert)
    r_reset_prev = client.post(
        "/api/v1/dalton/watchlist/tier-policies/preview",
        json={"tier": "core", "action": "reset_tier_policy"},
    )
    assert r_reset_prev.status_code == 200
    reset_prev_body = r_reset_prev.json()
    r_reset = client.post(
        "/api/v1/dalton/watchlist/tier-policies/core/reset",
        json={
            "snapshot_id": reset_prev_body["snapshot_id"],
            "expected_policy_version": reset_prev_body["expected_policy_version"],
            "expected_roster_hash": reset_prev_body["expected_roster_hash"],
        },
    )
    assert r_reset.status_code == 200, r_reset.text
    assert r_reset.json()["policy"]["policy_version"] == 4
    assert r_reset.json()["policy"]["watch_cron"] == policies_mod.WATCH_CRON_CORE_DEFAULT
    assert r_reset.json()["policy"]["move_threshold_pct"] == 4.0
