"""Unit tests for Outbox, per-company lock order, policy monotonicity, pause state matrix, and DST cron checks."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.autonomy import schedule as schedule_mod
from src.dalton.database import coverage as coverage_db
from src.dalton.watchlist import policies as policies_mod
from src.dalton.watchlist import reconciler as reconciler_mod
from src.dalton.watchlist import service as service_mod
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
from src.server.database import workspace as ws_db
from src.server.services.automation_scheduler import AutomationScheduler


class _FakeWatchlistInfra:
    """In-memory harness modeling coverage rows, outbox jobs, automations, and per-company locks."""

    def __init__(self) -> None:
        self.coverage: dict[tuple[str, str], dict[str, Any]] = {}
        self.policies: dict[tuple[str, str], policies_mod.TierPolicy] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.automations: dict[str, dict[str, Any]] = {}
        self.workspaces: dict[tuple[str, str], str] = {}
        self.workspace_owners: dict[str, str] = {}
        self.workspace_rows: dict[str, dict[str, Any]] = {}
        self.lock_events: list[tuple[str, str | None]] = []
        self.skip_calls: list[dict[str, Any]] = []
        self.update_conns: dict[str, list[Any]] = {}
        self.price_nudges = 0
        self.home_sync_result = True
        self.home_sync_calls = 0
        self.fail_tickers: set[str] = set()
        self.schedule_fail_tickers: set[str] = set()
        self._conn_seq = 0

    def add_automation(
        self,
        *,
        user_id: str,
        workspace_id: str,
        name: str,
        trigger_type: str = "cron",
        cron_expression: str | None = "*/30 6-20 * * 1-5",
        timezone_name: str = "America/New_York",
        status: str = "active",
        metadata: dict[str, Any] | None = None,
        trigger_config: dict[str, Any] | None = None,
        next_run_at: datetime | None = None,
        config_version: int = 1,
    ) -> dict[str, Any]:
        aid = str(uuid.uuid4())
        row = {
            "automation_id": aid,
            "user_id": user_id,
            "workspace_id": workspace_id,
            "name": name,
            "description": "",
            "instruction": "",
            "trigger_type": trigger_type,
            "cron_expression": cron_expression,
            "timezone": timezone_name,
            "trigger_config": trigger_config,
            "status": status,
            "next_run_at": next_run_at,
            "config_version": config_version,
            "metadata": dict(metadata or {}),
        }
        self.automations[aid] = row
        return row

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        infra = self

        async def _get_cov(uid: str, ticker: str, *, conn: Any = None) -> dict[str, Any] | None:
            _ = conn
            row = infra.coverage.get((uid, ticker.upper()))
            return dict(row) if row is not None else None

        async def _list_cov(uid: str, *, conn: Any = None) -> list[dict[str, Any]]:
            _ = conn
            return [dict(v) for (u, _), v in infra.coverage.items() if u == uid]

        async def _get_ws(ws_id: str, *, conn: Any = None) -> dict[str, Any] | None:
            _ = conn
            row = infra.workspace_rows.get(ws_id)
            return dict(row) if row is not None else None

        async def _get_policies(uid: str, *, conn: Any = None) -> dict[str, policies_mod.TierPolicy]:
            _ = conn
            out = dict(policies_mod.DEFAULT_TIER_POLICIES)
            for (u, t), p in infra.policies.items():
                if u == uid:
                    out[t] = p
            return out

        async def _insert_job(
            uid: str,
            *,
            trigger_action: str,
            target_policy_versions: Any,
            pending_tickers: Any,
            override_user_pause: bool = False,
            job_context: Any = None,
            job_id: str | None = None,
            conn: Any = None,
        ) -> dict[str, Any]:
            _ = conn
            jid = job_id or str(uuid.uuid4())
            job = {
                "job_id": jid,
                "user_id": uid,
                "trigger_action": trigger_action,
                "target_policy_versions": dict(target_policy_versions),
                "pending_tickers": [str(t).upper() for t in pending_tickers],
                "completed_tickers": [],
                "failed_items": [],
                "override_user_pause": bool(override_user_pause),
                "job_context": dict(job_context or {}),
                "yaml_synced": False,
                "status": "pending",
            }
            infra.jobs[jid] = job
            return dict(job)

        async def _claim_job(
            uid: str,
            jid: str,
            *,
            stale_seconds: int = 300,
            force_claim: bool = False,
            conn: Any = None,
        ) -> dict[str, Any] | None:
            _ = (stale_seconds, conn)
            job = infra.jobs.get(jid)
            if not job or job["user_id"] != uid:
                return None
            if job["status"] == "running" and not force_claim:
                return None
            if job["status"] == "completed" and job.get("yaml_synced"):
                return None
            job["status"] = "running"
            return dict(job)

        async def _get_job(uid: str, jid: str, *, conn: Any = None) -> dict[str, Any] | None:
            _ = conn
            job = infra.jobs.get(jid)
            if job and job["user_id"] == uid:
                return dict(job)
            return None

        async def _list_unfinished(uid: str, *, conn: Any = None) -> list[dict[str, Any]]:
            _ = conn
            return [
                dict(j)
                for j in infra.jobs.values()
                if j["user_id"] == uid
                and (j["status"] in ("pending", "running", "partial") or not j["yaml_synced"])
            ]

        async def _update_job(
            uid: str,
            jid: str,
            *,
            pending_tickers: Any = None,
            completed_tickers: Any = None,
            failed_items: Any = None,
            yaml_synced: bool | None = None,
            status: str | None = None,
            conn: Any = None,
        ) -> dict[str, Any] | None:
            _ = conn
            job = infra.jobs.get(jid)
            if not job or job["user_id"] != uid:
                return None
            if pending_tickers is not None:
                job["pending_tickers"] = list(pending_tickers)
            if completed_tickers is not None:
                job["completed_tickers"] = list(completed_tickers)
            if failed_items is not None:
                job["failed_items"] = [dict(x) for x in failed_items]
            if yaml_synced is not None:
                job["yaml_synced"] = bool(yaml_synced)
            if status is not None:
                job["status"] = status
            return dict(job)

        @asynccontextmanager
        async def _fake_lock(uid: str, *, conn: Any = None, prelock_ws: str | None = None):
            infra._conn_seq += 1
            fake_conn = conn or f"conn-{infra._conn_seq}-{prelock_ws}"
            infra.lock_events.append((uid, prelock_ws))
            yield fake_conn

        async def _resolve_ws(uid: str, ticker: str, *, cov_row: Any = None, conn: Any = None) -> str | None:
            _ = (cov_row, conn)
            return infra.workspaces.get((uid, ticker.upper()))

        async def _resolve_owner(ws_id: str, workspace: Any = None, *, conn: Any = None, unit: Any = None) -> str:
            _ = (workspace, conn, unit)
            return infra.workspace_owners.get(ws_id, "u1")

        async def _list_autos(uid: str, *, name_prefix: str | None = None, conn: Any = None) -> list[dict[str, Any]]:
            _ = conn
            rows = [dict(r) for r in infra.automations.values() if r["user_id"] == uid]
            if name_prefix:
                rows = [r for r in rows if r["name"].lower().startswith(name_prefix.lower())]
            return rows

        async def _update_auto(
            automation_id: str,
            user_id: str,
            *,
            conn: Any = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            row = infra.automations[automation_id]
            assert row["user_id"] == user_id
            for k, v in kwargs.items():
                if k in ("status", "next_run_at", "metadata", "cron_expression", "timezone", "trigger_config", "description", "instruction"):
                    row[k] = v
            infra.update_conns.setdefault(automation_id, []).append(conn)
            return dict(row)

        async def _schedule_helper(
            name: str,
            spec: dict[str, Any],
            *,
            user_id: str,
            workspace_id: str,
            ticker: str | None = None,
            role: str = "chief_of_staff",
            action: str = "upsert",
            conn: Any = None,
            **kwargs: Any,
        ) -> dict[str, Any]:
            _ = (ticker, role, action, conn, kwargs)
            if ticker and ticker.upper() in infra.fail_tickers:
                raise RuntimeError("simulated single-company write error")
            if ticker and ticker.upper() in infra.schedule_fail_tickers:
                return {"ok": False, "error": "schedule_refused"}
            created = infra.add_automation(
                user_id=user_id,
                workspace_id=workspace_id,
                name=name,
                trigger_type=str(spec.get("trigger_type") or "cron"),
                cron_expression=spec.get("cron_expression"),
                timezone_name=str(spec.get("timezone") or "America/New_York"),
                status=str(spec.get("status") or "active"),
                metadata=dict(spec.get("metadata") or {}),
                trigger_config=spec.get("trigger_config"),
                next_run_at=(
                    AutomationScheduler.calculate_first_run(
                        spec["cron_expression"], spec["timezone"]
                    )
                    if spec.get("trigger_type") == "cron" and spec.get("status") == "active"
                    else None
                ),
            )
            return {"ok": True, "automation": created}

        async def _skip_execs(
            automation_ids: Any,
            *,
            reason: str = "unit_handoff",
            error_message: str = "",
            created_at_lte: Any = None,
            conn: Any = None,
        ) -> int:
            infra.skip_calls.append(
                {
                    "automation_ids": list(automation_ids),
                    "reason": reason,
                    "conn": conn,
                }
            )
            return len(list(automation_ids))

        async def _sync_home(uid: str, *, yaml_text: str | None = None, backend: Any = None, rel_path: str = "dalton/coverage.yaml") -> bool:
            _ = (uid, yaml_text, backend, rel_path)
            infra.home_sync_calls += 1
            return infra.home_sync_result

        async def _nudge() -> None:
            infra.price_nudges += 1

        monkeypatch.setattr(
            coverage_db,
            "get_coverage_row",
            create_autospec(coverage_db.get_coverage_row, side_effect=_get_cov),
        )
        monkeypatch.setattr(
            coverage_db,
            "list_coverage_rows",
            create_autospec(coverage_db.list_coverage_rows, side_effect=_list_cov),
        )
        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(ws_db.get_workspace, side_effect=_get_ws),
        )
        monkeypatch.setattr(
            reconciler_mod,
            "get_user_tier_policies",
            create_autospec(policies_mod.get_user_tier_policies, side_effect=_get_policies),
        )
        monkeypatch.setattr(
            reconciler_mod,
            "claim_reconcile_job",
            create_autospec(policies_mod.claim_reconcile_job, side_effect=_claim_job),
        )
        monkeypatch.setattr(
            reconciler_mod,
            "get_reconcile_job",
            create_autospec(policies_mod.get_reconcile_job, side_effect=_get_job),
        )
        monkeypatch.setattr(
            reconciler_mod,
            "list_unfinished_reconcile_jobs",
            create_autospec(policies_mod.list_unfinished_reconcile_jobs, side_effect=_list_unfinished),
        )
        monkeypatch.setattr(
            reconciler_mod,
            "update_reconcile_job_progress",
            create_autospec(policies_mod.update_reconcile_job_progress, side_effect=_update_job),
        )
        monkeypatch.setattr(
            service_mod,
            "insert_reconcile_job",
            create_autospec(policies_mod.insert_reconcile_job, side_effect=_insert_job),
        )
        monkeypatch.setattr(
            reconciler_mod,
            "_user_automations_lock_for_write",
            create_autospec(
                reconciler_mod._user_automations_lock_for_write,
                side_effect=_fake_lock,
            ),
        )
        monkeypatch.setattr(
            reconciler_mod,
            "resolve_company_workspace_id",
            create_autospec(reconciler_mod.resolve_company_workspace_id, side_effect=_resolve_ws),
        )
        monkeypatch.setattr(
            reconciler_mod,
            "resolve_workspace_tracking_owner",
            create_autospec(reconciler_mod.resolve_workspace_tracking_owner, side_effect=_resolve_owner),
        )
        monkeypatch.setattr(
            reconciler_mod,
            "list_all_user_automations",
            create_autospec(schedule_mod.list_all_user_automations, side_effect=_list_autos),
        )
        monkeypatch.setattr(
            auto_db,
            "update_automation",
            create_autospec(auto_db.update_automation, side_effect=_update_auto),
        )
        monkeypatch.setattr(
            reconciler_mod,
            "schedule_dalton_automation_helper",
            create_autospec(schedule_mod.schedule_dalton_automation_helper, side_effect=_schedule_helper),
        )
        monkeypatch.setattr(
            exec_db,
            "skip_unstarted_executions_for_automations",
            create_autospec(exec_db.skip_unstarted_executions_for_automations, side_effect=_skip_execs),
        )
        monkeypatch.setattr(
            reconciler_mod,
            "sync_user_coverage_to_home",
            create_autospec(reconciler_mod.sync_user_coverage_to_home, side_effect=_sync_home),
        )
        monkeypatch.setattr(
            reconciler_mod,
            "_nudge_price_monitor",
            create_autospec(reconciler_mod._nudge_price_monitor, side_effect=_nudge),
        )


@pytest.mark.asyncio
async def test_reconciler_pause_matrix_four_states_and_same_conn_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    infra = _FakeWatchlistInfra()
    infra.install(monkeypatch)
    uid = "u1"
    ws_id = "ws-nvda"
    infra.workspaces[(uid, "NVDA")] = ws_id
    infra.workspace_owners[ws_id] = uid
    infra.coverage[(uid, "NVDA")] = {
        "user_id": uid,
        "ticker": "NVDA",
        "tier": "core",
        "is_private": False,
        "task_overrides": None,
    }

    # Seed 4 tasks covering the four pause states:
    # 1. watch -> paused_by_policy (should auto-resume to active on core!)
    w_row = infra.add_automation(
        user_id=uid,
        workspace_id=ws_id,
        name="dalton-nvda-watch",
        cron_expression="30 7 * * 1-5",
        status="paused",
        metadata={"paused_by_policy": True, "tier": "radar"},
    )
    # 2. delta -> replaced_by_tracker (MUST stay paused, next_run_at=None, record target_policy_cron)
    d_row = infra.add_automation(
        user_id=uid,
        workspace_id=ws_id,
        name="dalton-nvda-delta",
        cron_expression="0 11 * * 6",
        timezone_name="Asia/Shanghai",
        status="paused",
        metadata={"replaced_by_tracker": "tracker-uuid-1", "tier": "watch"},
    )
    # 3. move-up -> paused_by_user (MUST stay paused when override_user_pause=False, params synced)
    mu_row = infra.add_automation(
        user_id=uid,
        workspace_id=ws_id,
        name="dalton-nvda-move-up",
        trigger_type="price",
        cron_expression=None,
        status="paused",
        metadata={"paused_by_user": True, "tier": "watch"},
        trigger_config={"symbol": "NVDA", "conditions": [{"type": "pct_change_above", "value": 6.0}]},
    )
    # 4. move-down -> handoff_paused_reason (MUST stay paused, next_run_at=None, params synced)
    md_row = infra.add_automation(
        user_id=uid,
        workspace_id=ws_id,
        name="dalton-nvda-move-down",
        trigger_type="price",
        cron_expression=None,
        status="paused",
        metadata={"handoff_paused_reason": "unit_transfer", "tier": "watch"},
        trigger_config={"symbol": "NVDA", "conditions": [{"type": "pct_change_below", "value": 6.0}]},
    )

    rec = reconciler_mod.TierPolicyReconciler()
    await rec.reconcile_company(uid, "NVDA", override_user_pause=False)

    # Lock acquired with prelock_ws=ws_id
    assert infra.lock_events == [(uid, ws_id)]

    # 1. paused_by_policy -> resumed active, cron updated to core, next_run_at computed
    w_now = infra.automations[w_row["automation_id"]]
    assert w_now["status"] == "active"
    assert w_now["cron_expression"] == "*/30 6-20 * * 1-5"
    assert w_now["next_run_at"] is not None
    assert "paused_by_policy" not in w_now["metadata"]

    # 2. replaced_by_tracker -> stayed paused, next_run_at=None, target cron recorded
    d_now = infra.automations[d_row["automation_id"]]
    assert d_now["status"] == "paused"
    assert d_now["next_run_at"] is None
    assert d_now["metadata"]["replaced_by_tracker"] == "tracker-uuid-1"
    assert d_now["metadata"]["target_policy_cron"] == "0 11 * * 2-6"

    # 3. paused_by_user -> stayed paused, threshold synced to 4.0%
    mu_now = infra.automations[mu_row["automation_id"]]
    assert mu_now["status"] == "paused"
    assert mu_now["next_run_at"] is None
    assert mu_now["metadata"]["paused_by_user"] is True
    assert mu_now["trigger_config"]["conditions"][0]["value"] == 4.0
    assert "+4%" in mu_now["instruction"]

    # 4. handoff_paused_reason -> stayed paused, threshold synced to 4.0%
    md_now = infra.automations[md_row["automation_id"]]
    assert md_now["status"] == "paused"
    assert md_now["next_run_at"] is None
    assert md_now["metadata"]["handoff_paused_reason"] == "unit_transfer"
    assert md_now["trigger_config"]["conditions"][0]["value"] == 4.0
    assert infra.price_nudges >= 1

    # Explicit override_user_pause=True resumes paused_by_user (price stays next_run_at=None),
    # while STILL keeping replaced_by_tracker and handoff_paused_reason paused!
    await rec.reconcile_company(uid, "NVDA", override_user_pause=True)
    assert infra.automations[mu_row["automation_id"]]["status"] == "active"
    assert infra.automations[mu_row["automation_id"]]["next_run_at"] is None
    assert "paused_by_user" not in infra.automations[mu_row["automation_id"]]["metadata"]
    assert infra.automations[d_row["automation_id"]]["status"] == "paused"
    assert infra.automations[md_row["automation_id"]]["status"] == "paused"

    # Now downgrade to radar -> pauses active tasks + calls skip_unstarted_executions on SAME conn
    infra.coverage[(uid, "NVDA")]["tier"] = "radar"
    await rec.reconcile_company(uid, "NVDA")

    assert infra.automations[w_row["automation_id"]]["status"] == "paused"
    assert infra.automations[w_row["automation_id"]]["next_run_at"] is None
    assert infra.automations[w_row["automation_id"]]["metadata"]["paused_by_policy"] is True
    assert len(infra.skip_calls) == 1
    skip_conn = infra.skip_calls[0]["conn"]
    assert skip_conn is not None
    assert infra.update_conns[w_row["automation_id"]][-1] == skip_conn


@pytest.mark.asyncio
async def test_reconciler_old_batch_never_regresses_newer_policy_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    infra = _FakeWatchlistInfra()
    infra.install(monkeypatch)
    uid = "u1"
    ws_id = "ws-aapl"
    infra.workspaces[(uid, "AAPL")] = ws_id
    infra.workspace_owners[ws_id] = uid
    infra.coverage[(uid, "AAPL")] = {"user_id": uid, "ticker": "AAPL", "tier": "core", "is_private": False}
    # Newer live policy v2 has custom watch_cron "0 9-16 * * 1-5" and move_threshold 6.0%
    infra.policies[(uid, "core")] = policies_mod.TierPolicy(
        tier="core",
        watch_enabled=True,
        watch_cron="0 9-16 * * 1-5",
        delta_enabled=True,
        delta_cron="0 11 * * 2-6",
        delta_timezone="Asia/Shanghai",
        move_enabled=True,
        move_threshold_pct=6.0,
        earnings_stages=("tm3", "t0", "tp1"),
        policy_version=2,
    )
    w_row = infra.add_automation(
        user_id=uid,
        workspace_id=ws_id,
        name="dalton-aapl-watch",
        cron_expression="*/30 6-20 * * 1-5",
        status="active",
    )
    m_row = infra.add_automation(
        user_id=uid,
        workspace_id=ws_id,
        name="dalton-aapl-move-up",
        trigger_type="price",
        cron_expression=None,
        status="active",
        trigger_config={"symbol": "AAPL", "conditions": [{"type": "pct_change_above", "value": 4.0}]},
    )

    # An older outbox job with target_policy_versions={"core": 1} runs AFTER v2 took effect
    jid = str(uuid.uuid4())
    infra.jobs[jid] = {
        "job_id": jid,
        "user_id": uid,
        "trigger_action": "set_tier",
        "target_policy_versions": {"core": 1},
        "pending_tickers": ["AAPL"],
        "completed_tickers": [],
        "failed_items": [],
        "override_user_pause": False,
        "yaml_synced": False,
        "status": "pending",
    }
    res = await reconciler_mod.TierPolicyReconciler().reconcile_job(uid, jid)
    assert res["status"] == "completed"
    # Converged to v2, never regressed to v1
    assert infra.automations[w_row["automation_id"]]["cron_expression"] == "0 9-16 * * 1-5"
    assert infra.automations[m_row["automation_id"]]["trigger_config"]["conditions"][0]["value"] == 6.0


@pytest.mark.asyncio
async def test_reconciler_partial_failure_isolation_and_idempotent_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    infra = _FakeWatchlistInfra()
    infra.install(monkeypatch)
    uid = "u1"
    for sym in ("NVDA", "TSM"):
        ws_id = f"ws-{sym.lower()}"
        infra.workspaces[(uid, sym)] = ws_id
        infra.workspace_owners[ws_id] = uid
        infra.coverage[(uid, sym)] = {"user_id": uid, "ticker": sym, "tier": "core", "is_private": False}

    # TSM fails during first reconcile pass AND Home YAML sync fails initially
    infra.fail_tickers.add("TSM")
    infra.home_sync_result = False

    jid = str(uuid.uuid4())
    infra.jobs[jid] = {
        "job_id": jid,
        "user_id": uid,
        "trigger_action": "set_tier",
        "target_policy_versions": {"core": 1},
        "pending_tickers": ["NVDA", "TSM"],
        "completed_tickers": [],
        "failed_items": [],
        "override_user_pause": False,
        "yaml_synced": False,
        "status": "pending",
    }

    rec = reconciler_mod.TierPolicyReconciler()
    first = await rec.reconcile_job(uid, jid)
    assert first["status"] == "partial"
    assert first["yaml_synced"] is False
    assert first["completed_tickers"] == ["NVDA"]
    assert len(first["failed_items"]) == 1
    assert first["failed_items"][0]["ticker"] == "TSM"
    # Independent per-company locks acquired sequentially
    assert infra.lock_events == [(uid, "ws-nvda"), (uid, "ws-tsm")]

    # Recovery after restart: clear transient failure and allow Home sync
    infra.fail_tickers.clear()
    infra.home_sync_result = True
    recovery = await rec.reconcile_pending_jobs(uid)
    assert recovery["ok"] is True
    assert recovery["processed_jobs"] == 1
    assert infra.jobs[jid]["status"] == "completed"
    assert infra.jobs[jid]["yaml_synced"] is True
    assert set(infra.jobs[jid]["completed_tickers"]) == {"NVDA", "TSM"}
    assert infra.jobs[jid]["failed_items"] == []

    # Second recovery call is a clean idempotent no-op
    recovery2 = await rec.reconcile_pending_jobs(uid)
    assert recovery2["processed_jobs"] == 0


@pytest.mark.asyncio
async def test_reconciler_remove_pauses_standard_and_future_earnings_on_same_conn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    infra = _FakeWatchlistInfra()
    infra.install(monkeypatch)
    uid = "u1"
    ws_id = "ws-meta"
    infra.workspaces[(uid, "META")] = ws_id
    infra.workspace_owners[ws_id] = uid
    # META is absent from infra.coverage -> removed!
    w_row = infra.add_automation(user_id=uid, workspace_id=ws_id, name="dalton-meta-watch", status="active")
    future_earn = infra.add_automation(
        user_id=uid,
        workspace_id=ws_id,
        name="dalton-meta-earn-fy26q4-t0",
        trigger_type="once",
        cron_expression=None,
        status="active",
        next_run_at=datetime.now(UTC) + timedelta(days=5),
    )

    await reconciler_mod.TierPolicyReconciler().reconcile_company(uid, "META")
    assert infra.automations[w_row["automation_id"]]["status"] == "paused"
    assert infra.automations[future_earn["automation_id"]]["status"] == "paused"
    assert len(infra.skip_calls) == 1
    assert set(infra.skip_calls[0]["automation_ids"]) == {
        w_row["automation_id"],
        future_earn["automation_id"],
    }
    assert infra.skip_calls[0]["conn"] == infra.update_conns[w_row["automation_id"]][-1]


def test_dst_aware_cron_and_threshold_validation() -> None:
    # Valid defaults pass 370-day DST scan
    assert policies_mod.validate_watch_cron("*/30 6-20 * * 1-5") == "*/30 6-20 * * 1-5"
    assert policies_mod.validate_delta_cron("0 11 * * 2-6", "Asia/Shanghai") == (
        "0 11 * * 2-6",
        "Asia/Shanghai",
    )
    # US Eastern DST spring-forward day has a 23h gap between consecutive 09:00 fires -> allowed (>=23h)
    assert policies_mod.validate_delta_cron("0 9 * * 1-5", "America/New_York") == (
        "0 9 * * 1-5",
        "America/New_York",
    )

    # Watch < 30 min interval rejected
    with pytest.raises(ValueError, match="30 分钟"):
        policies_mod.validate_watch_cron("*/15 6-20 * * 1-5")

    # Delta twice per local day rejected
    with pytest.raises(ValueError, match="每日最多触发 1 次"):
        policies_mod.validate_delta_cron("0 9,18 * * 1-5", "Asia/Shanghai")

    # Move threshold bounds
    assert policies_mod.validate_move_threshold_pct(4.5) == 4.5
    with pytest.raises(ValueError):
        policies_mod.validate_move_threshold_pct(0.5)
    with pytest.raises(ValueError):
        policies_mod.validate_move_threshold_pct(35.0)
    with pytest.raises(ValueError):
        policies_mod.validate_move_threshold_pct(True)

    # Earnings stages normalization
    assert policies_mod.validate_earnings_stages(["tp1", "tm3"]) == ("tm3", "tp1")
    with pytest.raises(ValueError):
        policies_mod.validate_earnings_stages(["t99"])


@pytest.mark.asyncio
async def test_r3_reconciler_removed_workspace_rebound_check_and_ok_false_and_exclusive_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Items 6 & 7: removed workspace verified inside lock; ok=False fails ticker; active claim skips duplicate."""
    infra = _FakeWatchlistInfra()
    infra.install(monkeypatch)
    uid = "u1"

    # Item 6: META removed, but its old workspace ws-meta was rebound to AMD -> must NOT pause!
    infra.workspace_owners["ws-meta"] = uid
    infra.workspace_rows["ws-meta"] = {"workspace_id": "ws-meta", "name": "AMD", "status": "active"}
    w_row = infra.add_automation(user_id=uid, workspace_id="ws-meta", name="dalton-meta-watch", status="active")
    res_rebound = await reconciler_mod.TierPolicyReconciler().reconcile_company(
        uid, "META", removed_workspace_id="ws-meta"
    )
    assert res_rebound["skipped"] is True
    assert res_rebound["reason"] == "workspace_ticker_changed"
    assert infra.automations[w_row["automation_id"]]["status"] == "active"

    # Item 7: schedule_dalton_automation_helper returning ok=False marks ticker failed & job partial
    infra.workspaces[(uid, "NVDA")] = "ws-nvda"
    infra.workspace_owners["ws-nvda"] = uid
    infra.coverage[(uid, "NVDA")] = {"user_id": uid, "ticker": "NVDA", "tier": "core", "is_private": False}
    infra.schedule_fail_tickers.add("NVDA")
    jid = str(uuid.uuid4())
    infra.jobs[jid] = {
        "job_id": jid,
        "user_id": uid,
        "trigger_action": "set_tier",
        "target_policy_versions": {"core": 1},
        "pending_tickers": ["NVDA"],
        "completed_tickers": [],
        "failed_items": [],
        "override_user_pause": False,
        "job_context": {},
        "yaml_synced": False,
        "status": "pending",
    }
    res_fail = await reconciler_mod.TierPolicyReconciler().reconcile_job(uid, jid)
    assert res_fail["status"] == "partial"
    assert len(res_fail["failed_items"]) == 1
    assert res_fail["failed_items"][0]["ticker"] == "NVDA"

    # Item 7: job already running/claimed returns claimed=False, reason=already_claimed
    infra.jobs[jid]["status"] = "running"
    res_dup = await reconciler_mod.TierPolicyReconciler().reconcile_job(uid, jid)
    assert res_dup["claimed"] is False
    assert res_dup["reason"] == "already_claimed"


