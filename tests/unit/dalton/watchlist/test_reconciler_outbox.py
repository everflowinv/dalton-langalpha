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
            roster_revision: int = 0,
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
                "claim_token": None,
                "claim_expires_at": None,
                "roster_revision": int(roster_revision or 0),
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
            lease_seconds: int | None = None,
            force_claim: bool = False,
            conn: Any = None,
        ) -> dict[str, Any] | None:
            _ = (stale_seconds, lease_seconds, conn)
            job = infra.jobs.get(jid)
            if not job or job["user_id"] != uid:
                return None
            if (job["status"] == "running" or job.get("claim_token")) and not force_claim:
                return None
            if job["status"] == "completed" and job.get("yaml_synced"):
                return None
            job["status"] = "running"
            job["claim_token"] = str(uuid.uuid4())
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

        async def _renew_lease(
            uid: str,
            jid: str,
            *,
            claim_token: str,
            lease_seconds: int = 300,
            conn: Any = None,
        ) -> bool:
            _ = (lease_seconds, conn)
            job = infra.jobs.get(jid)
            return bool(job and job["user_id"] == uid and job.get("claim_token") == claim_token)

        async def _update_job(
            uid: str,
            jid: str,
            *,
            pending_tickers: Any = None,
            completed_tickers: Any = None,
            failed_items: Any = None,
            yaml_synced: bool | None = None,
            status: str | None = None,
            claim_token: str | None = None,
            release_claim: bool = False,
            only_unclaimed_completed: bool = False,
            lease_seconds: int = 300,
            conn: Any = None,
        ) -> dict[str, Any] | None:
            _ = (lease_seconds, conn)
            job = infra.jobs.get(jid)
            if not job or job["user_id"] != uid:
                return None
            if claim_token is not None and job.get("claim_token") != claim_token:
                return None
            if claim_token is None and only_unclaimed_completed:
                if (
                    job.get("claim_token") is not None
                    or job.get("status") == "running"
                    or bool(job.get("pending_tickers"))
                    or bool(job.get("failed_items"))
                ):
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
            if release_claim:
                job["claim_token"] = None
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

        async def _sync_home(
            uid: str,
            *,
            yaml_text: str | None = None,
            backend: Any = None,
            rel_path: str = "dalton/coverage.yaml",
            roster_revision: int | None = None,
            lease_check: Any = None,
        ) -> bool:
            _ = (uid, yaml_text, backend, rel_path, roster_revision)
            if lease_check is not None and not await lease_check(None):
                return False
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
            "renew_reconcile_job_lease",
            create_autospec(policies_mod.renew_reconcile_job_lease, side_effect=_renew_lease),
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

    # Missing override_pause_versions MUST default to refusing override (r4 Decision 3)
    await rec.reconcile_company(uid, "NVDA", override_user_pause=True)
    assert infra.automations[mu_row["automation_id"]]["status"] == "paused"

    # Explicit version-authorized override_user_pause=True resumes paused_by_user (price stays next_run_at=None),
    # while STILL keeping replaced_by_tracker and handoff_paused_reason paused!
    await rec.reconcile_company(
        uid,
        "NVDA",
        override_user_pause=True,
        override_pause_versions={mu_row["automation_id"]: 1},
    )
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
    infra.workspace_rows[ws_id] = {"workspace_id": ws_id, "name": "META", "status": "active"}
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

    await reconciler_mod.TierPolicyReconciler().reconcile_company(
        uid, "META", removed_workspace_id=ws_id
    )
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


