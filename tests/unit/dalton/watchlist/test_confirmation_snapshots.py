"""Unit tests for server-side confirmation snapshots, StandingApprovalMiddleware, checkpoint_handler, and REST routes."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Annotated, Any, TypedDict
from unittest.mock import create_autospec

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import add_messages
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


class _RealHitlGraphState(TypedDict):
    messages: Annotated[list, add_messages]
    approval_snapshots: dict[str, dict[str, Any]]


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
        "snapshots": {},
        "user_state": {},
        "writes": 0,
        "reconciled_jobs": [],
        "workspaces": dict(workspaces or {}),
        "workspace_owners": {},
        "automations": [],
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
    ):
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
            "job_context": dict(job_context or {}),
            "roster_revision": int(roster_revision or 0),
            "yaml_synced": False,
            "status": "pending",
        }
        state["jobs"][jid] = job
        return dict(job)

    async def _insert_snapshot(
        uid: str,
        payload: Any,
        *,
        ttl_seconds: int = 900,
        snapshot_id: str | None = None,
        conn: Any = None,
    ) -> str:
        _ = (ttl_seconds, conn)
        sid = snapshot_id or str(uuid.uuid4())
        state["snapshots"][sid] = {
            "snapshot_id": sid,
            "user_id": uid,
            "payload": dict(payload),
            "consumed_at": None,
            "is_expired": False,
        }
        return sid

    async def _get_snapshot(
        uid: str,
        snapshot_id: str,
        *,
        for_update: bool = False,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        _ = (for_update, conn)
        row = state["snapshots"].get(str(snapshot_id or "").strip())
        if row is None or row["user_id"] != uid:
            return None
        return {**row, "payload": dict(row["payload"])}

    async def _consume_snapshot(
        uid: str,
        snapshot_id: str,
        *,
        conn: Any = None,
    ) -> bool:
        _ = conn
        row = state["snapshots"].get(str(snapshot_id or "").strip())
        if row is None or row["user_id"] != uid or row["consumed_at"] is not None or row["is_expired"]:
            return False
        row["consumed_at"] = "now"
        return True

    async def _get_roster_state(uid: str, *, conn: Any = None) -> dict[str, int]:
        _ = conn
        st = state["user_state"].get(uid) or {"roster_revision": 0, "last_yaml_synced_revision": 0}
        return dict(st)

    async def _bump_roster_rev(uid: str, *, conn: Any = None) -> int:
        _ = conn
        st = state["user_state"].setdefault(uid, {"roster_revision": 0, "last_yaml_synced_revision": 0})
        st["roster_revision"] += 1
        return int(st["roster_revision"])

    async def _insert_group(
        uid: str,
        *,
        label: str,
        criteria: Any,
        default_tier: str = "watch",
        auto_refresh: bool = True,
        last_resolved_at: Any = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        _ = conn
        state["writes"] += 1
        gid = str(uuid.uuid4())
        grp = {
            "group_id": gid,
            "user_id": uid,
            "label": label,
            "criteria": dict(criteria or {}),
            "default_tier": default_tier,
            "auto_refresh": bool(auto_refresh),
            "last_resolved_at": last_resolved_at,
        }
        state["groups"][gid] = grp
        return dict(grp)

    async def _update_group_resolved(uid: str, gid: str, *, last_resolved_at: Any = None, conn: Any = None):
        _ = conn
        g = state["groups"].get(gid)
        if g and g["user_id"] == uid:
            g["last_resolved_at"] = last_resolved_at
            return dict(g)
        return None

    async def _mark_group_stale(uid: str, tickers: Any, *, group_id: str, stale: bool = True, conn: Any = None):
        _ = (uid, group_id, stale, conn)
        return list(tickers)

    async def _update_tags(uid: str, tickers: list[str], tags: list[str], *, conn: Any = None):
        _ = conn
        state["writes"] += 1
        out = []
        for t in tickers:
            row = state["rows"].get((uid, t.upper()))
            if row:
                row["tags"] = list(tags)
                out.append(dict(row))
        return out

    async def _delete_group_and_exclusive(uid: str, gid: str, *, conn: Any = None):
        _ = conn
        state["writes"] += 1
        existing_groups = [g for g in state["groups"].values() if g["user_id"] == uid]
        rows = [v for (u, _), v in state["rows"].items() if u == uid]
        to_del = coverage_db.compute_exclusive_group_deletion_tickers(gid, existing_groups, rows)
        for sym in to_del:
            state["rows"].pop((uid, sym), None)
        grp = state["groups"].pop(gid, None)
        return {
            "deleted": grp is not None,
            "group_id": gid,
            "label": (grp or {}).get("label"),
            "removed_tickers": to_del,
        }

    async def _fake_reconcile_job(
        self: Any,
        uid: str,
        jid: str,
        *,
        sync_home: bool = True,
        _preclaimed_job: Any = None,
    ):
        _ = (self, uid, sync_home, _preclaimed_job)
        state["reconciled_jobs"].append(jid)
        job = state["jobs"][jid]
        job["completed_tickers"] = list(job["pending_tickers"])
        job["pending_tickers"] = []
        job["yaml_synced"] = bool(sync_home)
        job["status"] = "completed" if sync_home else "partial"
        return {
            "ok": bool(sync_home),
            "job_id": jid,
            "status": job["status"],
            "yaml_synced": job["yaml_synced"],
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
        coverage_db,
        "update_coverage_tags",
        create_autospec(coverage_db.update_coverage_tags, side_effect=_update_tags),
    )
    monkeypatch.setattr(
        coverage_db,
        "delete_coverage_group_and_exclusive_members",
        create_autospec(
            coverage_db.delete_coverage_group_and_exclusive_members,
            side_effect=_delete_group_and_exclusive,
        ),
    )
    monkeypatch.setattr(
        service_mod,
        "resolve_workspace_tracking_owner",
        create_autospec(
            service_mod.resolve_workspace_tracking_owner,
            side_effect=lambda ws_id, workspace=None, *, conn=None, unit=None: state["workspace_owners"].get(ws_id, USER_ALICE),
        ),
    )
    monkeypatch.setattr(
        service_mod,
        "list_all_user_automations",
        create_autospec(
            service_mod.list_all_user_automations,
            side_effect=lambda uid, *, name_prefix=None, conn=None: list(state["automations"]),
        ),
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
        "insert_confirm_snapshot",
        create_autospec(policies_mod.insert_confirm_snapshot, side_effect=_insert_snapshot),
    )
    monkeypatch.setattr(
        service_mod,
        "get_confirm_snapshot",
        create_autospec(policies_mod.get_confirm_snapshot, side_effect=_get_snapshot),
    )
    monkeypatch.setattr(
        service_mod,
        "consume_confirm_snapshot",
        create_autospec(policies_mod.consume_confirm_snapshot, side_effect=_consume_snapshot),
    )
    monkeypatch.setattr(
        service_mod,
        "get_user_roster_state",
        create_autospec(policies_mod.get_user_roster_state, side_effect=_get_roster_state),
    )
    monkeypatch.setattr(
        service_mod,
        "bump_user_roster_revision",
        create_autospec(policies_mod.bump_user_roster_revision, side_effect=_bump_roster_rev),
    )
    monkeypatch.setattr(
        coverage_db,
        "insert_coverage_group",
        create_autospec(coverage_db.insert_coverage_group, side_effect=_insert_group),
    )
    monkeypatch.setattr(
        coverage_db,
        "update_coverage_group_resolved",
        create_autospec(coverage_db.update_coverage_group_resolved, side_effect=_update_group_resolved),
    )
    monkeypatch.setattr(
        coverage_db,
        "mark_coverage_rows_group_stale",
        create_autospec(coverage_db.mark_coverage_rows_group_stale, side_effect=_mark_group_stale),
    )
    monkeypatch.setattr(
        service_mod,
        "ensure_user_earnings_calendar_automation",
        create_autospec(
            cov_svc.ensure_user_earnings_calendar_automation,
            return_value={"ok": True, "action": "exists"},
        ),
    )
    monkeypatch.setattr(
        reconciler_mod.TierPolicyReconciler,
        "reconcile_job",
        create_autospec(
            reconciler_mod.TierPolicyReconciler.reconcile_job,
            side_effect=_fake_reconcile_job,
        ),
    )
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

    # Preview via shared WatchlistMutationService -> issues server snapshot_id
    r_prev = client.post(
        "/api/v1/dalton/watchlist/preview",
        json={"action": "set_tier", "tickers": ["NVDA"], "tier": "radar"},
    )
    assert r_prev.status_code == 200
    prev = r_prev.json()
    assert prev["action"] == "watchlist_set_tier"
    assert prev["requires_confirmation"] is True
    assert prev["snapshot_id"]

    # Missing snapshot_id on confirmable set_tier -> 409 scope_changed & zero writes
    r_missing = client.post(
        "/api/v1/dalton/watchlist/mutate",
        json={"action": "set_tier", "tickers": ["NVDA"], "tier": "radar"},
    )
    assert r_missing.status_code == 409
    assert r_missing.json()["detail"]["code"] == "scope_changed"
    assert state["writes"] == 0

    # Tampered tier vs server-issued snapshot_id -> 409 scope_changed & zero writes
    r_tamper = client.post(
        "/api/v1/dalton/watchlist/mutate",
        json={
            "action": "set_tier",
            "snapshot_id": prev["snapshot_id"],
            "tickers": ["NVDA"],
            "tier": "watch",
        },
    )
    assert r_tamper.status_code == 409
    assert r_tamper.json()["detail"]["code"] == "scope_changed"
    assert state["writes"] == 0

    # Unknown / bogus snapshot_id -> 409 scope_changed & zero writes
    r_conflict = client.post(
        "/api/v1/dalton/watchlist/mutate",
        json={
            "action": "set_tier",
            "snapshot_id": str(uuid.uuid4()),
        },
    )
    assert r_conflict.status_code == 409
    assert r_conflict.json()["detail"]["code"] == "scope_changed"
    assert state["writes"] == 0

    # Mutate with valid server-issued snapshot_id -> 200 & reconciled
    r_ok = client.post(
        "/api/v1/dalton/watchlist/mutate",
        json={
            "action": "set_tier",
            "snapshot_id": prev["snapshot_id"],
        },
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["updated"] == ["NVDA"]
    assert state["rows"][(USER_ALICE, "NVDA")]["tier"] == "radar"
    assert len(state["reconciled_jobs"]) == 1

    # Snapshot is single-use: replaying consumed snapshot_id -> 409 scope_changed
    r_replay = client.post(
        "/api/v1/dalton/watchlist/mutate",
        json={"action": "set_tier", "snapshot_id": prev["snapshot_id"]},
    )
    assert r_replay.status_code == 409
    assert r_replay.json()["detail"]["code"] == "scope_changed"

    # Company system user is strictly rejected on personal watchlist endpoints (401 Unauthorized)
    current_user["id"] = COMPANY_SYSTEM_USER_ID
    r_sys = client.get("/api/v1/dalton/watchlist")
    assert r_sys.status_code == 401


@pytest.mark.asyncio
async def test_r3_real_langgraph_interrupt_pause_approve_and_reject_without_mocking_hitl_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 1: Real LangGraph StateGraph + MemorySaver hits real hitl_confirm (GraphInterrupt NOT swallowed)."""
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

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
    tool = cov_tools_mod.build_coverage_tools(role="chief_of_staff")[0]

    async def _run_tool(s: _RealHitlGraphState, config: RunnableConfig):
        return await tool.ainvoke(
            {
                "name": "dalton_coverage",
                "id": "call-real-hitl",
                "type": "tool_call",
                "args": {"action": "set_tier", "tickers": ["NVDA"], "tier": "radar"},
            },
            config=config,
        )

    builder = StateGraph(_RealHitlGraphState)
    builder.add_node("tool_node", _run_tool)
    builder.add_edge(START, "tool_node")
    builder.add_edge("tool_node", END)
    graph = builder.compile(checkpointer=MemorySaver())

    snap = await service_mod.WatchlistMutationService().preview_mutation(
        USER_ALICE,
        action="set_tier",
        tickers=["NVDA"],
        tier="radar",
        tool_call_id="call-real-hitl",
    )

    # 1. Pause -> Approve -> Resumes & writes DB
    cfg_ok = {"configurable": {"thread_id": "t-approve", "user_id": USER_ALICE}}
    paused = await graph.ainvoke({"messages": [], "approval_snapshots": {"call-real-hitl": snap}}, config=cfg_ok)
    assert "__interrupt__" in paused
    req0 = paused["__interrupt__"][0].value["action_requests"][0]
    assert (req0.get("type") or req0.get("action")) == "watchlist_set_tier"
    assert state["writes"] == 0

    resumed_ok = await graph.ainvoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        config=cfg_ok,
    )
    assert "__interrupt__" not in resumed_ok
    payload_ok = json.loads(resumed_ok["messages"][-1].content)
    assert payload_ok["ok"] is True
    assert state["rows"][(USER_ALICE, "NVDA")]["tier"] == "radar"

    # 2. Pause -> Reject -> Zero DB writes
    state["rows"][(USER_ALICE, "NVDA")]["tier"] = "core"
    writes_before = state["writes"]
    snap2 = await service_mod.WatchlistMutationService().preview_mutation(
        USER_ALICE,
        action="set_tier",
        tickers=["NVDA"],
        tier="radar",
        tool_call_id="call-real-hitl",
    )
    cfg_rej = {"configurable": {"thread_id": "t-reject", "user_id": USER_ALICE}}
    paused2 = await graph.ainvoke({"messages": [], "approval_snapshots": {"call-real-hitl": snap2}}, config=cfg_rej)
    assert "__interrupt__" in paused2

    resumed_rej = await graph.ainvoke(
        Command(resume={"decisions": [{"type": "reject", "message": "cancel"}]}),
        config=cfg_rej,
    )
    assert "__interrupt__" not in resumed_rej
    rej_content = str(resumed_rej["messages"][-1].content)
    assert rej_content.startswith("User declined")
    assert state["writes"] == writes_before
    assert state["rows"][(USER_ALICE, "NVDA")]["tier"] == "core"