@pytest.mark.asyncio
async def test_r3_scoped_override_user_pause_disabled_and_dotted_ticker_earnings_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Items 8 & 9: version-bound override_user_pause, disabled preservation, 0700.HK slug & earnings stage pause."""
    infra = _FakeWatchlistInfra()
    infra.install(monkeypatch)
    uid = "u1"
    ws_id = "ws-0700"
    infra.workspaces[(uid, "0700.HK")] = ws_id
    infra.workspace_owners[ws_id] = uid
    infra.coverage[(uid, "0700.HK")] = {
        "user_id": uid,
        "ticker": "0700.HK",
        "tier": "watch",
        "is_private": False,
    }

    # 1. User paused move-up at version 2 (confirmation was at version 1!) -> must NOT clear pause
    move_up = infra.add_automation(
        user_id=uid,
        workspace_id=ws_id,
        name="dalton-0700-hk-move-up",
        trigger_type="price",
        cron_expression=None,
        status="paused",
        config_version=2,
        metadata={"paused_by_user": True},
    )
    # 2. Disabled move-down -> must NEVER turn back to active
    move_down = infra.add_automation(
        user_id=uid,
        workspace_id=ws_id,
        name="dalton-0700.hk-move-down",
        trigger_type="price",
        cron_expression=None,
        status="disabled",
        metadata={"paused_by_policy": True},
    )
    # 3. Future earnings tasks: tm3 (dropped in watch!) vs t0 (kept in watch!)
    future_tm3 = infra.add_automation(
        user_id=uid,
        workspace_id=ws_id,
        name="dalton-0700-hk-earn-fy26q4-tm3",
        trigger_type="once",
        cron_expression=None,
        status="active",
        next_run_at=datetime.now(UTC) + timedelta(days=4),
        metadata={"paused_by_user": False},
    )
    future_t0 = infra.add_automation(
        user_id=uid,
        workspace_id=ws_id,
        name="dalton-0700-hk-earn-fy26q4-t0",
        trigger_type="once",
        cron_expression=None,
        status="active",
        next_run_at=datetime.now(UTC) + timedelta(days=7),
    )

    await reconciler_mod.TierPolicyReconciler().reconcile_company(
        uid,
        "0700.HK",
        override_user_pause=True,
        override_pause_versions={move_up["automation_id"]: 1},
    )

    # New user pause (version 2 != 1) was NOT overridden; next_run_at is None
    assert infra.automations[move_up["automation_id"]]["status"] == "paused"
    assert infra.automations[move_up["automation_id"]]["next_run_at"] is None
    assert infra.automations[move_up["automation_id"]]["metadata"]["paused_by_user"] is True

    # Disabled row stayed disabled
    assert infra.automations[move_down["automation_id"]]["status"] == "disabled"

    # tm3 paused by policy with next_run_at=None & skipped on same conn; t0 stayed active
    assert infra.automations[future_tm3["automation_id"]]["status"] == "paused"
    assert infra.automations[future_tm3["automation_id"]]["next_run_at"] is None
    assert infra.automations[future_tm3["automation_id"]]["metadata"]["paused_by_policy"] is True
    assert infra.automations[future_t0["automation_id"]]["status"] == "active"
    assert any(future_tm3["automation_id"] in c["automation_ids"] for c in infra.skip_calls)


@pytest.mark.asyncio
async def test_r3_coverage_units_enabled_uses_real_query_with_conn_and_propagates_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Items 16 & 19: coverage units enabled calls list_active_company_units_with_ticker(conn=conn) & does not swallow DB errors."""
    from src.dalton import capabilities as cap_mod
    from src.dalton import settings as dalton_settings
    from src.dalton.database import coverage_units as cu_db

    monkeypatch.setattr(cap_mod, "has_capability", create_autospec(cap_mod.has_capability, return_value=True))
    monkeypatch.setattr(dalton_settings, "is_coverage_units_enabled", create_autospec(dalton_settings.is_coverage_units_enabled, return_value=True))

    seen_conns: list[Any] = []

    async def _list_units(*, conn: Any = None):
        seen_conns.append(conn)
        return [
            {
                "unit_id": "cu-1",
                "ticker": "NVDA",
                "ticker_key": "NVDA",
                "lead_analyst_user_id": "alice",
                "workspace_id": "ws-unit-nvda",
            }
        ]

    real_list_units = cu_db.list_active_company_units_with_ticker
    monkeypatch.setattr(
        cu_db,
        "list_active_company_units_with_ticker",
        create_autospec(real_list_units, side_effect=_list_units),
    )

    sentinel_conn = object()
    ws = await reconciler_mod.resolve_company_workspace_id("alice", "NVDA", conn=sentinel_conn)
    assert ws == "ws-unit-nvda"
    assert seen_conns == [sentinel_conn]

    # DB errors MUST propagate clearly instead of being silently swallowed
    monkeypatch.setattr(
        cu_db,
        "list_active_company_units_with_ticker",
        create_autospec(real_list_units, side_effect=RuntimeError("pg connection reset")),
    )
    with pytest.raises(RuntimeError, match="pg connection reset"):
        await reconciler_mod.resolve_company_workspace_id("alice", "NVDA", conn=sentinel_conn)