@pytest.mark.asyncio
async def test_r4_outbox_claim_token_lease_loss_first_failure_and_batched_startup_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 Decision 2: first-ticker failure keeps claim_token until job end; lost lease stops writes;
    startup scan drains >batch_size jobs without re-claiming failed rows in the same pass."""
    infra = _FakeWatchlistInfra()
    infra.install(monkeypatch)
    uid = "u1"
    for sym in ("NVDA", "TSM"):
        ws_id = f"ws-{sym.lower()}"
        infra.workspaces[(uid, sym)] = ws_id
        infra.workspace_owners[ws_id] = uid
        infra.coverage[(uid, sym)] = {"user_id": uid, "ticker": sym, "tier": "core", "is_private": False}

    # First ticker NVDA fails, second ticker TSM succeeds -> claim_token must stay held during NVDA failure!
    infra.fail_tickers.add("NVDA")
    observed_updates: list[dict[str, Any]] = []
    orig_upd = reconciler_mod.update_reconcile_job_progress

    async def _spy_upd(*args: Any, **kwargs: Any):
        observed_updates.append(dict(kwargs))
        return await orig_upd(*args, **kwargs)

    monkeypatch.setattr(
        reconciler_mod,
        "update_reconcile_job_progress",
        create_autospec(policies_mod.update_reconcile_job_progress, side_effect=_spy_upd),
    )

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
        "job_context": {},
        "claim_token": None,
        "roster_revision": 1,
        "yaml_synced": False,
        "status": "pending",
    }

    res = await reconciler_mod.TierPolicyReconciler().reconcile_job(uid, jid)
    assert res["status"] == "partial"
    # Update 0 (NVDA failure): status='partial', release_claim=False (claim_token preserved!)
    assert observed_updates[0]["status"] == "partial"
    assert observed_updates[0]["release_claim"] is False
    assert observed_updates[0]["claim_token"] is not None
    # Final update: release_claim=True
    assert observed_updates[-1]["release_claim"] is True
    assert infra.jobs[jid]["claim_token"] is None

    # Lease loss mid-job: if claim_token is stolen before progress update, worker stops with lease_lost
    jid2 = str(uuid.uuid4())
    infra.jobs[jid2] = {
        "job_id": jid2,
        "user_id": uid,
        "trigger_action": "set_tier",
        "target_policy_versions": {"core": 1},
        "pending_tickers": ["TSM"],
        "completed_tickers": [],
        "failed_items": [],
        "override_user_pause": False,
        "job_context": {},
        "claim_token": "other-worker-token",
        "roster_revision": 1,
        "yaml_synced": False,
        "status": "running",
    }
    res_lost = await reconciler_mod.TierPolicyReconciler().reconcile_job(
        uid,
        jid2,
        _preclaimed_job={**infra.jobs[jid2], "claim_token": "stale-worker-token"},
    )
    assert res_lost["ok"] is False
    assert res_lost["reason"] == "lease_lost"

    # Batched startup scan: 5 jobs with batch_size=2 drains all 5, and a failed job is NEVER re-claimed in same pass
    infra.jobs.clear()
    infra.fail_tickers = {"NVDA"}
    batch_jids: list[str] = []
    for i in range(5):
        bj = str(uuid.uuid4())
        batch_jids.append(bj)
        infra.jobs[bj] = {
            "job_id": bj,
            "user_id": uid,
            "trigger_action": "set_tier",
            "target_policy_versions": {"core": 1},
            "pending_tickers": ["NVDA" if i == 0 else "TSM"],
            "completed_tickers": [],
            "failed_items": [],
            "override_user_pause": False,
            "job_context": {},
            "claim_token": None,
            "roster_revision": i + 1,
            "yaml_synced": False,
            "status": "pending",
        }

    claim_calls: list[set[str]] = []

    async def _startup_claim(*, limit: int = 100, exclude_job_ids: Any = None, lease_seconds: int = 300, conn: Any = None):
        _ = (lease_seconds, conn)
        excl = set(exclude_job_ids or ())
        claim_calls.append(excl)
        picked = []
        for j in infra.jobs.values():
            if j["job_id"] in excl:
                continue
            if j["status"] in ("pending", "partial") and j.get("claim_token") is None:
                j["status"] = "running"
                j["claim_token"] = str(uuid.uuid4())
                picked.append(dict(j))
                if len(picked) >= limit:
                    break
        return picked

    monkeypatch.setattr(
        reconciler_mod,
        "claim_unfinished_reconcile_jobs_for_startup",
        create_autospec(policies_mod.claim_unfinished_reconcile_jobs_for_startup, side_effect=_startup_claim),
    )

    scan = await reconciler_mod.run_startup_reconcile_scan(batch_size=2)
    assert scan["processed_jobs"] == 5
    # Batch 0 (2), Batch 1 (2), Batch 2 (1), Batch 3 (0 -> stop); failed job batch_jids[0] was never re-claimed!
    assert len(claim_calls) == 4
    assert batch_jids[0] in claim_calls[1]


@pytest.mark.asyncio
async def test_r4_manual_pause_persistence_remove_identity_and_preflight_yaml_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 Decisions 3, 5, 6: single-task pause sets paused_by_user; demote->promote preserves manual pause;
    remove compensation ignores ticker fallback when pre_row is None; preflight YAML guard blocks stale overwrite
    and never marks incomplete jobs yaml_synced."""
    from src.dalton.coverage import service as cov_svc
    from src.server.services.automations import lifecycle as lifecycle_mod

    # 1. Single-task manual pause stamps paused_by_user=True for Dalton tasks only
    dalton_row = {
        "automation_id": "a1",
        "name": "dalton-nvda-watch",
        "status": "active",
        "trigger_type": "cron",
        "metadata": {"tier": "core"},
    }
    pw = lifecycle_mod.pause_update_fields(dalton_row)
    assert pw["metadata"]["paused_by_user"] is True
    locked_kw: dict[str, Any] = {"status": "paused"}
    lifecycle_mod.apply_locked_pause_schedule(dalton_row, locked_kw)
    assert locked_kw["metadata"]["paused_by_user"] is True

    non_dalton_row = {
        "automation_id": "a2",
        "name": "user-custom-alert",
        "status": "active",
        "trigger_type": "cron",
        "metadata": {},
    }
    pw_non = lifecycle_mod.pause_update_fields(non_dalton_row)
    assert "metadata" not in pw_non

    # 2. Demote manually paused task to radar, then promote back to core -> MUST stay paused!
    infra = _FakeWatchlistInfra()
    infra.install(monkeypatch)
    uid = "u1"
    ws_id = "ws-nvda"
    infra.workspaces[(uid, "NVDA")] = ws_id
    infra.workspace_owners[ws_id] = uid
    infra.coverage[(uid, "NVDA")] = {"user_id": uid, "ticker": "NVDA", "tier": "radar", "is_private": False}
    manual_paused = infra.add_automation(
        user_id=uid,
        workspace_id=ws_id,
        name="dalton-nvda-watch",
        status="paused",
        metadata={"paused_by_user": True, "tier": "core"},
    )
    overdue_unstarted_earn = infra.add_automation(
        user_id=uid,
        workspace_id=ws_id,
        name="dalton-nvda-earn-fy26q4-t0",
        trigger_type="once",
        cron_expression=None,
        status="active",
        next_run_at=datetime.now(UTC) - timedelta(minutes=10),  # overdue but never ran (last_run_at=None)
    )
    rec = reconciler_mod.TierPolicyReconciler()
    await rec.reconcile_company(uid, "NVDA")
    # Manual pause MUST NOT have been stamped paused_by_policy=True; overdue unstarted earn WAS paused!
    assert "paused_by_policy" not in infra.automations[manual_paused["automation_id"]]["metadata"]
    assert infra.automations[overdue_unstarted_earn["automation_id"]]["status"] == "paused"

    infra.coverage[(uid, "NVDA")]["tier"] = "core"
    await rec.reconcile_company(uid, "NVDA")
    assert infra.automations[manual_paused["automation_id"]]["status"] == "paused"

    # 3. Remove compensation when pre_row is None NEVER falls back to ticker lookup
    infra.workspaces[(uid, "META")] = "ws-newly-bound-meta"
    infra.workspace_owners["ws-newly-bound-meta"] = uid
    infra.workspace_rows["ws-newly-bound-meta"] = {"workspace_id": "ws-newly-bound-meta", "name": "META", "status": "active"}
    meta_auto = infra.add_automation(user_id=uid, workspace_id="ws-newly-bound-meta", name="dalton-meta-watch", status="active")
    res_no_rem = await rec.reconcile_company(uid, "META", removed_workspace_id=None)
    assert res_no_rem["reason"] == "no_workspace"
    assert infra.automations[meta_auto["automation_id"]]["status"] == "active"

    # 4. Preflight YAML revision guard blocks stale overwrite & never marks incomplete jobs yaml_synced
    sync_state = {"roster_revision": 2, "last_yaml_synced_revision": 2}
    writes_yaml: list[str] = []

    class _FakeBackend:
        async def awrite_text(self, rel_path: str, content: str) -> bool:
            _ = rel_path
            writes_yaml.append(content)
            return True

    monkeypatch.setattr(
        policies_mod,
        "get_user_roster_state",
        create_autospec(policies_mod.get_user_roster_state, side_effect=lambda u, *, conn=None: dict(sync_state)),
    )
    monkeypatch.setattr(
        policies_mod,
        "record_yaml_synced_revision",
        create_autospec(policies_mod.record_yaml_synced_revision, return_value=True),
    )
    ok_stale = await cov_svc.sync_user_coverage_to_home(
        uid, yaml_text="old-v1", backend=_FakeBackend(), roster_revision=1
    )
    assert ok_stale is False
    assert writes_yaml == []

    # mark_yaml_synced_for_user NEVER marks a job with pending_tickers as yaml_synced=True
    j_inc = str(uuid.uuid4())
    j_done = str(uuid.uuid4())
    infra.jobs[j_inc] = {
        "job_id": j_inc,
        "user_id": uid,
        "trigger_action": "set_tier",
        "target_policy_versions": {"core": 1},
        "pending_tickers": ["NVDA"],
        "completed_tickers": [],
        "failed_items": [],
        "override_user_pause": False,
        "job_context": {},
        "roster_revision": 1,
        "yaml_synced": False,
        "status": "pending",
    }
    infra.jobs[j_done] = {
        "job_id": j_done,
        "user_id": uid,
        "trigger_action": "set_tier",
        "target_policy_versions": {"core": 1},
        "pending_tickers": [],
        "completed_tickers": ["NVDA"],
        "failed_items": [],
        "override_user_pause": False,
        "job_context": {},
        "roster_revision": 2,
        "yaml_synced": False,
        "status": "partial",
    }
    marked = await rec.mark_yaml_synced_for_user(uid, covered_job_ids=[j_inc, j_done], max_revision=2)
    assert marked == 1
    assert infra.jobs[j_inc]["yaml_synced"] is False
    assert infra.jobs[j_done]["yaml_synced"] is True
    assert infra.jobs[j_done]["status"] == "completed"