@pytest.mark.asyncio
async def test_r3_exempt_boundaries_group_label_uuid_drift_and_update_tags_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Items 3, 4, 13: requested-count > 5 requires confirm; remove_group by label uses UUID & detects membership drift; update_tags writes outbox & covered_job_ids."""
    gid = str(uuid.uuid4())
    initial = [
        cov_models.company_to_row(
            cov_models.make_new_company_dict("NVDA", name="NVIDIA", tier="core"),
            user_id=USER_ALICE,
            source="chat",
        ),
        {
            **cov_models.company_to_row(
                cov_models.make_new_company_dict("AMD", name="AMD", tier="watch"),
                user_id=USER_ALICE,
                source="group",
            ),
            "group_id": gid,
            "extra": {"group_ids": [gid]},
        },
    ]
    state = _wire_in_memory_watchlist(
        monkeypatch, initial_rows=initial, workspaces={"NVDA": "ws-nvda"}
    )
    state["groups"][gid] = {"group_id": gid, "user_id": USER_ALICE, "label": "AI Leaders"}

    svc = service_mod.WatchlistMutationService()

    # Item 4: 6 requested tickers where only 2 validate -> STILL requires confirmation (by requested count!)
    async def _only_two_valid(tickers: Any):
        valid = [{"ticker": t, "name": t, "market": "US"} for t in list(tickers)[:2]]
        return valid, list(tickers)[2:]

    monkeypatch.setattr(
        service_mod,
        "validate_and_enrich_tickers",
        create_autospec(cov_svc.validate_and_enrich_tickers, side_effect=_only_two_valid),
    )
    prev_6 = await svc.preview_mutation(
        USER_ALICE,
        action="add",
        tickers=["A1", "A2", "BAD1", "BAD2", "BAD3", "BAD4"],
        tier="radar",
    )
    assert prev_6["requested_count"] == 6
    assert len(prev_6["target_tickers"]) == 2
    assert prev_6["requires_confirmation"] is True

    # Item 4: add touching an existing ticker (even same tier, <=5) requires confirmation
    prev_dup = await svc.preview_mutation(
        USER_ALICE, action="add", tickers=["NVDA"], tier="core", skip_ticker_validation=True
    )
    assert prev_dup["requires_confirmation"] is True

    # Item 3: remove_group previewed by label resolves UUID; drifting membership before Stage 1 -> scope_changed
    prev_grp = await svc.preview_mutation(USER_ALICE, action="remove_group", label="AI Leaders")
    assert prev_grp["group_id"] == gid
    assert prev_grp["target_tickers"] == ["AMD"]

    # Add another group member after preview -> exclusive deletion set drifts -> scope_changed with zero writes
    state["rows"][(USER_ALICE, "AVGO")] = {
        **cov_models.company_to_row(
            cov_models.make_new_company_dict("AVGO", name="Broadcom", tier="watch"),
            user_id=USER_ALICE,
            source="group",
        ),
        "group_id": gid,
        "extra": {"group_ids": [gid]},
    }
    with pytest.raises(service_mod.WatchlistScopeChangedError):
        await svc.execute_mutation(
            USER_ALICE,
            action="remove_group",
            snapshot_id=prev_grp["snapshot_id"],
            label="AI Leaders",
        )
    assert state["writes"] == 0

    # Fresh preview after drift -> executes using resolved UUID even when label is passed
    prev_grp2 = await svc.preview_mutation(USER_ALICE, action="remove_group", label="AI Leaders")
    assert set(prev_grp2["target_tickers"]) == {"AMD", "AVGO"}
    res_grp = await svc.execute_mutation(
        USER_ALICE,
        action="remove_group",
        snapshot_id=prev_grp2["snapshot_id"],
        label="AI Leaders",
    )
    assert res_grp["ok"] is True
    assert res_grp["group_id"] == gid
    assert gid not in state["groups"]

    # Item 13: update_tags writes tags + outbox job in same tx; covered_job_ids only settles pre-render jobs
    res_tags = await svc.execute_mutation(
        USER_ALICE,
        action="update_tags",
        tickers=["NVDA"],
        tags=["ai", "gpu"],
        sync_home=False,
    )
    assert res_tags["ok"] is True
    j1 = res_tags["job_id"]
    assert j1 in state["jobs"]
    assert state["jobs"][j1]["trigger_action"] == "update_tags"
    assert state["jobs"][j1]["yaml_synced"] is False

    # Simulate a second job inserted AFTER render began
    res_tags2 = await svc.execute_mutation(
        USER_ALICE,
        action="update_tags",
        tickers=["NVDA"],
        tags=["ai"],
        sync_home=False,
    )
    j2 = res_tags2["job_id"]

    async def _list_unf(uid: str, *, conn: Any = None):
        _ = conn
        return [dict(j) for j in state["jobs"].values() if j["user_id"] == uid and not j["yaml_synced"]]

    async def _upd_prog(uid: str, jid: str, **kwargs):
        _ = uid
        job = state["jobs"][jid]
        for k in ("yaml_synced", "status"):
            if kwargs.get(k) is not None:
                job[k] = kwargs[k]
        return dict(job)

    monkeypatch.setattr(reconciler_mod, "list_unfinished_reconcile_jobs", create_autospec(policies_mod.list_unfinished_reconcile_jobs, side_effect=_list_unf))
    monkeypatch.setattr(reconciler_mod, "update_reconcile_job_progress", create_autospec(policies_mod.update_reconcile_job_progress, side_effect=_upd_prog))

    marked = await reconciler_mod.TierPolicyReconciler().mark_yaml_synced_for_user(
        USER_ALICE, covered_job_ids=[j1]
    )
    assert marked == 1
    assert state["jobs"][j1]["yaml_synced"] is True
    assert state["jobs"][j2]["yaml_synced"] is False


@pytest.mark.asyncio
async def test_r4_server_issued_snapshots_web_exempt_add_group_binding_legacy_and_resolve_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 Decision 1: Web exempt single-stock add returns 200; empty group A snapshot cannot delete group B;
    add_group binds criteria/auto_refresh; legacy /coverage & /coverage/groups routes enforce snapshot_id;
    resolve_coverage_group runs unified internal action='resolve_group'."""
    gid_a = str(uuid.uuid4())
    gid_b = str(uuid.uuid4())
    initial = [
        cov_models.company_to_row(
            cov_models.make_new_company_dict("NVDA", name="NVIDIA", tier="core"),
            user_id=USER_ALICE,
            source="chat",
        ),
    ]
    state = _wire_in_memory_watchlist(
        monkeypatch, initial_rows=initial, workspaces={"NVDA": "ws-nvda"}
    )
    state["groups"][gid_a] = {"group_id": gid_a, "user_id": USER_ALICE, "label": "Empty A", "criteria": {}, "default_tier": "watch", "auto_refresh": True}
    state["groups"][gid_b] = {"group_id": gid_b, "user_id": USER_ALICE, "label": "Empty B", "criteria": {}, "default_tier": "watch", "auto_refresh": True}

    app = FastAPI()
    app.dependency_overrides[get_current_user_id] = lambda: USER_ALICE
    app.include_router(dalton_router)
    client = TestClient(app, raise_server_exceptions=False)

    # 1. Regression: Web single-stock exempt add (NO snapshot_id) -> 200 OK via real WatchlistMutationService
    r_prev_1 = client.post(
        "/api/v1/dalton/watchlist/preview",
        json={"action": "add", "tickers": ["NET"], "tier": "watch"},
    )
    assert r_prev_1.status_code == 200
    assert r_prev_1.json()["requires_confirmation"] is False
    assert r_prev_1.json()["snapshot_id"] is None

    r_add_1 = client.post(
        "/api/v1/dalton/watchlist/mutate",
        json={"action": "add", "tickers": ["NET"], "tier": "watch"},
    )
    assert r_add_1.status_code == 200
    assert r_add_1.json()["added_tickers"] == ["NET"]
    assert (USER_ALICE, "NET") in state["rows"]

    # 2. Empty group A snapshot CANNOT be used to delete empty group B -> 409 scope_changed
    r_prev_ga = client.post(
        "/api/v1/dalton/watchlist/preview",
        json={"action": "remove_group", "group_id": gid_a},
    )
    assert r_prev_ga.status_code == 200
    snap_ga = r_prev_ga.json()["snapshot_id"]
    assert snap_ga

    r_cross_grp = client.post(
        "/api/v1/dalton/watchlist/mutate",
        json={"action": "remove_group", "group_id": gid_b, "snapshot_id": snap_ga},
    )
    assert r_cross_grp.status_code == 409
    assert gid_b in state["groups"]

    # 3. add_group binds criteria & auto_refresh -> tampering either rejects with 409
    async def _fake_prev_grp(label: str, criteria: Any):
        return {
            "ok": True,
            "label": label,
            "criteria": dict(criteria or {}),
            "companies": [{"ticker": "CRWD", "name": "CrowdStrike", "market": "US"}],
            "not_found": [],
        }

    monkeypatch.setattr(
        service_mod,
        "preview_coverage_group",
        create_autospec(cov_svc.preview_coverage_group, side_effect=_fake_prev_grp),
    )
    r_prev_ag = client.post(
        "/api/v1/dalton/watchlist/preview",
        json={"action": "add_group", "label": "Security", "criteria": {"sector": "Tech"}, "default_tier": "radar", "auto_refresh": True},
    )
    assert r_prev_ag.status_code == 200
    snap_ag = r_prev_ag.json()["snapshot_id"]
    r_tamper_crit = client.post(
        "/api/v1/dalton/watchlist/mutate",
        json={"action": "add_group", "label": "Security", "criteria": {"sector": "Energy"}, "auto_refresh": True, "snapshot_id": snap_ag},
    )
    assert r_tamper_crit.status_code == 409
    r_tamper_auto = client.post(
        "/api/v1/dalton/watchlist/mutate",
        json={"action": "add_group", "label": "Security", "criteria": {"sector": "Tech"}, "auto_refresh": False, "snapshot_id": snap_ag},
    )
    assert r_tamper_auto.status_code == 409

    # 4. Legacy POST /coverage and DELETE /coverage/groups/{id} enforce snapshot_id
    r_legacy_no_snap = client.post(
        "/api/v1/dalton/coverage",
        json={"action": "set_tier", "tickers": ["NVDA"], "tier": "watch"},
    )
    assert r_legacy_no_snap.status_code == 409

    r_legacy_del_no_snap = client.delete(f"/api/v1/dalton/coverage/groups/{gid_a}")
    assert r_legacy_del_no_snap.status_code == 409

    r_legacy_del_ok = client.delete(f"/api/v1/dalton/coverage/groups/{gid_a}?snapshot_id={snap_ga}")
    assert r_legacy_del_ok.status_code == 200
    assert gid_a not in state["groups"]

    # 5. Internal quarterly refresh runs through WatchlistMutationService(action='resolve_group')
    res_resolve = await cov_svc.resolve_coverage_group(USER_ALICE, gid_b, sync_home=True)
    assert res_resolve["ok"] is True
    assert "CRWD" in res_resolve["added_tickers"]
    assert res_resolve["job_id"] in state["jobs"]
    assert state["jobs"][res_resolve["job_id"]]["trigger_action"] == "resolve_group"
    assert state["jobs"][res_resolve["job_id"]]["roster_revision"] >= 1

    # 6. r5: Public /watchlist/mutate rejects internal-only action='resolve_group' (400),
    #    public POST /coverage/groups/{id}/resolve requires snapshot_id (409 without),
    #    and legacy POST /coverage/groups/preview issues a real server snapshot_id.
    r_pub_resolve_mutate = client.post(
        "/api/v1/dalton/watchlist/mutate",
        json={"action": "resolve_group", "group_id": gid_b},
    )
    assert r_pub_resolve_mutate.status_code == 400

    r_pub_resolve_no_snap = client.post(f"/api/v1/dalton/coverage/groups/{gid_b}/resolve")
    assert r_pub_resolve_no_snap.status_code == 409

    r_legacy_preview = client.post(
        "/api/v1/dalton/coverage/groups/preview",
        json={"label": "Legacy Cloud", "criteria": {"sector": "Tech"}, "default_tier": "watch"},
    )
    assert r_legacy_preview.status_code == 200
    assert r_legacy_preview.json()["snapshot_id"]
    assert "CRWD" in r_legacy_preview.json()["matched_tickers"]


