"""Unit tests for server-side confirmation snapshots, StandingApprovalMiddleware, checkpoint_handler, and REST routes."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.types import Command

from src.dalton.app import dalton_router
from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.coverage import models as cov_models
from src.dalton.coverage import service as cov_svc
from src.dalton.database import coverage as coverage_db
from src.dalton.tools import coverage_tools as cov_tools_mod
from src.dalton.watchlist import policies as policies_mod
from src.dalton.watchlist import reconciler as reconciler_mod
from src.dalton.watchlist import service as service_mod
from src.server.handlers import checkpoint_handler as cp_handler
from src.server.models.chat import ChatRequest, HITLDecision, HITLResponse
from src.server.utils.api import get_current_user_id
from src.tools.secretary import _commands as sec_commands
from src.tools.secretary.approvals import StandingApprovalMiddleware

USER_ALICE = "user-alice-wl"


def _unpack_command_json(cmd: Command) -> tuple[dict[str, Any], Any]:
    msg = cmd.update["messages"][0]
    return json.loads(msg.content), msg


def _wire_in_memory_watchlist(
    monkeypatch: pytest.MonkeyPatch,
    *,
    initial_rows: list[dict[str, Any]] | None = None,
    workspaces: dict[str, str] | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "rows": {
            (r["user_id"], r["ticker"]): dict(r)
            for r in (initial_rows or [])
        },
        "groups": {},
        "jobs": {},
        "writes": 0,
        "reconciled_jobs": [],
        "workspaces": dict(workspaces or {}),
    }

    @asynccontextmanager
    async def _fake_db_conn():
        class _Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class _Conn:
            def transaction(self):
                return _Tx()

        yield _Conn()

    async def _noop_lock(conn: Any, uid: str) -> None:
        _ = (conn, uid)

    async def _list_rows(uid: str, *, conn: Any = None) -> list[dict[str, Any]]:
        _ = conn
        return [dict(v) for (u, _), v in state["rows"].items() if u == uid]

    async def _get_row(uid: str, ticker: str, *, conn: Any = None) -> dict[str, Any] | None:
        _ = conn
        r = state["rows"].get((uid, ticker.upper()))
        return dict(r) if r else None

    async def _count_rows(uid: str, *, conn: Any = None) -> int:
        _ = conn
        return sum(1 for (u, _) in state["rows"] if u == uid)

    async def _upsert_rows(uid: str, rows: list[dict[str, Any]], *, expected_unique_count: int | None = None, conn: Any = None):
        _ = (expected_unique_count, conn)
        state["writes"] += 1
        out = []
        for r in rows:
            stored = dict(r)
            state["rows"][(uid, r["ticker"].upper())] = stored
            out.append(dict(stored))
        return len(state["rows"]), out

    async def _update_tier(uid: str, tickers: list[str], tier: str, *, conn: Any = None):
        _ = conn
        state["writes"] += 1
        out = []
        for t in tickers:
            row = state["rows"].get((uid, t.upper()))
            if row:
                row["tier"] = tier
                out.append(dict(row))
        return out

    async def _delete_tickers(uid: str, tickers: list[str], *, conn: Any = None):
        _ = conn
        state["writes"] += 1
        removed = []
        for t in tickers:
            if (uid, t.upper()) in state["rows"]:
                del state["rows"][(uid, t.upper())]
                removed.append(t.upper())
        return removed

    async def _list_groups(uid: str, *, conn: Any = None):
        _ = conn
        return [dict(g) for g in state["groups"].values() if g["user_id"] == uid]

    async def _get_group(uid: str, gid: str, *, conn: Any = None):
        _ = conn
        g = state["groups"].get(gid)
        return dict(g) if g and g["user_id"] == uid else None

    async def _insert_job(uid: str, *, trigger_action: str, target_policy_versions: Any, pending_tickers: Any, override_user_pause: bool = False, job_id: str | None = None, conn: Any = None):
        _ = conn
        jid = job_id or str(uuid.uuid4())
        job = {
            "job_id": jid,
            "user_id": uid,
            "trigger_action": trigger_action,
            "target_policy_versions": dict(target_policy_versions),
            "pending_tickers": list(pending_tickers),
            "completed_tickers": [],
            "failed_items": [],
            "override_user_pause": override_user_pause,
            "yaml_synced": False,
            "status": "pending",
        }
        state["jobs"][jid] = job
        return dict(job)

    async def _fake_reconcile_job(self: Any, uid: str, jid: str, *, sync_home: bool = True):
        _ = (self, uid, sync_home)
        state["reconciled_jobs"].append(jid)
        job = state["jobs"][jid]
        job["completed_tickers"] = list(job["pending_tickers"])
        job["pending_tickers"] = []
        job["yaml_synced"] = True
        job["status"] = "completed"
        return {
            "ok": True,
            "job_id": jid,
            "status": "completed",
            "yaml_synced": True,
            "failed_items": [],
        }

    async def _fake_validate(tickers: Any):
        valid = [
            {"ticker": str(t).strip().upper(), "name": f"{str(t).strip().upper()} Inc", "market": "US"}
            for t in tickers
        ]
        return valid, []

    monkeypatch.setattr(coverage_db, "get_db_connection", _fake_db_conn)
    monkeypatch.setattr(
        coverage_db,
        "acquire_coverage_advisory_lock",
        create_autospec(coverage_db.acquire_coverage_advisory_lock, side_effect=_noop_lock),
    )
    monkeypatch.setattr(
        coverage_db,
        "list_coverage_rows",
        create_autospec(coverage_db.list_coverage_rows, side_effect=_list_rows),
    )
    monkeypatch.setattr(
        coverage_db,
        "get_coverage_row",
        create_autospec(coverage_db.get_coverage_row, side_effect=_get_row),
    )
    monkeypatch.setattr(
        coverage_db,
        "count_coverage_rows",
        create_autospec(coverage_db.count_coverage_rows, side_effect=_count_rows),
    )
    monkeypatch.setattr(
        coverage_db,
        "upsert_coverage_rows",
        create_autospec(coverage_db.upsert_coverage_rows, side_effect=_upsert_rows),
    )
    monkeypatch.setattr(
        coverage_db,
        "update_coverage_tier",
        create_autospec(coverage_db.update_coverage_tier, side_effect=_update_tier),
    )
    monkeypatch.setattr(
        coverage_db,
        "delete_coverage_tickers",
        create_autospec(coverage_db.delete_coverage_tickers, side_effect=_delete_tickers),
    )
    monkeypatch.setattr(
        coverage_db,
        "list_coverage_groups",
        create_autospec(coverage_db.list_coverage_groups, side_effect=_list_groups),
    )
    monkeypatch.setattr(
        coverage_db,
        "get_coverage_group",
        create_autospec(coverage_db.get_coverage_group, side_effect=_get_group),
    )
    monkeypatch.setattr(
        service_mod,
        "validate_and_enrich_tickers",
        create_autospec(cov_svc.validate_and_enrich_tickers, side_effect=_fake_validate),
    )
    monkeypatch.setattr(
        service_mod,
        "_user_workspace_tickers",
        create_autospec(cov_svc._user_workspace_tickers, return_value=state["workspaces"]),
    )
    monkeypatch.setattr(
        service_mod,
        "resolve_company_workspace_id",
        create_autospec(
            reconciler_mod.resolve_company_workspace_id,
            side_effect=lambda uid, sym, *, cov_row=None, conn=None: state["workspaces"].get(sym.upper()),
        ),
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
            return_value=dict(policies_mod.DEFAULT_TIER_POLICIES),
        ),
    )
    monkeypatch.setattr(
        service_mod,
        "insert_reconcile_job",
        create_autospec(policies_mod.insert_reconcile_job, side_effect=_insert_job),
    )
    monkeypatch.setattr(
        service_mod,
        "ensure_user_earnings_calendar_automation",
        create_autospec(
            cov_svc.ensure_user_earnings_calendar_automation,
            return_value={"ok": True, "action": "exists"},
        ),
    )
    monkeypatch.setattr(reconciler_mod.TierPolicyReconciler, "reconcile_job", _fake_reconcile_job)
    return state


@pytest.mark.asyncio
async def test_exempt_vs_confirmable_actions_and_middleware_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = [
        cov_models.company_to_row(
            cov_models.make_new_company_dict("NVDA", name="NVIDIA", tier="core"),
            user_id=USER_ALICE,
            source="chat",
        )
    ]
    state = _wire_in_memory_watchlist(
        monkeypatch, initial_rows=initial, workspaces={"NVDA": "ws-nvda"}
    )

    mw = StandingApprovalMiddleware(USER_ALICE)

    # 1. Exempt: add <= 5 net-new tickers -> middleware returns None, tool runs without hitl_confirm
    exempt_msg = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call-add-2",
                "name": "dalton_coverage",
                "args": {"action": "add", "tickers": ["NET", "CRWD"], "tier": "radar"},
            }
        ],
    )
    assert await mw.aafter_model({"messages": [exempt_msg]}, None) is None

    forbid_hitl = create_autospec(
        sec_commands.hitl_confirm,
        side_effect=AssertionError("hitl_confirm must not be called for exempt add"),
    )
    monkeypatch.setattr(cov_tools_mod, "hitl_confirm", forbid_hitl)
    tool = cov_tools_mod.build_coverage_tools(role="chief_of_staff")[0]
    cmd = await tool.ainvoke(
        {
            "name": "dalton_coverage",
            "id": "call-add-2",
            "type": "tool_call",
            "args": {
                "action": "add",
                "tickers": ["NET", "CRWD"],
                "tier": "radar",
            },
        },
        config={"configurable": {"user_id": USER_ALICE}},
    )
    payload, _ = _unpack_command_json(cmd)
    assert payload["ok"] is True
    assert set(payload["added_tickers"]) == {"NET", "CRWD"}
    assert state["rows"][(USER_ALICE, "NET")]["tier"] == "radar"

    # 2. Batch add > 5 tickers -> middleware freezes watchlist_batch_add snapshot
    batch_msg = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call-batch-6",
                "name": "dalton_coverage",
                "args": {
                    "action": "add",
                    "tickers": ["T1", "T2", "T3", "T4", "T5", "T6"],
                    "tier": "radar",
                },
            }
        ],
    )
    batch_upd = await mw.aafter_model({"messages": [batch_msg]}, None)
    assert batch_upd is not None
    snap_batch = batch_upd["approval_snapshots"]["call-batch-6"]
    assert snap_batch["action"] == "watchlist_batch_add"
    assert snap_batch["requires_confirmation"] is True
    assert snap_batch["owner_user_id"] == USER_ALICE
    assert len(snap_batch["target_tickers"]) == 6

    # 3. set_tier on NVDA -> middleware freezes watchlist_set_tier snapshot with workspace & hash
    tier_msg = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call-tier-1",
                "name": "dalton_coverage",
                "args": {"action": "set_tier", "tickers": ["NVDA"], "tier": "radar"},
            }
        ],
    )
    tier_upd = await mw.aafter_model({"messages": [tier_msg]}, None)
    assert tier_upd is not None
    snap_tier = tier_upd["approval_snapshots"]["call-tier-1"]
    assert snap_tier["action"] == "watchlist_set_tier"
    assert snap_tier["target_tier"] == "radar"
    assert snap_tier["affected_workspaces"][0]["workspace_id"] == "ws-nvda"
    assert snap_tier["expected_roster_hash"]


@pytest.mark.asyncio
async def test_scope_changed_aborts_with_zero_db_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = [
        cov_models.company_to_row(
            cov_models.make_new_company_dict("NVDA", name="NVIDIA", tier="core"),
            user_id=USER_ALICE,
            source="chat",
        )
    ]
    state = _wire_in_memory_watchlist(
        monkeypatch, initial_rows=initial, workspaces={"NVDA": "ws-nvda"}
    )
    mw = StandingApprovalMiddleware(USER_ALICE)
    call = {
        "id": "call-drift-1",
        "name": "dalton_coverage",
        "args": {"action": "set_tier", "tickers": ["NVDA"], "tier": "radar"},
    }
    upd = await mw.aafter_model({"messages": [AIMessage(content="", tool_calls=[call])]}, None)
    assert upd is not None
    frozen_snap = upd["approval_snapshots"]["call-drift-1"]

    # Simulate concurrent tier drift BEFORE user approves confirmation card
    state["rows"][(USER_ALICE, "NVDA")]["tier"] = "watch"
    writes_before = state["writes"]

    approve_hitl = create_autospec(
        sec_commands.hitl_confirm,
        return_value=(True, {"decisions": [{"type": "approve"}]}),
    )
    monkeypatch.setattr(cov_tools_mod, "hitl_confirm", approve_hitl)

    tool = cov_tools_mod.build_coverage_tools(role="chief_of_staff")[0]
    cmd = await tool.ainvoke(
        {
            "name": "dalton_coverage",
            "id": "call-drift-1",
            "type": "tool_call",
            "args": {
                "action": "set_tier",
                "tickers": ["NVDA"],
                "tier": "radar",
                "state": {"approval_snapshots": {"call-drift-1": frozen_snap}},
            },
        },
        config={"configurable": {"user_id": USER_ALICE}},
    )
    payload, msg = _unpack_command_json(cmd)
    assert payload["success"] is False
    assert payload["code"] == "scope_changed"
    assert msg.status == "error"
    # Zero writes occurred when scope drifted!
    assert state["writes"] == writes_before
    assert state["rows"][(USER_ALICE, "NVDA")]["tier"] == "watch"
    assert state["jobs"] == {}


@pytest.mark.asyncio
async def test_checkpoint_handler_verifies_watchlist_web_confirm_and_stamps_scope_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "watchlist_batch_add" in cp_handler.WEB_CONFIRM_ACTION_TYPES
    assert "watchlist_set_tier" in cp_handler.WEB_CONFIRM_ACTION_TYPES
    assert "watchlist_remove" in cp_handler.WEB_CONFIRM_ACTION_TYPES
    assert "watchlist_tier_policy" in cp_handler.WEB_CONFIRM_ACTION_TYPES

    snap = {
        "action": "watchlist_set_tier",
        "tool_call_id": "call-wl-1",
        "owner_user_id": USER_ALICE,
        "expected_roster_hash": "abc123",
        "expected_policy_versions": {"core": 1, "radar": 1},
        "target_tickers": ["NVDA"],
        "target_tier": "radar",
        "affected_workspaces": [{"ticker": "NVDA", "workspace_id": "ws-nvda"}],
    }
    action_req = {
        "type": "watchlist_set_tier",
        "tool_call_id": "call-wl-1",
        "owner_user_id": USER_ALICE,
        "expected_roster_hash": "abc123",
        "expected_policy_versions": {"core": 1, "radar": 1},
        "target_tickers": ["NVDA"],
        "target_tier": "radar",
        "affected_workspaces": [{"ticker": "NVDA", "workspace_id": "ws-nvda"}],
    }
    assert cp_handler._snapshot_matches_action(snap, action_req) is True
    assert cp_handler._snapshot_matches_action(snap, {**action_req, "expected_roster_hash": "drifted"}) is False

    from src.config import settings as app_settings
    from src.dalton import auth as dalton_auth
    from src.dalton import settings as dalton_settings

    monkeypatch.setattr(app_settings, "HOST_MODE", "platform")
    monkeypatch.setattr(dalton_auth, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(dalton_settings, "get_dalton_auth_mode", lambda: "supabase")

    pending_record = {
        "id": "int-wl-1",
        "value": {"action_requests": [action_req]},
    }
    newest = SimpleNamespace(
        checkpoint={"channel_values": {"approval_snapshots": {"call-wl-1": snap}}}
    )
    req = ChatRequest(
        hitl_response={"int-wl-1": HITLResponse(decisions=[HITLDecision(type="approve")])}
    )
    raw_req = SimpleNamespace(headers={})
    thread_meta = {"user_id": USER_ALICE, "workspace_owner_id": USER_ALICE}

    # 1. Service principal (channel/API) is rejected with 403 web_session_required
    with pytest.raises(HTTPException) as exc_svc:
        await cp_handler._verify_web_confirm_resume(
            thread_id="t-1",
            request=req,
            newest=newest,
            pending_by_id={"int-wl-1": pending_record},
            guarded_by_id={"int-wl-1": pending_record},
            principal=SimpleNamespace(auth="service", user_id=USER_ALICE),
            raw_request=raw_req,
            thread_meta=thread_meta,
        )
    assert exc_svc.value.status_code == 403
    assert exc_svc.value.detail["code"] == "web_session_required"

    # 2. Valid initiator session with matching snapshot succeeds without rewrite
    res_ok = await cp_handler._verify_web_confirm_resume(
        thread_id="t-1",
        request=req,
        newest=newest,
        pending_by_id={"int-wl-1": pending_record},
        guarded_by_id={"int-wl-1": pending_record},
        principal=SimpleNamespace(auth="session", user_id=USER_ALICE),
        raw_request=raw_req,
        thread_meta=thread_meta,
    )
    assert res_ok is None

    # 3. Drifted snapshot on approval rewrites hitl_response with scope_changed=True
    drifted_newest = SimpleNamespace(
        checkpoint={
            "channel_values": {
                "approval_snapshots": {
                    "call-wl-1": {**snap, "expected_roster_hash": "other-hash"}
                }
            }
        }
    )
    res_drift = await cp_handler._verify_web_confirm_resume(
        thread_id="t-1",
        request=req,
        newest=drifted_newest,
        pending_by_id={"int-wl-1": pending_record},
        guarded_by_id={"int-wl-1": pending_record},
        principal=SimpleNamespace(auth="session", user_id=USER_ALICE),
        raw_request=raw_req,
        thread_meta=thread_meta,
    )
    assert res_drift is not None
    assert res_drift["int-wl-1"].scope_changed is True


def test_web_rest_watchlist_preview_mutate_conflict_and_company_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = [
        cov_models.company_to_row(
            cov_models.make_new_company_dict("NVDA", name="NVIDIA", tier="core"),
            user_id=USER_ALICE,
            source="chat",
        )
    ]
    state = _wire_in_memory_watchlist(
        monkeypatch, initial_rows=initial, workspaces={"NVDA": "ws-nvda"}
    )

    app = FastAPI()
    current_user = {"id": USER_ALICE}
    app.dependency_overrides[get_current_user_id] = lambda: current_user["id"]
    app.include_router(dalton_router)
    client = TestClient(app, raise_server_exceptions=False)

    # Preview via shared WatchlistMutationService
    r_prev = client.post(
        "/api/v1/dalton/watchlist/preview",
        json={"action": "set_tier", "tickers": ["NVDA"], "tier": "radar"},
    )
    assert r_prev.status_code == 200
    prev = r_prev.json()
    assert prev["action"] == "watchlist_set_tier"
    assert prev["requires_confirmation"] is True

    # Mutate with stale expected_roster_hash -> 409 scope_changed & zero writes
    r_conflict = client.post(
        "/api/v1/dalton/watchlist/mutate",
        json={
            "action": "set_tier",
            "tickers": ["NVDA"],
            "tier": "radar",
            "expected_roster_hash": "stale-hash",
            "expected_policy_versions": prev["expected_policy_versions"],
            "expected_target_tickers": ["NVDA"],
        },
    )
    assert r_conflict.status_code == 409
    assert r_conflict.json()["detail"]["code"] == "scope_changed"
    assert state["writes"] == 0

    # Mutate with matching snapshot -> 200 & reconciled
    r_ok = client.post(
        "/api/v1/dalton/watchlist/mutate",
        json={
            "action": "set_tier",
            "tickers": ["NVDA"],
            "tier": "radar",
            "expected_roster_hash": prev["expected_roster_hash"],
            "expected_policy_versions": prev["expected_policy_versions"],
            "expected_target_tickers": ["NVDA"],
        },
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["updated"] == ["NVDA"]
    assert state["rows"][(USER_ALICE, "NVDA")]["tier"] == "radar"
    assert len(state["reconciled_jobs"]) == 1

    # Company system user is strictly rejected on personal watchlist endpoints (401 Unauthorized)
    current_user["id"] = COMPANY_SYSTEM_USER_ID
    r_sys = client.get("/api/v1/dalton/watchlist")
    assert r_sys.status_code == 401