@pytest.mark.asyncio
async def test_r4_earnings_stage_guard_and_calendar_state_live_tier_consistency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """r4 Decision 4: validate_earnings_stage_allowed_for_user blocks radar/disabled stages;
    calendar_state check/mark/diff enforce live tier & allowed_stages."""
    import importlib.util
    import json
    from pathlib import Path
    from types import SimpleNamespace

    from src.dalton.watchlist import pause_guards as pg_mod

    cov_map = {
        ("u1", "NET"): {"user_id": "u1", "ticker": "NET", "tier": "radar", "is_private": False},
        ("u1", "MSFT"): {"user_id": "u1", "ticker": "MSFT", "tier": "watch", "is_private": False},
        ("u1", "NVDA"): {"user_id": "u1", "ticker": "NVDA", "tier": "core", "is_private": False},
    }
    monkeypatch.setattr(
        coverage_db,
        "get_coverage_row",
        create_autospec(
            coverage_db.get_coverage_row,
            side_effect=lambda u, t, *, conn=None: cov_map.get((u, t.upper())),
        ),
    )
    monkeypatch.setattr(
        policies_mod,
        "get_user_tier_policy",
        create_autospec(
            policies_mod.get_user_tier_policy,
            side_effect=lambda u, tier, *, conn=None: policies_mod.get_default_tier_policy(tier),
        ),
    )

    err_radar = await pg_mod.validate_earnings_stage_allowed_for_user("u1", "dalton-net-earn-fy26q4-t0", ticker="NET")
    assert err_radar is not None and err_radar["error"] == "earnings_stage_disabled"

    err_watch_tm3 = await pg_mod.validate_earnings_stage_allowed_for_user("u1", "dalton-msft-earn-fy26q4-tm3", ticker="MSFT")
    assert err_watch_tm3 is not None and err_watch_tm3["error"] == "earnings_stage_disabled"

    ok_watch_t0 = await pg_mod.validate_earnings_stage_allowed_for_user("u1", "dalton-msft-earn-fy26q4-t0", ticker="MSFT")
    assert ok_watch_t0 is None

    ok_core_tm3 = await pg_mod.validate_earnings_stage_allowed_for_user("u1", "dalton-nvda-earn-fy26q4-tm3", ticker="NVDA")
    assert ok_core_tm3 is None

    # calendar_state.py check, mark (filtering explicit disabled stages), and diff (live tier over stale ledger)
    script_path = Path(__file__).resolve().parents[4] / "plugins/dalton/skills/dalton-earnings-calendar/scripts/calendar_state.py"
    spec = importlib.util.spec_from_file_location("calendar_state_r4", script_path)
    assert spec and spec.loader
    cs_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs_mod)

    cov_yaml = tmp_path / "coverage.yaml"
    cov_yaml.write_text(
        "companies:\n"
        "  - ticker: MSFT\n"
        "    name: Microsoft\n"
        "    tier: watch\n"
        "    workspace:\n"
        "      workspace_id: ws-msft\n"
        "      status: active\n"
        "  - ticker: NET\n"
        "    name: Cloudflare\n"
        "    tier: radar\n"
        "    workspace:\n"
        "      workspace_id: ws-net\n"
        "      status: active\n",
        encoding="utf-8",
    )
    ledger_path = tmp_path / "state.json"
    ledger_path.write_text(
        json.dumps(
            {
                "updated": "2026-10-01",
                "entries": {
                    "NET": {
                        "name": "Cloudflare",
                        "tier": "Core",  # stale ledger tier!
                        "workspace_id": "ws-net",
                        "status": "confirmed",
                        "date_status": "confirmed",
                        "quarter": "FY26Q3",
                        "date": "2026-11-05",
                        "automations": {},
                    }
                },
                "reported": [],
            }
        ),
        encoding="utf-8",
    )

    # cmd_check returns allowed_stages matching live tier
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        cs_mod.cmd_check(
            SimpleNamespace(state=str(ledger_path), coverage=str(cov_yaml), ticker="MSFT", date="2026-10-28")
        )
    chk = json.loads(buf.getvalue())
    assert chk["allowed_stages"] == ["t0"]

    # cmd_mark with explicit --tm3 and --t0 on Watch filters out tm3!
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        cs_mod.cmd_mark(
            SimpleNamespace(
                state=str(ledger_path),
                coverage=str(cov_yaml),
                ticker="MSFT",
                name=None,
                tier=None,
                workspace_id=None,
                fiscal_note=None,
                status="confirmed",
                quarter="FY26Q1",
                date="2026-10-28",
                timing="AMC",
                release_time_et="16:05",
                call_time_et="17:30",
                confidence="high",
                source_url="https://ir.microsoft.com",
                source_type="company_ir",
                reason=None,
                automations_json=None,
                tm3="dalton-msft-earn-fy26q1-tm3.json",
                t0="dalton-msft-earn-fy26q1-t0.json",
                tp1="dalton-msft-earn-fy26q1-tp1.json",
                tentative_date=None,
                tentative_sources=None,
                recheck_automation=None,
            )
        )
    marked_out = json.loads(buf2.getvalue())
    assert list(marked_out["entry"]["automations"].keys()) == ["t0"]
    assert set(marked_out["filtered_out_stages"]) == {"tm3", "tp1"}

    # cmd_diff uses live coverage tier (radar -> []) rather than stale ledger tier (Core -> tm3/t0/tp1)
    scan_file = tmp_path / "scan.jsonl"
    scan_file.write_text(
        json.dumps({"ticker": "NET", "status": "confirmed", "confidence": "high", "earnings_date": "2026-11-05"}) + "\n",
        encoding="utf-8",
    )
    buf3 = io.StringIO()
    with redirect_stdout(buf3):
        cs_mod.cmd_diff(
            SimpleNamespace(state=str(ledger_path), coverage=str(cov_yaml), scan=str(scan_file), automations_dir=None)
        )
    diff_out = json.loads(buf3.getvalue())
    assert diff_out["reconciled"] is True
    assert diff_out["missing"] == []