@pytest.mark.asyncio
async def test_r5_shared_thread_strips_context_and_withholds_tool_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r5: Individually shared thread uses real get_thread_auth_meta, strips context,
    withholds dalton_coverage tool, and fails closed when share lookup errors."""
    from types import SimpleNamespace
    from src.dalton.tools import build_dalton_tools
    from src.dalton.watchlist import privacy as privacy_mod
    from src.server.database.conversation import threads_read as threads_read_mod

    orig_get_meta = threads_read_mod.get_thread_auth_meta
    monkeypatch.setattr(
        threads_read_mod,
        "get_thread_auth_meta",
        create_autospec(
            orig_get_meta,
            return_value={"user_id": USER_ALICE, "is_shared": True},
        ),
    )
    assert await privacy_mod.is_thread_shared("thread-shared-1") is True
    assert (
        privacy_mod.is_personal_watchlist_turn_allowed(
            own_data=True,
            thread_is_shared=True,
            user_id=USER_ALICE,
            owner_id=USER_ALICE,
        )
        is False
    )
    tools_shared = build_dalton_tools(
        "chief_of_staff",
        None,
        allow_personal_watchlist=privacy_mod.is_personal_watchlist_turn_allowed(
            own_data=True,
            thread_is_shared=await privacy_mod.is_thread_shared("thread-shared-1"),
            user_id=USER_ALICE,
            owner_id=USER_ALICE,
        ),
    )
    assert all(getattr(t, "name", "") != "dalton_coverage" for t in tools_shared)

    req_shared = SimpleNamespace(
        additional_context=[
            {"type": "watchlist_context", "content": "<watchlist_context>secret</watchlist_context>"},
            {"type": "directive", "content": "keep"},
        ]
    )
    await privacy_mod.sanitize_turn_watchlist_context(
        req_shared,
        thread_id="thread-shared-1",
        is_shared=False,
        user_id=USER_ALICE,
        workspace_owner_id=USER_ALICE,
    )
    assert len(req_shared.additional_context) == 1
    assert req_shared.additional_context[0]["type"] == "directive"

    # Fail-closed when thread auth meta lookup raises
    monkeypatch.setattr(
        threads_read_mod,
        "get_thread_auth_meta",
        create_autospec(
            orig_get_meta,
            side_effect=RuntimeError("db down"),
        ),
    )
    assert await privacy_mod.is_thread_shared("thread-err-1") is True