@pytest.mark.asyncio
async def test_r5_outbox_lease_checked_before_side_effects_and_mark_yaml_skips_leased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r5: Stale worker aborts BEFORE executing reconcile_company or Home YAML sync when lease is lost;
    mark_yaml_synced_for_user never marks currently-leased or non-completed jobs."""
    infra = _FakeWatchlistInfra()
    infra.install(monkeypatch)
    infra.workspaces[("u1", "NVDA")] = "ws-nvda"
    infra.workspace_owners["ws-nvda"] = "u1"
    infra.workspace_rows["ws-nvda"] = {"workspace_id": "ws-nvda", "user_id": "u1", "name": "NVDA", "status": "running"}
    infra.coverage[("u1", "NVDA")] = {"user_id": "u1", "ticker": "NVDA", "tier": "core", "is_private": False, "extra": {"workspace": {"workspace_id": "ws-nvda"}}}

    jid = str(uuid.uuid4())
    job: dict[str, Any] = {
        "job_id": jid,
        "user_id": "u1",
        "trigger_action": "set_tier",
        "target_policy_versions": {"core": 1},
        "pending_tickers": ["NVDA"],
        "completed_tickers": [],
        "failed_items": [],
        "override_user_pause": False,
        "job_context": {},
        "roster_revision": 1,
        "yaml_synced": False,
        "status": "running",
        "claim_token": "tok-worker-b",  # Another worker now owns the lease
    }
    infra.jobs[jid] = job

    reconcile_calls: list[str] = []

    async def _spy_company(self: Any, uid: str, sym: str, **kw: Any) -> dict[str, Any]:
        _ = (self, uid, kw)
        reconcile_calls.append(sym)
        return {"ticker": sym, "ok": True, "actions": []}

    monkeypatch.setattr(
        reconciler_mod.TierPolicyReconciler,
        "reconcile_company",
        create_autospec(reconciler_mod.TierPolicyReconciler.reconcile_company, side_effect=_spy_company),
    )

    # Old Worker A holds stale token 'tok-worker-a' -> MUST abort before calling reconcile_company or YAML sync
    stale_preclaimed = {**job, "claim_token": "tok-worker-a"}
    res_stale = await reconciler_mod.TierPolicyReconciler().reconcile_job(
        "u1", jid, sync_home=True, _preclaimed_job=stale_preclaimed
    )
    assert res_stale["reason"] == "lease_lost"
    assert reconcile_calls == []
    assert infra.home_sync_calls == 0

    # mark_yaml_synced_for_user skips leased running job even when pending_tickers is empty
    job["pending_tickers"] = []
    job["failed_items"] = []
    marked = await reconciler_mod.TierPolicyReconciler().mark_yaml_synced_for_user("u1")
    assert marked == 0
    assert job["yaml_synced"] is False


@pytest.mark.asyncio
async def test_r5_tracking_locked_reconciler_composed_and_manual_pause_once_clears_next_run_at() -> None:
    """r5: Nested automation_history_context composes parent & inner locked_row_reconcilers;
    manual pause of Dalton once task clears next_run_at while preserving once_schedule_at for future resume."""
    from src.dalton.services.company_automations import (
        automation_history_context,
        get_current_history_context,
    )
    from src.dalton.watchlist.pause_guards import build_locked_resume_reconciler
    from src.server.services.automations.lifecycle import (
        apply_locked_pause_schedule,
        pause_update_fields,
        resume_update_fields,
    )

    # 1. Nested automation_history_context composes inner + outer reconcilers (outer keeps final authority)
    order: list[str] = []

    async def _inner(locked_row: Any, eff: dict[str, Any], conn: Any) -> None:
        _ = (locked_row, conn)
        order.append("inner")
        eff["status"] = "active"
        eff["next_run_at"] = datetime.now(UTC) + timedelta(hours=1)

    outer = build_locked_resume_reconciler(override_user_pause=False, override_pause_versions=None)
    with automation_history_context(actor_user_id="u1", action="resume", locked_row_reconciler=outer):
        with automation_history_context(actor_user_id="u1", action="resume", locked_row_reconciler=_inner):
            ctx = get_current_history_context()
            assert ctx is not None and ctx.locked_row_reconciler is not None
            eff: dict[str, Any] = {"status": "active"}
            locked_paused = {
                "automation_id": "a-1",
                "status": "paused",
                "config_version": 3,
                "metadata": {"paused_by_user": True},
            }
            await ctx.locked_row_reconciler(locked_paused, eff, None)
            assert order == ["inner"]
            assert eff["status"] == "paused"
            assert eff["next_run_at"] is None
            assert eff["metadata"]["paused_by_user"] is True

    # 2. Manual pause of Dalton once task clears next_run_at and saves once_schedule_at; resume restores it
    future_ts = datetime.now(UTC) + timedelta(days=5)
    once_row = {
        "automation_id": "a-once-1",
        "name": "dalton-nvda-earn-2026q4-t0",
        "trigger_type": "once",
        "status": "active",
        "timezone": "UTC",
        "next_run_at": future_ts,
        "metadata": {"ticker": "NVDA", "dalton_managed": True},
    }
    p_fields = pause_update_fields(once_row)
    assert p_fields["status"] == "paused"
    assert p_fields["next_run_at"] is None
    assert p_fields["metadata"]["paused_by_user"] is True
    assert p_fields["metadata"]["once_schedule_at"] == future_ts.isoformat()

    locked_eff: dict[str, Any] = {"status": "paused", "next_run_at": future_ts}
    apply_locked_pause_schedule(once_row, locked_eff)
    assert locked_eff["next_run_at"] is None
    assert locked_eff["metadata"]["once_schedule_at"] == future_ts.isoformat()

    paused_row = {**once_row, **p_fields}
    r_fields = resume_update_fields(paused_row)
    assert r_fields["status"] == "active"
    assert r_fields["next_run_at"] == future_ts


@pytest.mark.asyncio
async def test_r5_earnings_guard_refuses_removed_ticker_and_catches_claimed_pending_once_and_calendar_unified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """r5: Removed ticker and DB errors are refused (fail-closed); scheduler-claimed pending once is caught;
    calendar_state check/mark/diff share the exact same live-coverage stage set."""
    import importlib.util
    import io
    import json
    from contextlib import redirect_stdout
    from pathlib import Path
    from types import SimpleNamespace
    from src.dalton.watchlist.pause_guards import validate_earnings_stage_allowed_for_user

    orig_get_cov = coverage_db.get_coverage_row
    orig_list_exec = reconciler_mod.exec_db.list_executions

    # 1. Removed ticker (cov_row is None) -> REFUSED
    monkeypatch.setattr(
        coverage_db,
        "get_coverage_row",
        create_autospec(orig_get_cov, return_value=None),
    )
    err_removed = await validate_earnings_stage_allowed_for_user(
        "u1", "dalton-nvda-earn-2026q4-t0", ticker="NVDA"
    )
    assert err_removed is not None and err_removed["error"] == "earnings_stage_disabled"

    # 2. DB error containing 'does not exist' -> REFUSED (fail-closed, never allowed)
    monkeypatch.setattr(
        coverage_db,
        "get_coverage_row",
        create_autospec(
            orig_get_cov,
            side_effect=RuntimeError('relation "dalton_coverage" does not exist'),
        ),
    )
    err_db = await validate_earnings_stage_allowed_for_user(
        "u1", "dalton-nvda-earn-2026q4-t0", ticker="NVDA"
    )
    assert err_db is not None and err_db["error"] == "earnings_stage_check_unavailable"

    # 3. Scheduler-claimed pending once (last_run_at set, pending execution) is caught by is_unstarted_earn_once
    claimed_once_row = {
        "automation_id": "a-claimed-once",
        "user_id": "u1",
        "name": "dalton-nvda-earn-2026q4-t0",
        "trigger_type": "once",
        "status": "active",
        "last_run_at": datetime.now(UTC),
        "run_count": 0,
    }
    monkeypatch.setattr(
        reconciler_mod.exec_db,
        "list_executions",
        create_autospec(
            orig_list_exec,
            return_value=([{"status": "pending"}], False),
        ),
    )
    assert await reconciler_mod.is_unstarted_earn_once(claimed_once_row) is True

    monkeypatch.setattr(
        reconciler_mod.exec_db,
        "list_executions",
        create_autospec(
            orig_list_exec,
            return_value=([{"status": "completed"}], False),
        ),
    )
    assert await reconciler_mod.is_unstarted_earn_once(claimed_once_row) is False

    # 4. calendar_state.py check / mark (--tier core) / diff all enforce live coverage (radar & removed -> [])
    script_path = (
        Path(__file__).resolve().parents[4]
        / "plugins/dalton/skills/dalton-earnings-calendar/scripts/calendar_state.py"
    )
    spec = importlib.util.spec_from_file_location("calendar_state_r5_test", script_path)
    assert spec and spec.loader
    cs_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs_mod)

    cov_yaml = tmp_path / "coverage.yaml"
    cov_yaml.write_text(
        "companies:\n"
        "  - ticker: NET\n"
        "    name: Cloudflare\n"
        "    tier: radar\n"
        "    workspace:\n"
        "      workspace_id: ws-net\n",
        encoding="utf-8",
    )
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(
        json.dumps(
            {
                "updated": "2026-10-01",
                "entries": {
                    "GONE": {
                        "ticker": "GONE",
                        "tier": "Core",
                        "workspace_id": "ws-gone",
                        "status": "confirmed",
                        "date_status": "confirmed",
                        "date": "2026-11-05",
                        "quarter": "FY26Q3",
                        "automations": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    # Radar company with explicit --tier core --t0 MUST still filter out t0!
    buf_mark = io.StringIO()
    with redirect_stdout(buf_mark):
        cs_mod.cmd_mark(
            SimpleNamespace(
                state=str(ledger_path),
                coverage=str(cov_yaml),
                ticker="NET",
                name="Cloudflare",
                tier="core",
                workspace_id="ws-net",
                fiscal_note=None,
                status="confirmed",
                date="2026-11-05",
                date_status="confirmed",
                quarter="FY26Q3",
                timing="AMC",
                release_time_et=None,
                call_time_et=None,
                confidence="high",
                source_url="https://ir.cloudflare.com",
                source_type="company_ir",
                reason=None,
                automations_json=None,
                tm3=None,
                t0="dalton-net-earn-fy26q3-t0.json",
                tp1=None,
                tentative_date=None,
                tentative_sources=None,
                recheck_automation=None,
            )
        )
    mark_res = json.loads(buf_mark.getvalue())
    assert mark_res["entry"]["automations"] == {}
    assert mark_res["filtered_out_stages"] == ["t0"]

    # Removed company GONE -> check returns tier=None, allowed_stages=[]
    buf_chk = io.StringIO()
    with redirect_stdout(buf_chk):
        cs_mod.cmd_check(
            SimpleNamespace(state=str(ledger_path), coverage=str(cov_yaml), ticker="GONE", date="2026-11-05")
        )
    chk_res = json.loads(buf_chk.getvalue())
    assert chk_res["tier"] is None
    assert chk_res["allowed_stages"] == []


@pytest.mark.asyncio
async def test_r6_generic_resume_refuses_removed_or_downgraded_earnings_task_and_keeps_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r6 Item 1: Generic lifecycle.resume_automation shares the locked earnings-stage guard
    with Dalton scheduling: resuming an earnings task after ticker removal or tier downgrade
    is refused and never reschedules next_run_at."""
    from src.dalton import settings as dalton_settings_mod
    from src.dalton.services import company_automations as comp_auto_mod
    from src.server.database import automation as auto_db_mod
    from src.server.services.automations import lifecycle as lifecycle_mod

    future_ts = datetime.now(UTC) + timedelta(days=3)
    uid = "u-resume-r6"
    auto_id = str(uuid.uuid4())
    stored_row: dict[str, Any] = {
        "automation_id": auto_id,
        "user_id": uid,
        "name": "dalton-nvda-earn-fy26q4-tm3",
        "trigger_type": "once",
        "status": "paused",
        "next_run_at": None,
        "workspace_id": "ws-nvda",
        "timezone": "America/New_York",
        "config_version": 2,
        "metadata": {
            "ticker": "NVDA",
            "dalton_managed": True,
            "paused_by_policy": True,
            "once_schedule_at": future_ts.isoformat(),
        },
    }

    updates_written: list[dict[str, Any]] = []

    async def _fake_update_automation(
        aid: str,
        user_id: str,
        *,
        conn: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        _ = (aid, user_id, conn)
        effective = dict(kwargs)
        hist_ctx = comp_auto_mod.get_current_history_context()
        if hist_ctx is not None and hist_ctx.locked_row_reconciler is not None:
            await hist_ctx.locked_row_reconciler(dict(stored_row), effective, None)
        stored_row.update(effective)
        updates_written.append(effective)
        return dict(stored_row)

    monkeypatch.setattr(dalton_settings_mod, "is_coverage_units_enabled", lambda: False)
    monkeypatch.setattr(
        auto_db_mod,
        "get_automation",
        create_autospec(auto_db_mod.get_automation, side_effect=lambda *a, **kw: dict(stored_row)),
    )
    monkeypatch.setattr(
        auto_db_mod,
        "update_automation",
        create_autospec(auto_db_mod.update_automation, side_effect=_fake_update_automation),
    )

    orig_get_cov = coverage_db.get_coverage_row

    # Case A: Ticker removed from watchlist -> generic resume MUST refuse and keep paused + next_run_at=None
    monkeypatch.setattr(
        coverage_db,
        "get_coverage_row",
        create_autospec(orig_get_cov, return_value=None),
    )
    with pytest.raises(lifecycle_mod.AutomationRefusal) as exc_info:
        await lifecycle_mod.resume_automation(auto_id, uid)
    assert getattr(exc_info.value, "schedule_error_code", None) == "earnings_stage_disabled"
    assert updates_written == []
    assert stored_row["status"] == "paused"
    assert stored_row["next_run_at"] is None

    # Case B: Ticker downgraded to watch (only t0 allowed) -> tm3 refused, t0 allowed
    monkeypatch.setattr(
        coverage_db,
        "get_coverage_row",
        create_autospec(
            orig_get_cov,
            return_value={"user_id": uid, "ticker": "NVDA", "tier": "watch", "is_private": False},
        ),
    )
    monkeypatch.setattr(
        policies_mod,
        "get_user_tier_policy",
        create_autospec(
            policies_mod.get_user_tier_policy,
            return_value=policies_mod.DEFAULT_TIER_POLICIES["watch"],
        ),
    )
    with pytest.raises(lifecycle_mod.AutomationRefusal):
        await lifecycle_mod.resume_automation(auto_id, uid)
    assert updates_written == []
    assert stored_row["status"] == "paused"
    assert stored_row["next_run_at"] is None

    # Allowed stage (t0 on watch tier) resumes and restores next_run_at
    stored_row["name"] = "dalton-nvda-earn-fy26q4-t0"
    resumed = await lifecycle_mod.resume_automation(auto_id, uid)
    assert resumed is not None
    assert resumed["status"] == "active"
    assert resumed["next_run_at"] == future_ts


def test_r6_calendar_state_mark_default_branch_and_diff_live_workspace(
    tmp_path: Any,
) -> None:
    """r6 Item 2: calendar_state mark default branch derives automations solely from live allowed_stages
    (empty -> {}), and diff includes workspaces newly bound in live coverage.yaml."""
    import importlib.util
    import io
    import json
    from contextlib import redirect_stdout
    from pathlib import Path
    from types import SimpleNamespace

    script_path = (
        Path(__file__).resolve().parents[4]
        / "plugins/dalton/skills/dalton-earnings-calendar/scripts/calendar_state.py"
    )
    spec = importlib.util.spec_from_file_location("calendar_state_r6_test", script_path)
    assert spec and spec.loader
    cs_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs_mod)

    cov_yaml = tmp_path / "coverage.yaml"
    cov_yaml.write_text(
        "companies:\n"
        "  - ticker: SHARED\n"
        "    name: Shared Ticker Only\n"
        "    workspace:\n"
        "      workspace_id: ws-shared\n"
        "  - ticker: OPENAI\n"
        "    name: OpenAI\n"
        "    tier: core\n"
        "    is_private: true\n"
        "    workspace:\n"
        "      workspace_id: ws-openai\n"
        "  - ticker: NEWWS\n"
        "    name: Newly Bound Co\n"
        "    tier: core\n"
        "    workspace:\n"
        "      workspace_id: ws-newly-bound\n",
        encoding="utf-8",
    )
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text(
        json.dumps(
            {
                "updated": "2026-10-01",
                "entries": {
                    "GONE": {
                        "ticker": "GONE",
                        "tier": "Core",
                        "workspace_id": "ws-gone",
                        "status": "confirmed",
                        "date_status": "confirmed",
                        "date": "2026-11-05",
                        "quarter": "FY26Q3",
                        "automations": {"tm3": "old-tm3.json", "t0": "old-t0.json", "tp1": "old-tp1.json"},
                    },
                    "NEWWS": {
                        "ticker": "NEWWS",
                        "tier": "Core",
                        "workspace_id": None,
                        "status": "confirmed",
                        "date_status": "confirmed",
                        "date": "2026-11-10",
                        "quarter": "FY26Q3",
                        "automations": {},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    def _mark_default(ticker: str, tier_arg: str | None = "core", ws_arg: str | None = None) -> dict[str, Any]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            cs_mod.cmd_mark(
                SimpleNamespace(
                    state=str(ledger_path),
                    coverage=str(cov_yaml),
                    ticker=ticker,
                    name=None,
                    tier=tier_arg,
                    workspace_id=ws_arg,
                    fiscal_note=None,
                    status="confirmed",
                    date="2026-11-05",
                    date_status="confirmed",
                    quarter="FY26Q3",
                    timing="AMC",
                    release_time_et=None,
                    call_time_et=None,
                    confidence="high",
                    source_url="https://ir.example.com",
                    source_type="company_ir",
                    reason=None,
                    automations_json=None,
                    tm3=None,
                    t0=None,
                    tp1=None,
                    tentative_date=None,
                    tentative_sources=None,
                    recheck_automation=None,
                )
            )
        return json.loads(buf.getvalue())

    # Removed ticker, shared Home ticker-only, and private company ALL write empty automations in default branch
    for sym in ("GONE", "SHARED", "OPENAI"):
        res = _mark_default(sym, tier_arg="core")
        assert res["entry"]["automations"] == {}, f"{sym} should write empty automations"

    # diff catches NEWWS whose ledger workspace_id is None but live coverage.yaml newly bound ws-newly-bound
    scan_path = tmp_path / "scan.jsonl"
    scan_path.write_text(
        json.dumps({"ticker": "NEWWS", "status": "confirmed", "confidence": "high", "earnings_date": "2026-11-10"})
        + "\n",
        encoding="utf-8",
    )
    buf_diff = io.StringIO()
    with redirect_stdout(buf_diff):
        rc = cs_mod.cmd_diff(
            SimpleNamespace(
                state=str(ledger_path),
                coverage=str(cov_yaml),
                scan=str(scan_path),
                automations_dir=None,
            )
        )
    diff_out = json.loads(buf_diff.getvalue())
    assert rc == 2
    assert diff_out["reconciled"] is False
    assert any(
        m["ticker"] == "NEWWS"
        and m["reason"] == "missing_automation_entries_in_ledger"
        and m["missing_automations"] == ["tm3", "t0", "tp1"]
        for m in diff_out["missing"]
    )


@pytest.mark.asyncio
async def test_r6_list_executions_signature_bind_and_fail_safe_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r6 Item 3: list_executions call site matches real signature (verified via inspect.signature().bind),
    unpacks the (rows, has_more) tuple, passes held conn, and fails safe (returns True -> pause) on error."""
    import inspect

    orig_list_exec = reconciler_mod.exec_db.list_executions
    sig = inspect.signature(orig_list_exec)
    bound_with_conn = sig.bind("u-sig", automation_id="auto-123", limit=10, conn=object())
    assert bound_with_conn.arguments["user_id"] == "u-sig"
    assert bound_with_conn.arguments["automation_id"] == "auto-123"
    assert bound_with_conn.arguments["limit"] == 10

    bound_no_conn = sig.bind("u-sig", automation_id="auto-123", limit=10)
    assert bound_no_conn.arguments["user_id"] == "u-sig"

    row = {
        "automation_id": "auto-123",
        "user_id": "u-sig",
        "name": "dalton-nvda-earn-fy26q4-t0",
        "trigger_type": "once",
        "status": "active",
        "last_run_at": datetime.now(UTC),
        "run_count": 0,
    }

    # Cursor failure falls back to list_executions(user_id, automation_id=..., limit=10, conn=held_conn)
    class _BrokenCursorConn:
        def cursor(self):
            raise RuntimeError("cursor boom")

    held_conn = _BrokenCursorConn()
    spy_ok = create_autospec(orig_list_exec, return_value=([{"status": "waiting"}], True))
    monkeypatch.setattr(reconciler_mod.exec_db, "list_executions", spy_ok)
    assert await reconciler_mod.is_unstarted_earn_once(row, user_id="u-sig", conn=held_conn) is True
    spy_ok.assert_awaited_once_with("u-sig", automation_id="auto-123", limit=10, conn=held_conn)

    # Query failure in list_executions fails safe -> returns True (should pause)
    spy_err = create_autospec(orig_list_exec, side_effect=RuntimeError("db down"))
    monkeypatch.setattr(reconciler_mod.exec_db, "list_executions", spy_err)
    assert await reconciler_mod.is_unstarted_earn_once(row, user_id="u-sig", conn=held_conn) is True


@pytest.mark.asyncio
async def test_r6_removed_workspace_fallback_passes_conn_and_fails_safe_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r6 Item 4: _resolve_workspace_ticker receives current conn; query errors during removed-row
    workspace identity fallback treat the workspace as still bound and pause tasks instead of skipping."""
    from src.dalton.autonomy import tracking as tracking_mod

    infra = _FakeWatchlistInfra()
    infra.install(monkeypatch)
    uid = "u-fb-r6"
    ws_id = "ws-custom-name"
    sentinel_conn = object()

    @asynccontextmanager
    async def _lock_with_sentinel(user_id: str, *, conn: Any = None, prelock_ws: str | None = None):
        _ = (user_id, conn, prelock_ws)
        yield sentinel_conn

    monkeypatch.setattr(reconciler_mod, "_user_automations_lock_for_write", _lock_with_sentinel)
    infra.workspace_owners[ws_id] = uid
    infra.workspace_rows[ws_id] = {
        "workspace_id": ws_id,
        "name": "Custom AI Research Workspace",
        "status": "active",
    }
    auto = infra.add_automation(
        user_id=uid,
        workspace_id=ws_id,
        name="dalton-nvda-watch",
        status="active",
    )

    # Fallback lookup raises a DB error -> MUST pass conn and fail safe (still bound -> PAUSE, not skip)
    resolve_err_spy = create_autospec(
        tracking_mod._resolve_workspace_ticker,
        side_effect=RuntimeError("knowledge table unavailable"),
    )
    monkeypatch.setattr(tracking_mod, "_resolve_workspace_ticker", resolve_err_spy)

    rec = reconciler_mod.TierPolicyReconciler()
    res = await rec.reconcile_company(uid, "NVDA", removed_workspace_id=ws_id)
    assert res.get("skipped") is not True
    assert infra.automations[auto["automation_id"]]["status"] == "paused"
    assert resolve_err_spy.await_count == 1
    assert resolve_err_spy.await_args.kwargs.get("conn") is sentinel_conn


@pytest.mark.asyncio
async def test_r6_reconciler_fences_lease_inside_automation_and_yaml_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r6 Item 5: When lease expires AFTER pre-lock renew (while waiting on automation lock or YAML lock),
    in-lock lease verification aborts with reason='lease_lost' and performs zero DB/YAML writes."""
    from src.dalton.coverage import service as cov_svc

    infra = _FakeWatchlistInfra()
    infra.install(monkeypatch)
    uid = "u-fence-r6"
    ws_id = "ws-nvda-fence"
    infra.workspaces[(uid, "NVDA")] = ws_id
    infra.workspace_owners[ws_id] = uid
    infra.coverage[(uid, "NVDA")] = {"user_id": uid, "ticker": "NVDA", "tier": "radar", "is_private": False}
    auto = infra.add_automation(
        user_id=uid,
        workspace_id=ws_id,
        name="dalton-nvda-watch",
        status="active",
    )

    jid = str(uuid.uuid4())
    preclaimed = {
        "job_id": jid,
        "user_id": uid,
        "trigger_action": "set_tier",
        "target_policy_versions": {"radar": 1},
        "pending_tickers": ["NVDA"],
        "completed_tickers": [],
        "failed_items": [],
        "override_user_pause": False,
        "job_context": {},
        "claim_token": "tok-old",
        "roster_revision": 1,
        "yaml_synced": False,
        "status": "running",
    }

    # 1st call (pre-lock renew) returns True; 2nd call (inside automation lock) returns False!
    renew_calls = 0

    async def _renew_lose_in_lock(
        user_id: str,
        job_id: str,
        *,
        claim_token: str,
        lease_seconds: int = 300,
        conn: Any = None,
    ) -> bool:
        nonlocal renew_calls
        _ = (user_id, job_id, claim_token, lease_seconds, conn)
        renew_calls += 1
        return renew_calls == 1

    monkeypatch.setattr(
        reconciler_mod,
        "renew_reconcile_job_lease",
        create_autospec(policies_mod.renew_reconcile_job_lease, side_effect=_renew_lose_in_lock),
    )

    rec = reconciler_mod.TierPolicyReconciler()
    res = await rec.reconcile_job(uid, jid, sync_home=False, _preclaimed_job=preclaimed)
    assert res["reason"] == "lease_lost"
    assert renew_calls == 2
    assert infra.automations[auto["automation_id"]]["status"] == "active"

    # Part 2: YAML sync lease check fails right before actual write -> zero backend writes
    writes: list[str] = []

    class _SpyBackend:
        async def awrite_text(self, rel_path: str, content: str) -> bool:
            _ = rel_path
            writes.append(content)
            return True

    yaml_checks = 0

    async def _lose_before_write(lock_conn: Any = None) -> bool:
        nonlocal yaml_checks
        _ = lock_conn
        yaml_checks += 1
        return yaml_checks == 1  # passes after YAML lock, fails right before actual write

    monkeypatch.setattr(
        policies_mod,
        "get_user_roster_state",
        create_autospec(
            policies_mod.get_user_roster_state,
            return_value={"roster_revision": 2, "last_yaml_synced_revision": 1},
        ),
    )
    ok_yaml = await cov_svc.sync_user_coverage_to_home(
        uid,
        yaml_text="companies: []\n",
        backend=_SpyBackend(),
        roster_revision=2,
        lease_check=_lose_before_write,
    )
    assert ok_yaml is False
    assert yaml_checks == 2
    assert writes == []


@pytest.mark.asyncio
async def test_r6_home_yaml_sync_reuses_lock_conn_with_pool_size_one_and_discards_on_unlock_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r6 Item 6: sync_user_coverage_to_home succeeds with a strict 1-slot pool (reuses the lock
    connection for roster read, render, and revision record) and closes/discards the connection
    when pg_advisory_unlock fails."""
    from src.dalton.coverage import service as cov_svc

    uid = "u-pool1-r6"
    pool_in_use = 0
    unlock_should_fail = False
    created_conns: list[Any] = []

    class _FakeCursor:
        def __init__(self, conn: Any) -> None:
            self._conn = conn
            self._last_sql = ""

        async def __aenter__(self) -> "_FakeCursor":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def execute(self, sql: str, params: Any = None) -> None:
            _ = params
            self._last_sql = sql
            if "pg_advisory_unlock" in sql and unlock_should_fail:
                raise RuntimeError("unlock network error")

        async def fetchone(self) -> Any:
            if "pg_advisory_unlock" in self._last_sql:
                return (True,)
            return None

    class _FakeConn:
        def __init__(self) -> None:
            self.closed = False

        def cursor(self, **_kw: Any) -> _FakeCursor:
            return _FakeCursor(self)

        async def close(self) -> None:
            self.closed = True

    @asynccontextmanager
    async def _one_slot_db_conn(existing_conn: Any = None):
        nonlocal pool_in_use
        if existing_conn is not None:
            yield existing_conn
            return
        if pool_in_use >= 1:
            raise RuntimeError("PoolTimeout: pool max_size=1 exhausted by nested checkout!")
        pool_in_use += 1
        conn = _FakeConn()
        created_conns.append(conn)
        try:
            yield conn
        finally:
            pool_in_use -= 1

    seen_conns: list[Any] = []

    async def _fake_get_state(user_id: str, *, conn: Any = None) -> dict[str, int]:
        _ = user_id
        async with _one_slot_db_conn(conn) as c:
            seen_conns.append(c)
        return {"roster_revision": 3, "last_yaml_synced_revision": 2}

    async def _fake_render(user_id: str, *, conn: Any = None) -> str:
        _ = user_id
        async with _one_slot_db_conn(conn) as c:
            seen_conns.append(c)
        return "companies: [{ticker: NVDA}]\n"

    async def _fake_record(user_id: str, revision: int, *, conn: Any = None) -> bool:
        _ = (user_id, revision)
        async with _one_slot_db_conn(conn) as c:
            seen_conns.append(c)
        return True

    class _CaptureBackend:
        def __init__(self) -> None:
            self.writes: list[str] = []

        async def awrite_text(self, rel_path: str, content: str) -> bool:
            _ = rel_path
            self.writes.append(content)
            return True

    monkeypatch.setattr(coverage_db, "get_db_connection", _one_slot_db_conn)
    monkeypatch.setattr(
        policies_mod,
        "get_user_roster_state",
        create_autospec(policies_mod.get_user_roster_state, side_effect=_fake_get_state),
    )
    monkeypatch.setattr(
        cov_svc,
        "render_coverage_yaml",
        create_autospec(cov_svc.render_coverage_yaml, side_effect=_fake_render),
    )
    monkeypatch.setattr(
        policies_mod,
        "record_yaml_synced_revision",
        create_autospec(policies_mod.record_yaml_synced_revision, side_effect=_fake_record),
    )

    backend = _CaptureBackend()
    ok = await cov_svc.sync_user_coverage_to_home(uid, backend=backend)
    assert ok is True
    assert len(backend.writes) == 1
    assert len(created_conns) == 1
    assert created_conns[0].closed is False
    # Every DB call inside the lock reused the exact same lock connection
    assert len(seen_conns) == 4
    assert all(c is created_conns[0] for c in seen_conns)

    # When pg_advisory_unlock fails, the connection MUST be closed/discarded (not returned locked to pool)
    unlock_should_fail = True
    ok2 = await cov_svc.sync_user_coverage_to_home(uid, backend=backend)
    assert ok2 is True
    assert len(created_conns) == 2
    assert created_conns[1].closed is True
