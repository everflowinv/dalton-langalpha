"""Unit tests for the three billing gap fixes + #478 workflow settlement regression.

Covers:
1. Gap 1: `dalton_complete` / `dalton_llm_journal` resolves user custom models via the
   chat path (`user_models.classify_model` / `route_custom_entry` / `client.metadata`)
   before pricing lookup; unresolved cash models are marked `pricing_failed=True` with
   `pricing_failed_models` and retain the conservative floor.
2. Gap 2: Unpriced BYOK/cash calls in `calculate_cost_from_per_call_records` mark
   `pricing_failed=True` + `pricing_failed_models`, `budget/service.py` never counts them
   as $0 cash or subscription notional, and `database/budget.py` (Steps 1 and 2) treats
   them as incomplete so the `reserved_usd` floor is preserved.
3. Gap 3: `finalize_run` stamps `conversation_responses.usage_settled_at` only when a
   turn usage row (`msg_type='ptc'` with non-empty `per_call_costs`) exists in
   `conversation_usages` or zero model calls are proven (`no_model_calls=True`); when a
   paid model omits `stream_usage`, `usage_settled_at` stays NULL, a secret-free warning
   is logged, and budget settlement retains the reservation floor.
4. Regression (#478): Finished `RunWorkflow` turns whose workflow children and direct
   subagents all have complete, priced usage rows still settle by actual usage.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, create_autospec, patch

import psycopg
import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from src.dalton.budget import service as budget_svc
from src.dalton.database import budget as budget_db
from src.dalton.database import llm_journal as journal_db
from src.dalton.llm import complete as complete_mod
from src.dalton.llm.complete import (
    compute_call_costs,
    compute_call_costs_detailed,
    dalton_complete,
)
from src.llms.api_call import collect_api_call_usage, make_api_call
from src.server.database.runs import credit_ledger as credit_ledger_db
from src.server.database.runs import lifecycle as lifecycle_db
from src.server.database.runs.lifecycle import RunOutcome, finalize_run
from src.server.services.llm import user_models
from src.server.services.llm.service import LLMService
from src.server.services.runs import finalization
from src.server.services.runs.coordinator import RunCoordinator, RunHandle
from src.server.services.runs.recovery import RecoveryScanner
from src.utils.tracking.core import calculate_cost_from_per_call_records
from src.utils.tracking.per_call_token_tracker import PerCallTokenTracker


# ---------------------------------------------------------------------------
# Shared fake DBs / cursors mirroring real Postgres row shapes
# ---------------------------------------------------------------------------


@dataclass
class _FakeJournalDB:
    """In-memory `dalton_llm_journal` + `dalton_llm_journal_attempts` store."""

    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    attempts: dict[tuple[str, int, int], dict[str, Any]] = field(default_factory=dict)
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    def conn(self) -> Any:
        store = self

        class _Cursor:
            def __init__(self) -> None:
                self._one: dict[str, Any] | None = None
                self._many: list[dict[str, Any]] = []

            async def __aenter__(self) -> "_Cursor":
                return self

            async def __aexit__(self, *_args: Any) -> bool:
                return False

            async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
                text = " ".join(sql.split()).upper()
                self._one = None
                self._many = []

                if text.startswith("SELECT * FROM DALTON_LLM_JOURNAL WHERE INVOCATION_ID"):
                    row = store.rows.get(str(params[0]))
                    self._one = dict(row) if row is not None else None
                    return

                if text.startswith("INSERT INTO DALTON_LLM_JOURNAL"):
                    inv_id = str(params[0])
                    if inv_id not in store.rows:
                        has_usage = len(params) == 9
                        store.rows[inv_id] = {
                            "invocation_id": inv_id,
                            "user_id": params[1],
                            "scope_ref": params[2],
                            "purpose": params[3],
                            "model_key": params[4],
                            "provider": params[5],
                            "billing_type": params[6],
                            "request_hash": params[7],
                            "usage": json.loads(params[8]) if has_usage else None,
                            "response": None,
                            "state": "pending",
                            "attempt": 1,
                            "cash_usd": Decimal("0"),
                            "notional_usd": Decimal("0"),
                            "error_kind": None,
                            "error_text": None,
                            "claimed_at": store.now,
                            "completed_at": None,
                            "created_at": store.now,
                        }
                        self._one = dict(store.rows[inv_id])
                    return

                if "SET STATE = 'DONE'" in text and "DALTON_LLM_JOURNAL" in text:
                    resp_json, usage_json, add_cash, add_notional, prov, btype, inv_id, att = params[:8]
                    row = store.rows.get(str(inv_id))
                    if row is not None and int(row["attempt"]) == int(att):
                        row["state"] = "done"
                        row["completed_at"] = store.now
                        row["response"] = json.loads(resp_json) if resp_json is not None else None
                        row["usage"] = json.loads(usage_json) if isinstance(usage_json, str) else usage_json
                        row["cash_usd"] = Decimal(str(row.get("cash_usd") or "0")) + Decimal(str(add_cash))
                        row["notional_usd"] = Decimal(str(row.get("notional_usd") or "0")) + Decimal(str(add_notional))
                        if prov is not None:
                            row["provider"] = prov
                        if btype is not None:
                            row["billing_type"] = btype
                        self._one = dict(row)
                    if "DALTON_LLM_JOURNAL_ATTEMPTS" in text and row is not None:
                        records = json.loads(str(params[-3]))
                        for item in records:
                            seq = int(item.get("call_seq") or 1)
                            key = (str(inv_id), int(att), seq)
                            raw_occ = item.get("occurred_at")
                            occ = datetime.fromisoformat(raw_occ) if isinstance(raw_occ, str) and raw_occ else store.now
                            store.attempts[key] = {
                                "invocation_id": str(inv_id),
                                "attempt": int(att),
                                "call_seq": seq,
                                "user_id": row["user_id"],
                                "scope_ref": row["scope_ref"],
                                "purpose": row["purpose"],
                                "model_key": row["model_key"],
                                "provider": item.get("provider") or prov or row["provider"],
                                "billing_type": item.get("billing_type") or btype or row["billing_type"],
                                "outcome": str(item.get("outcome") or "done"),
                                "error_kind": item.get("error_kind"),
                                "usage": item.get("usage"),
                                "cash_usd": Decimal(str(item.get("cash_usd") or "0")),
                                "notional_usd": Decimal(str(item.get("notional_usd") or "0")),
                                "usage_known": bool(item.get("usage_known", True)),
                                "unknown_usage_calls": int(item.get("unknown_usage_calls") or 0),
                                "occurred_at": occ,
                            }
                    return

                if "UPDATE DALTON_LLM_JOURNAL" in text and "SET STATE = %S" in text:
                    upd_part = text.split("ALREADY_SETTLED")[0]
                    has_usage_param = "USAGE = " in upd_part
                    has_provider_param = "PROVIDER = COALESCE(%S, PROVIDER)" in upd_part
                    idx = 5
                    usage_idx = idx if has_usage_param else None
                    if has_usage_param:
                        idx += 1
                    prov_idx = idx if has_provider_param else None
                    if has_provider_param:
                        idx += 2
                    inv_id = str(params[idx])
                    att = int(params[idx + 1])
                    prov = params[prov_idx] if prov_idx is not None else None
                    btype = params[prov_idx + 1] if prov_idx is not None else None
                    row = store.rows.get(inv_id)
                    if row is not None and int(row["attempt"]) == att:
                        row["state"] = params[0]
                        row["completed_at"] = store.now
                        row["error_kind"] = params[1]
                        row["error_text"] = params[2]
                        if params[3] is not None:
                            row["cash_usd"] = Decimal(str(row.get("cash_usd") or "0")) + Decimal(str(params[3]))
                        if params[4] is not None:
                            row["notional_usd"] = Decimal(str(row.get("notional_usd") or "0")) + Decimal(str(params[4]))
                        if usage_idx is not None:
                            row["usage"] = json.loads(params[usage_idx]) if isinstance(params[usage_idx], str) else params[usage_idx]
                        if prov is not None:
                            row["provider"] = prov
                        if btype is not None:
                            row["billing_type"] = btype
                        self._one = dict(row)
                    if "DALTON_LLM_JOURNAL_ATTEMPTS" in text and row is not None:
                        records = json.loads(str(params[-3]))
                        for item in records:
                            seq = int(item.get("call_seq") or 1)
                            key = (inv_id, att, seq)
                            raw_occ = item.get("occurred_at")
                            occ = datetime.fromisoformat(raw_occ) if isinstance(raw_occ, str) and raw_occ else store.now
                            store.attempts[key] = {
                                "invocation_id": inv_id,
                                "attempt": att,
                                "call_seq": seq,
                                "user_id": row["user_id"],
                                "scope_ref": row["scope_ref"],
                                "purpose": row["purpose"],
                                "model_key": row["model_key"],
                                "provider": item.get("provider") or prov or row["provider"],
                                "billing_type": item.get("billing_type") or btype or row["billing_type"],
                                "outcome": str(item.get("outcome") or params[0]),
                                "error_kind": item.get("error_kind"),
                                "usage": item.get("usage"),
                                "cash_usd": Decimal(str(item.get("cash_usd") or "0")),
                                "notional_usd": Decimal(str(item.get("notional_usd") or "0")),
                                "usage_known": bool(item.get("usage_known", True)),
                                "unknown_usage_calls": int(item.get("unknown_usage_calls") or 0),
                                "occurred_at": occ,
                            }
                    return

                if "SELECT COALESCE(SUM(CASH_USD), 0) AS JOURNAL_CASH_USD" in text:
                    day_start, day_end, uid = params[0], params[1], params[2]
                    total = Decimal("0")
                    attempt_invs = {inv for inv, _a, _s in store.attempts}
                    for a in store.attempts.values():
                        if uid is not None and a.get("user_id") not in (uid, None):
                            continue
                        if day_start <= a["occurred_at"] < day_end:
                            if a["outcome"] in ("done", "indeterminate") or a["cash_usd"] > 0:
                                total += Decimal(str(a["cash_usd"]))
                    for inv_id, r in store.rows.items():
                        if inv_id in attempt_invs:
                            continue
                        if uid is not None and r.get("user_id") not in (uid, None):
                            continue
                        if day_start <= r["created_at"] < day_end:
                            if r["state"] in ("done", "indeterminate") or r["cash_usd"] > 0:
                                total += Decimal(str(r["cash_usd"]))
                    self._one = {"journal_cash_usd": total}
                    return

                if "FROM ( SELECT A.INVOCATION_ID" in text and "DALTON_LLM_JOURNAL_ATTEMPTS" in text:
                    day_start, day_end, uid = params[0], params[1], params[2]
                    out: list[dict[str, Any]] = []
                    attempt_invs = {inv for inv, _a, _s in store.attempts}
                    for a in store.attempts.values():
                        if uid is not None and a.get("user_id") not in (uid, None):
                            continue
                        if day_start <= a["occurred_at"] < day_end:
                            out.append(
                                {
                                    **a,
                                    "state": a["outcome"],
                                    "created_at": a["occurred_at"],
                                    "completed_at": a["occurred_at"],
                                }
                            )
                    for inv_id, r in store.rows.items():
                        if inv_id in attempt_invs:
                            continue
                        if uid is not None and r.get("user_id") not in (uid, None):
                            continue
                        if day_start <= r["created_at"] < day_end:
                            out.append(dict(r))
                    self._many = out
                    return

            async def fetchone(self) -> dict[str, Any] | None:
                return self._one

            async def fetchall(self) -> list[dict[str, Any]]:
                return list(self._many)

        class _Conn:
            @asynccontextmanager
            async def transaction(self):
                yield

            def cursor(self, row_factory: Any = None) -> _Cursor:
                return _Cursor()

        return _Conn()


@dataclass
class _FakeBudgetAndLifecycleDB:
    """Behavioral mirror of `conversation_responses`, `conversation_usages`,
    `subagent_runs`, and `dalton_budget_reservations` for SQL-level tests."""

    reservations: dict[str, dict[str, Any]] = field(default_factory=dict)
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    subagent_runs: list[dict[str, Any]] = field(default_factory=list)
    usages: list[dict[str, Any]] = field(default_factory=list)
    executed_sql: list[str] = field(default_factory=list)
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    has_dalton_reservations_table: bool = True

    @staticmethod
    def _dalton_cash_usd(cu: dict[str, Any], rate: Decimal) -> Decimal:
        tu = cu.get("token_usage")
        if isinstance(tu, dict) and isinstance(tu.get("per_call_costs"), list):
            llm_usd = sum(
                (
                    Decimal(str(c.get("cost") or 0))
                    for c in tu["per_call_costs"]
                    if str(c.get("billing_type") or "platform") != "oauth"
                ),
                Decimal("0"),
            )
            infra = Decimal(str(cu.get("infrastructure_credits") or 0)) / rate
            return llm_usd + infra
        return Decimal(str(cu.get("total_credits") or 0)) / rate

    @staticmethod
    def _row_pricing_complete(cu: dict[str, Any]) -> bool:
        tu = cu.get("token_usage")
        if bool(cu.get("is_byok")):
            if not isinstance(tu, dict) or not isinstance(tu.get("per_call_costs"), list):
                return False
        if isinstance(tu, dict):
            if tu.get("pricing_failed") is True:
                return False
            pcc = tu.get("per_call_costs")
            if isinstance(pcc, list) and any(
                isinstance(c, dict) and c.get("pricing_failed") is True for c in pcc
            ):
                return False
        return True

    @staticmethod
    def _is_finished_workflow_driver(sr: dict[str, Any]) -> bool:
        facts = sr.get("replay_facts")
        return (
            sr.get("status") != "in_progress"
            and sr.get("cause") == "init"
            and isinstance(facts, dict)
            and facts.get("workflow_driver") is True
        )

    def _has_task_run_usage(self, run_ref: str, task_run_id: str) -> bool:
        return any(
            cu["conversation_response_id"] == run_ref
            and cu["msg_type"] == "task"
            and isinstance(cu.get("token_usage"), dict)
            and cu["token_usage"].get("task_run_id") == task_run_id
            for cu in self.usages
        )

    def _has_ptc_turn_usage(self, run_id: str) -> bool:
        return any(
            cu["conversation_response_id"] == run_id
            and cu["msg_type"] == "ptc"
            and isinstance(cu.get("token_usage"), dict)
            and isinstance(cu["token_usage"].get("per_call_costs"), list)
            and len(cu["token_usage"]["per_call_costs"]) > 0
            for cu in self.usages
        )

    @staticmethod
    def _has_dalton_finalized_at(cr: dict[str, Any]) -> bool:
        meta = cr.get("metadata")
        if not isinstance(meta, dict):
            return False
        raw_ts = meta.get("dalton_finalized_at")
        return isinstance(raw_ts, str) and len(raw_ts) >= 11 and raw_ts[10] == "T"

    @staticmethod
    def _proven_no_model_calls(cr: dict[str, Any]) -> bool:
        meta = cr.get("metadata")
        return isinstance(meta, dict) and meta.get("dalton_no_model_calls") is True

    @staticmethod
    def _effective_response_finalized_at(cr: dict[str, Any]) -> datetime:
        meta = cr.get("metadata")
        if isinstance(meta, dict):
            raw_ts = meta.get("dalton_finalized_at")
            if isinstance(raw_ts, str) and raw_ts:
                try:
                    return datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                except ValueError:
                    pass
        created = cr.get("created_at") or datetime.now(UTC)
        exec_s = max(float(cr.get("execution_time") or 0.0), 0.0)
        return created + timedelta(seconds=exec_s)

    @staticmethod
    def _parent_settled_grace_passed(cr: dict[str, Any], grace_cutoff: datetime) -> bool:
        meta = cr.get("metadata")
        if isinstance(meta, dict):
            raw_ts = meta.get("dalton_finalized_at")
            if isinstance(raw_ts, str) and len(raw_ts) >= 11 and raw_ts[10] == "T":
                try:
                    return datetime.fromisoformat(raw_ts.replace("Z", "+00:00")) < grace_cutoff
                except ValueError:
                    pass
        # Legacy parent without dalton_finalized_at: child grace is governed by child's own finalized_at.
        return True

    def _response_in_abandoned_scope(
        self, rid: str, cr: dict[str, Any], *, include_reservations: bool = True
    ) -> bool:
        if Decimal(str(cr.get("in_flight_credits") or 0)) > Decimal("0") and cr.get("user_id") is not None:
            return True
        if self._has_dalton_finalized_at(cr):
            return True
        if not include_reservations:
            return False
        resv = self.reservations.get(rid)
        return resv is not None and resv.get("reserved_usd") is not None

    def _direct_subagent_chain_has_usage(self, run_ref: str, sr: dict[str, Any]) -> bool:
        same_parent = [s for s in self.subagent_runs if s.get("parent_run_id") == run_ref]
        chain_ids: set[str] = {str(sr["task_run_id"])}
        chain_preds: set[str] = (
            {str(sr["predecessor_run_id"])} if sr.get("predecessor_run_id") else set()
        )
        chain_tasks: set[tuple[Any, Any]] = (
            {(sr.get("thread_id"), sr.get("task_id"))}
            if sr.get("thread_id") is not None and sr.get("task_id") is not None
            else set()
        )
        changed = True
        while changed:
            changed = False
            for s in same_parent:
                sid = str(s["task_run_id"])
                if sid in chain_ids:
                    continue
                spred = str(s["predecessor_run_id"]) if s.get("predecessor_run_id") else None
                stask = (
                    (s.get("thread_id"), s.get("task_id"))
                    if s.get("thread_id") is not None and s.get("task_id") is not None
                    else None
                )
                if (
                    (spred is not None and spred in chain_ids)
                    or sid in chain_preds
                    or (stask is not None and stask in chain_tasks)
                ):
                    chain_ids.add(sid)
                    if spred is not None:
                        chain_preds.add(spred)
                    if stask is not None:
                        chain_tasks.add(stask)
                    changed = True
        return any(self._has_task_run_usage(run_ref, cid) for cid in chain_ids)

    def _all_direct_subagents_have_usage(self, run_ref: str) -> bool:
        children = [sr for sr in self.subagent_runs if sr["parent_run_id"] == run_ref]
        return all(self._direct_subagent_chain_has_usage(run_ref, sr) for sr in children)

    def make_conn(self) -> Any:
        db = self

        class _Cursor:
            def __init__(self) -> None:
                self._one: dict[str, Any] | None = None
                self._many: list[dict[str, Any]] = []
                self.rowcount: int = 0

            async def __aenter__(self) -> "_Cursor":
                return self

            async def __aexit__(self, *_args: Any) -> bool:
                return False

            async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
                db.executed_sql.append(sql)
                self._one = None
                self._many = []
                self.rowcount = 0
                grace_cutoff = db.now - timedelta(minutes=30)

                if "to_regclass(" in sql and "dalton_budget_reservations" in sql:
                    self._one = {
                        "reg": (
                            "public.dalton_budget_reservations"
                            if db.has_dalton_reservations_table
                            else None
                        )
                    }
                    return

                if "UPDATE conversation_responses" in sql and "WHERE conversation_response_id = %s AND status = 'in_progress'" in sql:
                    # finalize_run primary CAS
                    status = params[0]
                    interrupt_reason = params[1]
                    no_model_calls = bool(params[10])
                    run_id = str(params[12])
                    row = db.responses.get(run_id)
                    if row is not None and row.get("status") == "in_progress":
                        row["status"] = "cancelled" if row.get("cancel_requested_at") else status
                        row["interrupt_reason"] = None if row.get("cancel_requested_at") else interrupt_reason
                        meta = dict(row.get("metadata") or {})
                        if isinstance(params[3], lifecycle_db.SafeJson):
                            meta.update(params[3].obj)
                        meta["dalton_finalized_at"] = db.now.isoformat().replace("+00:00", "Z")
                        if no_model_calls:
                            meta["dalton_no_model_calls"] = True
                            row["usage_settled_at"] = db.now
                        row["metadata"] = meta
                        self._one = dict(row)
                        self.rowcount = 1
                    return

                if "UPDATE conversation_responses" in sql and "AND EXISTS (" in sql and "FROM conversation_usages cu" in sql:
                    run_id = str(params[0])
                    row = db.responses.get(run_id)
                    if (
                        row is not None
                        and row.get("usage_settled_at") is None
                        and db._has_ptc_turn_usage(run_id)
                    ):
                        row["usage_settled_at"] = db.now
                        self._one = {"usage_settled_at": db.now}
                        self.rowcount = 1
                    return

                if "WITH abandoned_no_ptc_parents AS" in sql:
                    uid_filter = params[0]
                    rate = params[3]
                    stamped_ids: set[str] = set()
                    for rid, cr in db.responses.items():
                        if cr.get("usage_settled_at") is not None:
                            continue
                        if cr.get("status") == "in_progress":
                            continue
                        if uid_filter is not None:
                            resv = db.reservations.get(rid)
                            if cr.get("user_id") != uid_filter and not (resv and resv["user_id"] == uid_filter):
                                continue
                        if not db._response_in_abandoned_scope(rid, cr):
                            continue
                        if db._effective_response_finalized_at(cr) >= grace_cutoff:
                            continue
                        if db._has_ptc_turn_usage(rid):
                            continue
                        cr["usage_settled_at"] = db.now
                        stamped_ids.add(rid)

                    settled_count = 0
                    for r in db.reservations.values():
                        if uid_filter is not None and r["user_id"] != uid_filter:
                            continue
                        if r["state"] not in ("open", "released"):
                            continue
                        cr = db.responses.get(r["run_ref"])
                        if not cr or cr.get("status") == "in_progress":
                            continue
                        if cr.get("usage_settled_at") is None and r["run_ref"] not in stamped_ids:
                            continue
                        if db._effective_response_finalized_at(cr) >= grace_cutoff:
                            continue
                        if db._has_ptc_turn_usage(r["run_ref"]):
                            continue
                        children = [sr for sr in db.subagent_runs if sr["parent_run_id"] == r["run_ref"]]
                        if any(
                            sr["status"] == "in_progress"
                            or (
                                sr["usage_settled_at"] is None
                                and (sr.get("finalized_at") is None or sr["finalized_at"] >= grace_cutoff)
                            )
                            for sr in children
                        ):
                            continue
                        run_usages = [u for u in db.usages if u["conversation_response_id"] == r["run_ref"]]
                        usage_usd = sum(
                            (db._dalton_cash_usd(u, rate) for u in run_usages),
                            Decimal("0"),
                        )
                        all_complete = (
                            db._proven_no_model_calls(cr)
                            and all(db._row_pricing_complete(u) for u in run_usages)
                            and db._all_direct_subagents_have_usage(r["run_ref"])
                        )
                        r["state"] = "settled"
                        r["settled_at"] = db.now
                        if all_complete:
                            r["settled_usd"] = usage_usd
                            r["settle_source"] = "usage"
                        else:
                            r["settled_usd"] = max(r["reserved_usd"], r["observed_usd"], usage_usd)
                            r["settle_source"] = "conservative"
                        settled_count += 1
                    self.rowcount = settled_count
                    return

                if "UPDATE conversation_responses" in sql and "SET usage_settled_at = NOW()" in sql and "dalton_finalized_at" in sql:
                    include_resv = "dalton_budget_reservations" in sql
                    if include_resv and not db.has_dalton_reservations_table:
                        raise RuntimeError('relation "dalton_budget_reservations" does not exist')
                    cnt = 0
                    for rid, cr in db.responses.items():
                        if cr.get("usage_settled_at") is not None or cr.get("status") == "in_progress":
                            continue
                        if not db._response_in_abandoned_scope(rid, cr, include_reservations=include_resv):
                            continue
                        if db._effective_response_finalized_at(cr) < grace_cutoff:
                            cr["usage_settled_at"] = db.now
                            cnt += 1
                    self.rowcount = cnt
                    return

                if (
                    "UPDATE conversation_responses" in sql
                    and "SET usage_settled_at = NOW()" in sql
                    and "in_flight_credits > 0" in sql
                    and "dalton_finalized_at" not in sql
                ):
                    # Verbatim upstream `settle_abandoned("run")` SQL (no 30m wait on responses).
                    cnt = 0
                    for _rid, cr in db.responses.items():
                        if cr.get("usage_settled_at") is not None or cr.get("status") == "in_progress":
                            continue
                        if Decimal(str(cr.get("in_flight_credits") or 0)) <= Decimal("0"):
                            continue
                        if cr.get("user_id") is None:
                            continue
                        cr["usage_settled_at"] = db.now
                        cnt += 1
                    self.rowcount = cnt
                    return

                if "UPDATE subagent_runs" in sql and "SET usage_settled_at = NOW()" in sql and "parent_run_id" in sql:
                    if "dalton_finalized_at" not in sql:
                        # Verbatim upstream `settle_abandoned("task")` SQL.
                        cnt = 0
                        for sr in db.subagent_runs:
                            if sr.get("usage_settled_at") is not None or sr.get("status") == "in_progress":
                                continue
                            if Decimal(str(sr.get("in_flight_credits") or 0)) <= Decimal("0"):
                                continue
                            if sr.get("user_id") is None:
                                continue
                            if sr.get("finalized_at") is None or sr["finalized_at"] >= grace_cutoff:
                                continue
                            parent_id = sr.get("parent_run_id")
                            parent = db.responses.get(parent_id) if parent_id is not None else None
                            if parent is not None and (
                                parent.get("status") == "in_progress"
                                or parent.get("usage_settled_at") is None
                                or parent["usage_settled_at"] > grace_cutoff
                            ):
                                continue
                            sr["usage_settled_at"] = db.now
                            cnt += 1
                        self.rowcount = cnt
                        return
                    include_resv = "dalton_budget_reservations" in sql
                    if include_resv and not db.has_dalton_reservations_table:
                        raise RuntimeError('relation "dalton_budget_reservations" does not exist')
                    cnt = 0
                    for sr in db.subagent_runs:
                        if sr.get("usage_settled_at") is not None or sr.get("status") == "in_progress":
                            continue
                        if sr.get("finalized_at") is None or sr["finalized_at"] >= grace_cutoff:
                            continue
                        if db._is_finished_workflow_driver(sr):
                            continue
                        parent_id = sr.get("parent_run_id")
                        parent = db.responses.get(parent_id) if parent_id is not None else None
                        wf_in_scope = False
                        facts = sr.get("replay_facts")
                        if (
                            parent_id is None
                            and isinstance(facts, dict)
                            and facts.get("workflow_driver_run_id") is not None
                        ):
                            drv_id = str(facts["workflow_driver_run_id"])
                            for drv in db.subagent_runs:
                                if (
                                    drv.get("thread_id") == sr.get("thread_id")
                                    and str(drv.get("task_run_id")) == drv_id
                                ):
                                    wf_parent_id = drv.get("parent_run_id")
                                    wf_parent = db.responses.get(wf_parent_id) if wf_parent_id is not None else None
                                    if (
                                        wf_parent is not None
                                        and wf_parent.get("status") != "in_progress"
                                        and wf_parent.get("usage_settled_at") is not None
                                        and (
                                            db._has_dalton_finalized_at(wf_parent)
                                            or (
                                                include_resv
                                                and wf_parent_id in db.reservations
                                                and db.reservations[wf_parent_id].get("reserved_usd") is not None
                                            )
                                        )
                                        and db._parent_settled_grace_passed(wf_parent, grace_cutoff)
                                    ):
                                        wf_in_scope = True
                                        break
                        in_scope = (
                            (Decimal(str(sr.get("in_flight_credits") or 0)) > Decimal("0") and sr.get("user_id") is not None)
                            or (
                                parent is not None
                                and (
                                    db._has_dalton_finalized_at(parent)
                                    or (
                                        include_resv
                                        and parent_id in db.reservations
                                        and db.reservations[parent_id].get("reserved_usd") is not None
                                    )
                                )
                            )
                            or wf_in_scope
                        )
                        if not in_scope:
                            continue
                        if parent is not None:
                            if (
                                parent.get("status") == "in_progress"
                                or parent.get("usage_settled_at") is None
                                or not db._parent_settled_grace_passed(parent, grace_cutoff)
                            ):
                                continue
                        sr["usage_settled_at"] = db.now
                        cnt += 1
                    self.rowcount = cnt
                    return

                if "AS unreserved_usd" in sql:
                    floor_usd, _floor2, rate, user_id, day_start, day_end = params
                    total = Decimal("0")
                    for cu in db.usages:
                        if cu.get("user_id") != user_id:
                            continue
                        created = cu.get("created_at") or db.now
                        if not (day_start <= created < day_end):
                            continue
                        if cu["conversation_response_id"] in db.reservations:
                            continue
                        base = db._dalton_cash_usd(cu, rate)
                        tu = cu.get("token_usage")
                        if isinstance(tu, dict):
                            pcc = tu.get("per_call_costs")
                            if isinstance(pcc, list) and pcc:
                                adj = Decimal("0")
                                has_per_call_flag = any(
                                    isinstance(c, dict) and c.get("pricing_failed") is True for c in pcc
                                )
                                failed_models = tu.get("pricing_failed_models")
                                for c in pcc:
                                    if not isinstance(c, dict):
                                        continue
                                    is_pf = c.get("pricing_failed") is True or (
                                        tu.get("pricing_failed") is True
                                        and not has_per_call_flag
                                        and str(c.get("billing_type") or "platform") != "oauth"
                                        and Decimal(str(c.get("cost") or 0)) <= Decimal("0")
                                        and (
                                            not (isinstance(failed_models, list) and failed_models)
                                            or str(c.get("model_name") or "") in failed_models
                                        )
                                    )
                                    if is_pf:
                                        c_cost = max(Decimal(str(c.get("cost") or 0)), Decimal("0"))
                                        adj += max(Decimal(str(floor_usd)) - c_cost, Decimal("0"))
                                total += base + adj
                            elif tu.get("pricing_failed") is True:
                                total += max(Decimal(str(floor_usd)), base)
                            else:
                                total += base
                        else:
                            total += base
                    self._one = {"unreserved_usd": total}
                    return

                if "WITH run_usage AS" in sql:
                    rate, user_id = params
                    for r in db.reservations.values():
                        if r["user_id"] != user_id or r["state"] not in ("open", "released"):
                            continue
                        cr = db.responses.get(r["run_ref"])
                        if not cr or cr.get("usage_settled_at") is None:
                            continue
                        run_usages = [
                            u for u in db.usages if u["conversation_response_id"] == r["run_ref"]
                        ]
                        proven_zero = db._proven_no_model_calls(cr)
                        if not run_usages and not proven_zero:
                            continue
                        children = [
                            sr for sr in db.subagent_runs if sr["parent_run_id"] == r["run_ref"]
                        ]
                        if any(
                            sr["usage_settled_at"] is None or sr["status"] == "in_progress"
                            for sr in children
                        ):
                            continue
                        usage_usd = sum(
                            (db._dalton_cash_usd(u, rate) for u in run_usages),
                            Decimal("0"),
                        )
                        all_complete = (
                            all(db._row_pricing_complete(u) for u in run_usages)
                            and (db._has_ptc_turn_usage(r["run_ref"]) or proven_zero)
                            and db._all_direct_subagents_have_usage(r["run_ref"])
                        )
                        r["state"] = "settled"
                        r["settled_at"] = db.now
                        if all_complete:
                            r["settled_usd"] = usage_usd
                            r["settle_source"] = "usage"
                        else:
                            r["settled_usd"] = max(r["reserved_usd"], r["observed_usd"], usage_usd)
                            r["settle_source"] = "conservative"
                    return

                if "WITH abandoned_usage AS" in sql:
                    rate, user_id = params
                    for r in db.reservations.values():
                        if r["user_id"] != user_id or r["state"] not in ("open", "released"):
                            continue
                        cr = db.responses.get(r["run_ref"])
                        if (
                            not cr
                            or cr.get("usage_settled_at") is None
                            or not db._parent_settled_grace_passed(cr, grace_cutoff)
                        ):
                            continue
                        direct_children = [
                            sr for sr in db.subagent_runs if sr["parent_run_id"] == r["run_ref"]
                        ]
                        if any(sr["status"] == "in_progress" for sr in direct_children):
                            continue
                        unsettled = [sr for sr in direct_children if sr["usage_settled_at"] is None]
                        if not any(
                            sr["finalized_at"] is not None and sr["finalized_at"] < grace_cutoff
                            for sr in unsettled
                        ):
                            continue
                        if any(
                            sr["finalized_at"] is None or sr["finalized_at"] >= grace_cutoff
                            for sr in unsettled
                        ):
                            continue

                        run_usages = [
                            u for u in db.usages if u["conversation_response_id"] == r["run_ref"]
                        ]
                        usage_usd = sum(
                            (db._dalton_cash_usd(u, rate) for u in run_usages),
                            Decimal("0"),
                        )
                        usage_rows = len(run_usages)
                        proven_zero = db._proven_no_model_calls(cr)
                        has_ptc_usage = db._has_ptc_turn_usage(r["run_ref"])
                        all_byok_have_per_call_costs = all(
                            db._row_pricing_complete(u) for u in run_usages
                        )
                        all_direct_subagents_settled_with_usage = True
                        for sr in direct_children:
                            if db._is_finished_workflow_driver(sr):
                                continue
                            if sr["usage_settled_at"] is None or not db._direct_subagent_chain_has_usage(
                                r["run_ref"], sr
                            ):
                                all_direct_subagents_settled_with_usage = False
                                break
                        all_workflow_children_accounted = True
                        for drv in direct_children:
                            if not db._is_finished_workflow_driver(drv):
                                continue
                            for child in db.subagent_runs:
                                facts = child.get("replay_facts")
                                if (
                                    child.get("thread_id") == drv.get("thread_id")
                                    and child.get("parent_run_id") is None
                                    and isinstance(facts, dict)
                                    and facts.get("workflow_driver_run_id") == drv["task_run_id"]
                                ):
                                    if child["status"] == "in_progress" or not db._has_task_run_usage(
                                        r["run_ref"], child["task_run_id"]
                                    ):
                                        all_workflow_children_accounted = False
                                        break

                        r["state"] = "settled"
                        r["settled_at"] = db.now
                        complete = (
                            (usage_rows > 0 or proven_zero)
                            and (has_ptc_usage or proven_zero)
                            and all_byok_have_per_call_costs
                            and all_direct_subagents_settled_with_usage
                            and all_workflow_children_accounted
                        )
                        if complete:
                            r["settled_usd"] = max(r["observed_usd"], usage_usd)
                            r["settle_source"] = (
                                "usage" if usage_usd >= r["observed_usd"] else "conservative"
                            )
                        else:
                            r["settled_usd"] = max(r["reserved_usd"], r["observed_usd"], usage_usd)
                            r["settle_source"] = "conservative"
                    return

            async def fetchone(self) -> dict[str, Any] | None:
                return self._one

            async def fetchall(self) -> list[dict[str, Any]]:
                return list(self._many)

        class _Conn:
            prepare_threshold = None
            info = SimpleNamespace(transaction_status=psycopg.pq.TransactionStatus.INTRANS)

            @asynccontextmanager
            async def transaction(self):
                yield

            def cursor(self, row_factory: Any = None) -> _Cursor:
                return _Cursor()

        return _Conn()


# ===========================================================================
# Gap 1: dalton_complete custom model pricing resolution & pricing failure
# ===========================================================================


@pytest.mark.asyncio
async def test_gap1_dalton_complete_custom_model_with_pricing_resolves_model_id_and_bills_cash(
    monkeypatch,
):
    """Custom model display name (`fake-gpt-6-luna`) resolves via chat path's
    `user_models.classify_model` to `(model_id='gpt-6.1-terra', provider='openai')`,
    finds real pricing, and writes non-zero cash_usd into `dalton_llm_journal`."""
    store = _FakeJournalDB()

    @asynccontextmanager
    async def _fake_db_conn():
        yield store.conn()

    monkeypatch.setattr(journal_db, "get_db_connection", _fake_db_conn)
    monkeypatch.setattr(
        complete_mod,
        "_check_cash_budget_admission",
        create_autospec(
            complete_mod._check_cash_budget_admission,
            return_value=(True, None, None),
        ),
    )

    classify_mock = create_autospec(
        user_models.classify_model,
        return_value=(
            user_models.ModelSource.CUSTOM,
            {
                "name": "fake-gpt-6-luna",
                "model_id": "gpt-6.1-terra",
                "provider": "openai",
            },
        ),
    )
    cp_mock = create_autospec(user_models.get_custom_provider_config, return_value=None)
    monkeypatch.setattr(user_models, "classify_model", classify_mock)
    monkeypatch.setattr(user_models, "get_custom_provider_config", cp_mock)

    usage_payload = {"input_tokens": 20_000, "output_tokens": 5_000, "total_tokens": 25_000}
    expected_cash, _, det_failed, det_models = compute_call_costs_detailed(
        "fake-gpt-6-luna",
        "byok",
        usage_payload,
        pricing_model_id="gpt-6.1-terra",
        pricing_provider="openai",
    )
    assert expected_cash > Decimal("0")
    assert det_failed is False
    assert det_models == []

    llm_svc = create_autospec(LLMService, instance=True)
    llm_svc.complete.return_value = ("ok-answer", usage_payload)

    res = await dalton_complete(
        llm_svc,
        user_id="user-gap1",
        user_prompt="Summarize Q3 margins",
        model="fake-gpt-6-luna",
        purpose="gap1_test",
    )

    assert res.ok is True
    assert res.provider == "openai"
    assert res.billing_type == "byok"
    assert Decimal(str(res.cash_usd)).quantize(Decimal("0.000001")) == expected_cash
    assert "pricing_failed" not in res.usage
    assert res.usage["pricing_model_id"] == "gpt-6.1-terra"
    assert res.usage["pricing_provider"] == "openai"

    row = store.rows[res.invocation_id]
    assert row["provider"] == "openai"
    assert row["billing_type"] == "byok"
    assert row["cash_usd"] == expected_cash
    assert "pricing_failed" not in (row["usage"] or {})

    attempt = store.attempts[(res.invocation_id, 1, 1)]
    assert attempt["cash_usd"] == expected_cash
    assert attempt["provider"] == "openai"
    assert attempt["billing_type"] == "byok"


@pytest.mark.asyncio
async def test_gap1_dalton_complete_unpriced_custom_model_marks_pricing_failed_and_keeps_floor(
    monkeypatch,
):
    """When a custom model resolves to a model_id that still has no pricing card,
    the journal row is flagged `pricing_failed=True` with the model listed and retains
    the conservative floor instead of recording cash_usd=0."""
    store = _FakeJournalDB()

    @asynccontextmanager
    async def _fake_db_conn():
        yield store.conn()

    monkeypatch.setattr(journal_db, "get_db_connection", _fake_db_conn)
    monkeypatch.setattr(
        complete_mod,
        "_check_cash_budget_admission",
        create_autospec(
            complete_mod._check_cash_budget_admission,
            return_value=(True, None, None),
        ),
    )

    classify_mock = create_autospec(
        user_models.classify_model,
        return_value=(
            user_models.ModelSource.CUSTOM,
            {
                "name": "fake-unknown-luna",
                "model_id": "nonexistent-model-v999",
                "provider": "openai",
            },
        ),
    )
    cp_mock = create_autospec(user_models.get_custom_provider_config, return_value=None)
    monkeypatch.setattr(user_models, "classify_model", classify_mock)
    monkeypatch.setattr(user_models, "get_custom_provider_config", cp_mock)

    usage_payload = {"input_tokens": 12_000, "output_tokens": 3_000, "total_tokens": 15_000}
    llm_svc = create_autospec(LLMService, instance=True)
    llm_svc.complete.return_value = ("ok-answer", usage_payload)

    res = await dalton_complete(
        llm_svc,
        user_id="user-gap1",
        user_prompt="Summarize Q3 margins",
        model="fake-unknown-luna",
        purpose="gap1_unpriced",
    )

    expected_floor = complete_mod._conservative_pricing_floor_usd()
    assert expected_floor > Decimal("0")
    assert res.ok is True
    assert Decimal(str(res.cash_usd)).quantize(Decimal("0.000001")) == expected_floor
    assert res.usage["pricing_failed"] is True
    assert res.usage["pricing_failed_models"] == ["fake-unknown-luna"]
    assert res.usage["pricing_model_id"] == "nonexistent-model-v999"

    row = store.rows[res.invocation_id]
    assert row["cash_usd"] == expected_floor
    assert row["usage"]["pricing_failed"] is True
    assert row["usage"]["pricing_failed_models"] == ["fake-unknown-luna"]

    attempt = store.attempts[(res.invocation_id, 1, 1)]
    assert attempt["cash_usd"] == expected_floor
    assert attempt["usage"]["pricing_failed"] is True

    # Journal cash aggregation and provider breakdown both see the floor, never $0.
    day_start = store.now - timedelta(hours=1)
    day_end = store.now + timedelta(hours=1)
    journal_cash = await budget_db.fetch_journal_cash_today(
        store.conn(), day_start, day_end, user_id="user-gap1"
    )
    assert journal_cash == expected_floor

    j_rows = await journal_db.fetch_journal_rows_in_window(
        day_start, day_end, user_id="user-gap1", conn=store.conn()
    )
    breakdown = budget_svc.compute_provider_breakdown([], j_rows)
    assert breakdown["openai"]["cash_usd"] == pytest.approx(float(expected_floor))
    assert breakdown["openai"]["pricing_failed"] is True
    assert breakdown["openai"]["pricing_failed_calls"] == 1
    assert breakdown["openai"]["pricing_failed_models"] == ["fake-unknown-luna"]


@pytest.mark.asyncio
async def test_gap1_dalton_complete_mixed_priced_unpriced_and_oauth_custom_models(
    monkeypatch,
):
    """Mixed day with: (1) priced custom model via client metadata in make_api_call,
    (2) unpriced custom model, and (3) OAuth subscription custom model."""
    store = _FakeJournalDB()

    @asynccontextmanager
    async def _fake_db_conn():
        yield store.conn()

    monkeypatch.setattr(journal_db, "get_db_connection", _fake_db_conn)
    monkeypatch.setattr(
        complete_mod,
        "_check_cash_budget_admission",
        create_autospec(
            complete_mod._check_cash_budget_admission,
            return_value=(True, None, None),
        ),
    )

    custom_catalog = {
        "fake-gpt-6-luna": {
            "name": "fake-gpt-6-luna",
            "model_id": "gpt-6.1-terra",
            "provider": "openai",
        },
        "fake-mystery-byok": {
            "name": "fake-mystery-byok",
            "model_id": "totally-unpriced-xyz",
            "provider": "openai",
        },
        "fake-codex-sub": {
            "name": "fake-codex-sub",
            "model_id": "gpt-6.1-sol-oauth",
            "provider": "codex-oauth",
        },
    }

    async def _classify(_uid: str, name: str, **_kw: Any):
        if name in custom_catalog:
            return user_models.ModelSource.CUSTOM, custom_catalog[name]
        return user_models.ModelSource.UNKNOWN, {}

    monkeypatch.setattr(
        user_models,
        "classify_model",
        create_autospec(user_models.classify_model, side_effect=_classify),
    )
    monkeypatch.setattr(
        user_models,
        "get_custom_provider_config",
        create_autospec(user_models.get_custom_provider_config, return_value=None),
    )

    class _StampedClient:
        def __init__(self, metadata: dict[str, Any], in_tok: int, out_tok: int) -> None:
            self.metadata = metadata
            self._in_tok = in_tok
            self._out_tok = out_tok

        async def ainvoke(self, _messages: Any) -> AIMessage:
            return AIMessage(
                content="done",
                usage_metadata={
                    "input_tokens": self._in_tok,
                    "output_tokens": self._out_tok,
                    "total_tokens": self._in_tok + self._out_tok,
                },
            )

    async def _complete_via_make_api_call(*, request_model: str, **_kw: Any):
        entry = custom_catalog[request_model]
        btype = "oauth" if entry["provider"] == "codex-oauth" else "byok"
        client = _StampedClient(
            {
                "billing_type": btype,
                "manifest_model": request_model,
                "pricing_model_id": entry["model_id"],
                "pricing_provider": entry["provider"],
            },
            in_tok=10_000,
            out_tok=2_000,
        )
        return await make_api_call(
            client,
            system_prompt="",
            user_prompt="hi",
            return_token_usage=True,
            disable_tracing=True,
        )

    llm_svc = create_autospec(LLMService, instance=True)
    llm_svc.complete.side_effect = _complete_via_make_api_call

    r_priced = await dalton_complete(
        llm_svc, user_id="u-mix", user_prompt="p1", model="fake-gpt-6-luna", purpose="mix"
    )
    r_unpriced = await dalton_complete(
        llm_svc, user_id="u-mix", user_prompt="p2", model="fake-mystery-byok", purpose="mix"
    )
    r_oauth = await dalton_complete(
        llm_svc, user_id="u-mix", user_prompt="p3", model="fake-codex-sub", purpose="mix"
    )

    u_10k = {"input_tokens": 10_000, "output_tokens": 2_000, "total_tokens": 12_000}
    expected_priced, _ = compute_call_costs("gpt-6.1-terra", "byok", u_10k)
    expected_floor = complete_mod._conservative_pricing_floor_usd()

    assert Decimal(str(r_priced.cash_usd)).quantize(Decimal("0.000001")) == expected_priced
    assert Decimal(str(r_unpriced.cash_usd)).quantize(Decimal("0.000001")) == expected_floor
    assert r_unpriced.usage["pricing_failed"] is True
    assert r_oauth.billing_type == "oauth"
    assert r_oauth.cash_usd == 0.0
    assert "pricing_failed" not in r_oauth.usage

    day_start = store.now - timedelta(hours=1)
    day_end = store.now + timedelta(hours=1)
    total_journal_cash = await budget_db.fetch_journal_cash_today(
        store.conn(), day_start, day_end, user_id="u-mix"
    )
    assert total_journal_cash == expected_priced + expected_floor

    j_rows = await journal_db.fetch_journal_rows_in_window(
        day_start, day_end, user_id="u-mix", conn=store.conn()
    )
    bd = budget_svc.compute_provider_breakdown([], j_rows)
    assert bd["openai"]["cash_usd"] == pytest.approx(float(expected_priced + expected_floor))
    assert bd["openai"]["calls"] == 2
    assert bd["openai"]["pricing_failed"] is True
    assert bd["openai"]["pricing_failed_calls"] == 1
    assert bd["openai"]["pricing_failed_models"] == ["fake-mystery-byok"]
    assert bd["codex-oauth"]["cash_usd"] == 0.0
    assert "pricing_failed" not in bd["codex-oauth"]


# ===========================================================================
# Gap 2: BYOK unpriced call -> pricing_failed + service.py + database/budget.py
# ===========================================================================


def _make_call_record(
    model_name: str,
    *,
    billing_type: str = "byok",
    pricing_model_id: str | None = None,
    pricing_provider: str | None = None,
    input_tokens: int = 10_000,
    output_tokens: int = 2_000,
) -> dict[str, Any]:
    return {
        "model_name": model_name,
        "served_model": pricing_model_id or model_name,
        "pricing_model_id": pricing_model_id,
        "pricing_provider": pricing_provider,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "billing_type": billing_type,
        "started_at": datetime.now(UTC).isoformat(),
        "timestamp": datetime.now(UTC).isoformat(),
        "run_id": str(uuid.uuid4()),
        "parent_run_id": None,
    }


@pytest.mark.asyncio
async def test_gap2_byok_priced_call_settles_by_actual_usage():
    """BYOK call with a valid pricing card has no pricing_failed flag, aggregates exact
    cash_usd, and settles by actual usage in Step 1 (normal turn)."""
    rec = _make_call_record(
        "fake-gpt-6-luna",
        billing_type="byok",
        pricing_model_id="gpt-6.1-terra",
        pricing_provider="openai",
    )
    tu = calculate_cost_from_per_call_records([rec])
    assert tu["total_cost"] > 0
    assert "pricing_failed" not in tu
    assert "pricing_failed_models" not in tu
    assert "pricing_failed" not in tu["per_call_costs"][0]

    tu_rows = [{"token_usage": tu, "is_byok": True}]
    assert budget_svc.compute_subscription_notional_usd(tu_rows) == Decimal("0.000000")
    bd = budget_svc.compute_provider_breakdown(tu_rows, [])
    assert bd["openai"]["cash_usd"] == pytest.approx(tu["total_cost"], rel=1e-5)
    assert "pricing_failed" not in bd["openai"]

    db = _FakeBudgetAndLifecycleDB()
    run_id = "run-byok-priced"
    db.responses[run_id] = {"status": "completed", "usage_settled_at": db.now}
    db.reservations[run_id] = {
        "reservation_id": "res-1",
        "user_id": "u-1",
        "run_ref": run_id,
        "state": "released",
        "reserved_usd": Decimal("1.00"),
        "observed_usd": Decimal("0"),
        "settled_usd": None,
        "settle_source": None,
    }
    db.usages.append(
        {
            "conversation_usage_id": str(uuid.uuid4()),
            "conversation_response_id": run_id,
            "msg_type": "ptc",
            "is_byok": True,
            "token_usage": tu,
            "infrastructure_credits": Decimal("0"),
            "total_credits": Decimal("0"),
        }
    )

    await budget_db.reconcile_reservations_sql(db.make_conn(), "u-1", Decimal("1000"))
    r = db.reservations[run_id]
    assert r["state"] == "settled"
    assert r["settle_source"] == "usage"
    assert r["settled_usd"] == pytest.approx(Decimal(str(tu["total_cost"])), rel=Decimal("1e-5"))


@pytest.mark.asyncio
async def test_gap2_byok_unpriced_call_marks_pricing_failed_never_zero_and_keeps_reserved_floor():
    """BYOK call with no pricing card sets `pricing_failed=True` + `pricing_failed_models`,
    is never counted as $0 cash in `budget/service.py`, and keeps the `reserved_usd` floor
    in BOTH Step 1 (normal turn) and Step 2 (workflow turn) of `reconcile_reservations_sql`."""
    rec = _make_call_record(
        "fake-byok-mystery",
        billing_type="byok",
        pricing_model_id="no-such-model-999",
        pricing_provider="openai",
    )
    tu = calculate_cost_from_per_call_records([rec])
    assert tu["pricing_failed"] is True
    assert tu["pricing_failed_models"] == ["fake-byok-mystery"]
    assert tu["per_call_costs"][0]["pricing_failed"] is True

    tu_rows = [{"token_usage": tu, "is_byok": True}]
    assert budget_svc.compute_subscription_notional_usd(tu_rows) == Decimal("0.000000")
    bd = budget_svc.compute_provider_breakdown(tu_rows, [])
    expected_floor = budget_svc._conservative_pricing_floor_usd()
    assert bd["openai"]["cash_usd"] == pytest.approx(float(expected_floor))
    assert bd["openai"]["cash_usd"] > 0.0
    assert bd["openai"]["pricing_failed"] is True
    assert bd["openai"]["pricing_failed_calls"] == 1
    assert bd["openai"]["unknown_usage_calls"] == 1
    assert bd["openai"]["pricing_failed_models"] == ["fake-byok-mystery"]

    # Step 1 (normal turn) and Step 2 (workflow turn) both keep reserved_usd floor.
    db = _FakeBudgetAndLifecycleDB()
    old = db.now - timedelta(minutes=45)

    # Normal turn (Step 1)
    run_normal = "run-byok-unpriced-normal"
    db.responses[run_normal] = {"status": "completed", "usage_settled_at": db.now}
    db.reservations[run_normal] = {
        "reservation_id": "res-normal",
        "user_id": "u-1",
        "run_ref": run_normal,
        "state": "released",
        "reserved_usd": Decimal("1.00"),
        "observed_usd": Decimal("0"),
        "settled_usd": None,
        "settle_source": None,
    }
    db.usages.append(
        {
            "conversation_usage_id": str(uuid.uuid4()),
            "conversation_response_id": run_normal,
            "msg_type": "ptc",
            "is_byok": True,
            "token_usage": tu,
            "infrastructure_credits": Decimal("0"),
            "total_credits": Decimal("0"),
        }
    )

    # Workflow turn (Step 2) with finished workflow driver + child carrying pricing_failed
    run_wf = "run-byok-unpriced-wf"
    drv_id = str(uuid.uuid4())
    child_id = str(uuid.uuid4())
    db.responses[run_wf] = {"status": "completed", "usage_settled_at": old}
    db.reservations[run_wf] = {
        "reservation_id": "res-wf",
        "user_id": "u-1",
        "run_ref": run_wf,
        "state": "released",
        "reserved_usd": Decimal("1.00"),
        "observed_usd": Decimal("0"),
        "settled_usd": None,
        "settle_source": None,
    }
    db.subagent_runs.extend(
        [
            {
                "task_run_id": drv_id,
                "thread_id": "th-wf",
                "parent_run_id": run_wf,
                "status": "completed",
                "cause": "init",
                "replay_facts": {"workflow_driver": True},
                "finalized_at": old,
                "usage_settled_at": None,
            },
            {
                "task_run_id": child_id,
                "thread_id": "th-wf",
                "parent_run_id": None,
                "status": "completed",
                "cause": "init",
                "replay_facts": {"workflow_driver_run_id": drv_id},
                "finalized_at": old,
                "usage_settled_at": old,
            },
        ]
    )
    db.usages.extend(
        [
            {
                "conversation_usage_id": str(uuid.uuid4()),
                "conversation_response_id": run_wf,
                "msg_type": "ptc",
                "is_byok": True,
                "token_usage": {
                    "per_call_costs": [
                        {"model_name": "claude-opus-5-5-oauth", "billing_type": "oauth", "cost": 0.0}
                    ]
                },
                "infrastructure_credits": Decimal("0"),
                "total_credits": Decimal("0"),
            },
            {
                "conversation_usage_id": str(uuid.uuid4()),
                "conversation_response_id": run_wf,
                "msg_type": "task",
                "is_byok": True,
                "token_usage": {**tu, "task_run_id": child_id},
                "infrastructure_credits": Decimal("0"),
                "total_credits": Decimal("0"),
            },
        ]
    )

    await budget_db.reconcile_reservations_sql(db.make_conn(), "u-1", Decimal("1000"))

    assert db.reservations[run_normal]["state"] == "settled"
    assert db.reservations[run_normal]["settled_usd"] == Decimal("1.00")
    assert db.reservations[run_normal]["settle_source"] == "conservative"

    assert db.reservations[run_wf]["state"] == "settled"
    assert db.reservations[run_wf]["settled_usd"] == Decimal("1.00")
    assert db.reservations[run_wf]["settle_source"] == "conservative"

    # Confirm the actual SQL emitted checks pricing_failed in both Step 1 and Step 2.
    step1_sql = next(s for s in db.executed_sql if "WITH run_usage AS" in s)
    step2_sql = next(s for s in db.executed_sql if "WITH abandoned_usage AS" in s)
    assert '{"pricing_failed": true}' in step1_sql
    assert '{"pricing_failed": true}' in step2_sql
    assert "all_byok_have_per_call_costs" in step2_sql


@pytest.mark.asyncio
async def test_gap2_mixed_priced_and_unpriced_byok_calls_in_same_turn():
    """Mixed turn with 1 priced BYOK call + 1 unpriced BYOK call lists only the unpriced
    model, aggregates priced cost + conservative floor in breakdown, and retains the
    greater of reserved_usd or actual usage_usd during settlement."""
    priced_rec = _make_call_record(
        "gpt-6.1-terra",
        billing_type="byok",
        pricing_model_id="gpt-6.1-terra",
        pricing_provider="openai",
        input_tokens=10_000,
        output_tokens=2_000,
    )
    unpriced_rec = _make_call_record(
        "fake-mystery-byok",
        billing_type="byok",
        pricing_model_id="no-such-model-888",
        pricing_provider="openai",
        input_tokens=5_000,
        output_tokens=1_000,
    )
    tu = calculate_cost_from_per_call_records([priced_rec, unpriced_rec])
    priced_cost = tu["per_call_costs"][0]["cost"]
    assert priced_cost > 0
    assert "pricing_failed" not in tu["per_call_costs"][0]
    assert tu["per_call_costs"][1]["pricing_failed"] is True
    assert tu["pricing_failed"] is True
    assert tu["pricing_failed_models"] == ["fake-mystery-byok"]

    bd = budget_svc.compute_provider_breakdown([{"token_usage": tu, "is_byok": True}], [])
    expected_floor = float(budget_svc._conservative_pricing_floor_usd())
    assert bd["openai"]["calls"] == 2
    assert bd["openai"]["cash_usd"] == pytest.approx(priced_cost + expected_floor, rel=1e-5)
    assert bd["openai"]["pricing_failed"] is True
    assert bd["openai"]["pricing_failed_calls"] == 1
    assert bd["openai"]["pricing_failed_models"] == ["fake-mystery-byok"]

    # Case A: priced_cost < reserved_usd ($1.00) -> keeps $1.00 conservative floor.
    # Case B: priced_cost > reserved_usd ($0.001) -> charges actual higher priced_cost.
    db = _FakeBudgetAndLifecycleDB()
    for run_id, reserved in (("run-mix-low", Decimal("1.00")), ("run-mix-high", Decimal("0.001"))):
        db.responses[run_id] = {"status": "completed", "usage_settled_at": db.now}
        db.reservations[run_id] = {
            "reservation_id": f"res-{run_id}",
            "user_id": "u-1",
            "run_ref": run_id,
            "state": "released",
            "reserved_usd": reserved,
            "observed_usd": Decimal("0"),
            "settled_usd": None,
            "settle_source": None,
        }
        db.usages.append(
            {
                "conversation_usage_id": str(uuid.uuid4()),
                "conversation_response_id": run_id,
                "msg_type": "ptc",
                "is_byok": True,
                "token_usage": tu,
                "infrastructure_credits": Decimal("0"),
                "total_credits": Decimal("0"),
            }
        )

    await budget_db.reconcile_reservations_sql(db.make_conn(), "u-1", Decimal("1000"))

    assert db.reservations["run-mix-low"]["settled_usd"] == Decimal("1.00")
    assert db.reservations["run-mix-low"]["settle_source"] == "conservative"
    assert db.reservations["run-mix-high"]["settled_usd"] == pytest.approx(
        Decimal(str(priced_cost)), rel=Decimal("1e-5")
    )
    assert db.reservations["run-mix-high"]["settle_source"] == "conservative"


# ===========================================================================
# Gap 3: usage_settled_at guarded against missing parent usage row (stream_usage=False)
# ===========================================================================


def _patch_finalize_side_effects(monkeypatch) -> None:
    """Stub non-billing lifecycle helpers inside `finalize_run` with autospecs."""
    from src.server.database import conversation as qr_db

    monkeypatch.setattr(
        qr_db,
        "update_thread_status",
        create_autospec(qr_db.update_thread_status, return_value=True),
    )
    monkeypatch.setattr(
        qr_db,
        "_sync_provenance_for_response",
        create_autospec(qr_db._sync_provenance_for_response, return_value=None),
    )
    monkeypatch.setattr(
        lifecycle_db,
        "build_finalize_jobs_from_run_row",
        create_autospec(
            lifecycle_db.build_finalize_jobs_from_run_row,
            return_value=lambda _status: [],
        ),
    )
    monkeypatch.setattr(
        lifecycle_db,
        "release_deferred_notifications",
        create_autospec(lifecycle_db.release_deferred_notifications, return_value=0),
    )
    monkeypatch.setattr(
        lifecycle_db,
        "settle_run_steers",
        create_autospec(lifecycle_db.settle_run_steers, return_value=0),
    )


def _seed_in_progress_run(db: _FakeBudgetAndLifecycleDB, run_id: str, thread_id: str) -> None:
    db.responses[run_id] = {
        "conversation_response_id": run_id,
        "conversation_thread_id": thread_id,
        "turn_index": 0,
        "run_seq": 1,
        "status": "in_progress",
        "interrupt_reason": None,
        "metadata": {"workspace_id": "ws-1", "user_id": "u-1"},
        "created_at": db.now,
        "usage_settled_at": None,
        "cancel_requested_at": None,
    }


@pytest.mark.asyncio
async def test_gap3_normal_turn_with_usage_row_stamps_usage_settled_at(monkeypatch):
    """Normal turn (stream_usage=True): tracker records usage, usage_writer inserts a `ptc`
    usage row, finalize_run stamps `usage_settled_at`, and budget settles by actual usage."""
    _patch_finalize_side_effects(monkeypatch)
    db = _FakeBudgetAndLifecycleDB()
    run_id = "run-gap3-priced"
    thread_id = "th-gap3-priced"
    _seed_in_progress_run(db, run_id, thread_id)

    tracker = PerCallTokenTracker()
    llm_run_id = uuid.uuid4()
    tracker.on_chat_model_start(
        {},
        [],
        run_id=llm_run_id,
        metadata={
            "billing_type": "byok",
            "manifest_model": "gpt-6.1-terra",
            "pricing_model_id": "gpt-6.1-terra",
            "pricing_provider": "openai",
        },
    )
    tracker.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content="hello",
                            usage_metadata={"input_tokens": 5000, "output_tokens": 1000, "total_tokens": 6000},
                        )
                    )
                ]
            ]
        ),
        run_id=llm_run_id,
    )
    assert tracker.llm_calls_started() == 1
    records = tracker.get_per_call_records()
    assert len(records) == 1

    with patch.object(
        finalization,
        "_delivered_steering_inputs",
        create_autospec(finalization._delivered_steering_inputs, return_value=[]),
    ):
        outcome = await finalization.assemble_finalize_artifacts(
            (thread_id, run_id),
            metadata={"token_callback": tracker, "is_byok": True},
            status="completed",
            phase="completion",
            interrupt_reason=None,
            pause_message=None,
            error=None,
            handler=None,
            cancelled_by_user=False,
            workspace_id="ws-1",
            recorded_as="u-1",
        )
    assert outcome.no_model_calls is False
    assert outcome.per_call_records == records

    tu = calculate_cost_from_per_call_records(records)

    async def _usage_writer(_conn: Any, _status: str) -> None:
        db.usages.append(
            {
                "conversation_usage_id": str(uuid.uuid4()),
                "conversation_response_id": run_id,
                "msg_type": "ptc",
                "is_byok": True,
                "token_usage": tu,
                "infrastructure_credits": Decimal("0"),
                "total_credits": Decimal("0"),
            }
        )

    res = await finalize_run(
        run_id=run_id,
        thread_id=thread_id,
        outcome=outcome,
        usage_writer=_usage_writer,
        conn=db.make_conn(),
    )
    assert res.applied is True
    assert res.run is not None
    assert res.run["usage_settled_at"] is not None
    assert db.responses[run_id]["usage_settled_at"] is not None

    db.reservations[run_id] = {
        "reservation_id": "res-gap3-ok",
        "user_id": "u-1",
        "run_ref": run_id,
        "state": "released",
        "reserved_usd": Decimal("1.00"),
        "observed_usd": Decimal("0"),
        "settled_usd": None,
        "settle_source": None,
    }
    await budget_db.reconcile_reservations_sql(db.make_conn(), "u-1", Decimal("1000"))
    assert db.reservations[run_id]["state"] == "settled"
    assert db.reservations[run_id]["settle_source"] == "usage"
    assert db.reservations[run_id]["settled_usd"] == pytest.approx(
        Decimal(str(tu["total_cost"])), rel=Decimal("1e-5")
    )


@pytest.mark.asyncio
async def test_gap3_paid_model_without_stream_usage_skips_stamp_warns_and_keeps_floor(
    monkeypatch, caplog
):
    """When a paid model disables `stream_usage`, the LLM call runs (`llm_calls_started()==1`)
    but `per_call_records` is empty and no `ptc` usage row is written. `finalize_run` must NOT
    stamp `usage_settled_at`, must emit a secret-free warning, and budget must keep the
    reservation floor even if a subagent `task` usage row exists on the same `run_ref`."""
    _patch_finalize_side_effects(monkeypatch)
    db = _FakeBudgetAndLifecycleDB()
    run_id = "run-gap3-no-stream-usage"
    thread_id = "th-gap3-no-stream-usage"
    _seed_in_progress_run(db, run_id, thread_id)

    # Even if a child subagent persisted a $0 OAuth task usage row on this parent run_ref,
    # it must NOT trick finalize_run into stamping the parent's usage_settled_at.
    db.usages.append(
        {
            "conversation_usage_id": str(uuid.uuid4()),
            "conversation_response_id": run_id,
            "msg_type": "task",
            "is_byok": True,
            "token_usage": {
                "task_run_id": str(uuid.uuid4()),
                "per_call_costs": [
                    {"model_name": "claude-opus-5-5-oauth", "billing_type": "oauth", "cost": 0.0}
                ],
            },
            "infrastructure_credits": Decimal("0"),
            "total_credits": Decimal("0"),
        }
    )

    tracker = PerCallTokenTracker()
    llm_run_id = uuid.uuid4()
    tracker.on_chat_model_start(
        {},
        [],
        run_id=llm_run_id,
        metadata={
            "billing_type": "byok",
            "manifest_model": "gpt-6.1-terra",
            "pricing_model_id": "gpt-6.1-terra",
            "pricing_provider": "openai",
        },
    )
    # Provider returned streamed response with NO usage_metadata (stream_usage=False).
    tracker.on_llm_end(
        LLMResult(generations=[[ChatGeneration(message=AIMessage(content="no usage chunk"))]]),
        run_id=llm_run_id,
    )
    assert tracker.llm_calls_started() == 1
    assert tracker.get_per_call_records() == []

    secret_token = "sk-live-secret-must-never-appear-in-logs"
    with patch.object(
        finalization,
        "_delivered_steering_inputs",
        create_autospec(finalization._delivered_steering_inputs, return_value=[]),
    ):
        outcome = await finalization.assemble_finalize_artifacts(
            (thread_id, run_id),
            metadata={"token_callback": tracker, "is_byok": True, "api_key": secret_token},
            status="completed",
            phase="completion",
            interrupt_reason=None,
            pause_message=None,
            error=None,
            handler=None,
            cancelled_by_user=False,
            workspace_id="ws-1",
            recorded_as="u-1",
        )
    assert outcome.no_model_calls is False
    assert outcome.per_call_records == []

    with caplog.at_level(logging.WARNING, logger="src.server.database.runs.lifecycle"):
        res = await finalize_run(
            run_id=run_id,
            thread_id=thread_id,
            outcome=outcome,
            usage_writer=None,
            conn=db.make_conn(),
        )

    assert res.applied is True
    assert res.run is not None
    assert res.run["usage_settled_at"] is None
    assert db.responses[run_id]["usage_settled_at"] is None

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(run_id in msg and "usage_settled_at" in msg for msg in warnings)
    assert all(secret_token not in msg for msg in warnings)

    # Budget reconciliation must NOT settle the reservation to $0 from the subagent's task row.
    db.reservations[run_id] = {
        "reservation_id": "res-gap3-missing",
        "user_id": "u-1",
        "run_ref": run_id,
        "state": "released",
        "reserved_usd": Decimal("1.00"),
        "observed_usd": Decimal("0"),
        "settled_usd": None,
        "settle_source": None,
    }
    await budget_db.reconcile_reservations_sql(db.make_conn(), "u-1", Decimal("1000"))
    assert db.reservations[run_id]["state"] == "released"
    assert db.reservations[run_id]["settled_usd"] is None


@pytest.mark.asyncio
async def test_gap3_mixed_zero_model_calls_vs_tool_only_unmetered_call(monkeypatch, caplog):
    """Mixed cases:
    (a) Proven zero model calls (`no_model_calls=True`, e.g., fail_open_run or pre-LLM abort)
        stamps `usage_settled_at` with no warning.
    (b) Paid model with `stream_usage=False` (`llm_calls_started()==1`, `per_call_records=[]`)
        plus an infra tool call (`tool_usage` non-empty, `ptc` row has `token_usage=None`)
        still withholds `usage_settled_at` and logs a warning."""
    _patch_finalize_side_effects(monkeypatch)
    db = _FakeBudgetAndLifecycleDB()

    # (a) Proven zero model calls -> stamps usage_settled_at without warning.
    run_zero = "run-gap3-zero-calls"
    th_zero = "th-gap3-zero-calls"
    _seed_in_progress_run(db, run_zero, th_zero)

    empty_tracker = PerCallTokenTracker()
    assert empty_tracker.llm_calls_started() == 0
    with patch.object(
        finalization,
        "_delivered_steering_inputs",
        create_autospec(finalization._delivered_steering_inputs, return_value=[]),
    ):
        outcome_zero = await finalization.assemble_finalize_artifacts(
            (th_zero, run_zero),
            metadata={"token_callback": empty_tracker, "is_byok": True},
            status="error",
            phase="error",
            interrupt_reason=None,
            pause_message=None,
            error="sandbox boot failed",
            handler=None,
            cancelled_by_user=False,
            workspace_id="ws-1",
            recorded_as="u-1",
        )
    assert outcome_zero.no_model_calls is True

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="src.server.database.runs.lifecycle"):
        res_zero = await finalize_run(
            run_id=run_zero,
            thread_id=th_zero,
            outcome=outcome_zero,
            usage_writer=None,
            conn=db.make_conn(),
        )
    assert res_zero.applied is True
    assert res_zero.run["usage_settled_at"] is not None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    # Also verify fail_open_run defaults conservatively to no_model_calls=False and honors explicit True.
    captured_outcomes: list[RunOutcome] = []

    async def _capture_finalize(_handle: RunHandle, outcome: RunOutcome, **_kw: Any):
        captured_outcomes.append(outcome)
        return lifecycle_db.FinalizeResult(applied=True, run={"status": "error"})

    coord = RunCoordinator()
    monkeypatch.setattr(
        coord,
        "finalize_run",
        create_autospec(coord.finalize_run, side_effect=_capture_finalize),
    )
    handle = RunHandle(run_id="run-fail-open", thread_id="th-fail-open", turn_index=0, attempt_no=1)
    await coord.fail_open_run(handle, "post-exec unknown failure")
    await coord.fail_open_run(handle, "pre-exec failure", no_model_calls=True)
    assert len(captured_outcomes) == 2
    assert captured_outcomes[0].no_model_calls is False
    assert captured_outcomes[1].no_model_calls is True

    # (b) Unmetered LLM call + tool-only usage row (`token_usage=None`) -> must NOT stamp.
    run_tool = "run-gap3-tool-only"
    th_tool = "th-gap3-tool-only"
    _seed_in_progress_run(db, run_tool, th_tool)

    async def _tool_only_writer(_conn: Any, _status: str) -> None:
        db.usages.append(
            {
                "conversation_usage_id": str(uuid.uuid4()),
                "conversation_response_id": run_tool,
                "msg_type": "ptc",
                "is_byok": True,
                "token_usage": None,
                "infrastructure_credits": Decimal("1"),
                "total_credits": Decimal("1"),
            }
        )

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="src.server.database.runs.lifecycle"):
        res_tool = await finalize_run(
            run_id=run_tool,
            thread_id=th_tool,
            outcome=RunOutcome(status="completed", tool_usage={"TavilySearchTool": 1}, no_model_calls=False),
            usage_writer=_tool_only_writer,
            conn=db.make_conn(),
        )
    assert res_tool.applied is True
    assert res_tool.run["usage_settled_at"] is None
    assert any(run_tool in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


# ===========================================================================
# Regression (#478): Workflow turns with complete, priced usage settle unchanged
# ===========================================================================


@pytest.mark.asyncio
async def test_regression_478_workflow_turn_with_complete_usage_settles_by_usage_unchanged():
    """#478 regression: A finished `RunWorkflow` driver (never gets `usage_settled_at`) whose
    workflow children and direct subagents all have complete, priced `task_run_id` usage rows
    still drops the conservative `$1.00` floor and settles at actual usage ($0 for OAuth,
    exact cash for cash subagent)."""
    db = _FakeBudgetAndLifecycleDB()
    old = db.now - timedelta(minutes=45)

    # 1. Pure OAuth workflow turn -> settles at $0.00 (not $1.00 conservative)
    run_oauth = "run-478-oauth"
    drv_oauth = str(uuid.uuid4())
    child_oauth = str(uuid.uuid4())
    db.responses[run_oauth] = {"status": "completed", "usage_settled_at": old}
    db.reservations[run_oauth] = {
        "reservation_id": "res-478-oauth",
        "user_id": "u-478",
        "run_ref": run_oauth,
        "state": "released",
        "reserved_usd": Decimal("1.00"),
        "observed_usd": Decimal("0"),
        "settled_usd": None,
        "settle_source": None,
    }
    db.subagent_runs.extend(
        [
            {
                "task_run_id": drv_oauth,
                "thread_id": "th-478-o",
                "parent_run_id": run_oauth,
                "status": "completed",
                "cause": "init",
                "replay_facts": {"workflow_driver": True},
                "finalized_at": old,
                "usage_settled_at": None,
            },
            {
                "task_run_id": child_oauth,
                "thread_id": "th-478-o",
                "parent_run_id": None,
                "status": "completed",
                "cause": "init",
                "replay_facts": {"workflow_driver_run_id": drv_oauth},
                "finalized_at": old,
                "usage_settled_at": old,
            },
        ]
    )
    db.usages.extend(
        [
            {
                "conversation_usage_id": str(uuid.uuid4()),
                "conversation_response_id": run_oauth,
                "msg_type": "ptc",
                "is_byok": True,
                "token_usage": {
                    "per_call_costs": [
                        {"model_name": "claude-opus-5-5-oauth", "billing_type": "oauth", "cost": 0.0}
                    ]
                },
                "infrastructure_credits": Decimal("0"),
                "total_credits": Decimal("0"),
            },
            {
                "conversation_usage_id": str(uuid.uuid4()),
                "conversation_response_id": run_oauth,
                "msg_type": "task",
                "is_byok": True,
                "token_usage": {
                    "task_run_id": child_oauth,
                    "per_call_costs": [
                        {"model_name": "claude-opus-5-5-oauth", "billing_type": "oauth", "cost": 0.0}
                    ],
                },
                "infrastructure_credits": Decimal("0"),
                "total_credits": Decimal("0"),
            },
        ]
    )

    # 2. Mixed workflow turn (OAuth parent + priced BYOK child + priced direct Task) -> exact cash
    run_cash = "run-478-cash"
    drv_cash = str(uuid.uuid4())
    child_cash = str(uuid.uuid4())
    direct_cash = str(uuid.uuid4())
    db.responses[run_cash] = {"status": "completed", "usage_settled_at": old}
    db.reservations[run_cash] = {
        "reservation_id": "res-478-cash",
        "user_id": "u-478",
        "run_ref": run_cash,
        "state": "released",
        "reserved_usd": Decimal("1.00"),
        "observed_usd": Decimal("0"),
        "settled_usd": None,
        "settle_source": None,
    }
    db.subagent_runs.extend(
        [
            {
                "task_run_id": drv_cash,
                "thread_id": "th-478-c",
                "parent_run_id": run_cash,
                "status": "completed",
                "cause": "init",
                "replay_facts": {"workflow_driver": True},
                "finalized_at": old,
                "usage_settled_at": None,
            },
            {
                "task_run_id": child_cash,
                "thread_id": "th-478-c",
                "parent_run_id": None,
                "status": "completed",
                "cause": "init",
                "replay_facts": {"workflow_driver_run_id": drv_cash},
                "finalized_at": old,
                "usage_settled_at": old,
            },
            {
                "task_run_id": direct_cash,
                "thread_id": "th-478-c",
                "parent_run_id": run_cash,
                "status": "completed",
                "cause": "init",
                "replay_facts": None,
                "finalized_at": old,
                "usage_settled_at": old,
            },
        ]
    )
    child_tu = calculate_cost_from_per_call_records(
        [
            _make_call_record(
                "gpt-6.1-terra",
                billing_type="byok",
                pricing_model_id="gpt-6.1-terra",
                pricing_provider="openai",
                input_tokens=10_000,
                output_tokens=2_000,
            )
        ]
    )
    direct_tu = calculate_cost_from_per_call_records(
        [
            _make_call_record(
                "gpt-6.1-terra",
                billing_type="byok",
                pricing_model_id="gpt-6.1-terra",
                pricing_provider="openai",
                input_tokens=5_000,
                output_tokens=1_000,
            )
        ]
    )
    expected_cash = Decimal(str(child_tu["total_cost"])) + Decimal(str(direct_tu["total_cost"]))
    assert Decimal("0") < expected_cash < Decimal("1.00")

    db.usages.extend(
        [
            {
                "conversation_usage_id": str(uuid.uuid4()),
                "conversation_response_id": run_cash,
                "msg_type": "ptc",
                "is_byok": True,
                "token_usage": {
                    "per_call_costs": [
                        {"model_name": "claude-opus-5-5-oauth", "billing_type": "oauth", "cost": 0.0}
                    ]
                },
                "infrastructure_credits": Decimal("0"),
                "total_credits": Decimal("0"),
            },
            {
                "conversation_usage_id": str(uuid.uuid4()),
                "conversation_response_id": run_cash,
                "msg_type": "task",
                "is_byok": True,
                "token_usage": {**child_tu, "task_run_id": child_cash},
                "infrastructure_credits": Decimal("0"),
                "total_credits": Decimal("0"),
            },
            {
                "conversation_usage_id": str(uuid.uuid4()),
                "conversation_response_id": run_cash,
                "msg_type": "task",
                "is_byok": True,
                "token_usage": {**direct_tu, "task_run_id": direct_cash},
                "infrastructure_credits": Decimal("0"),
                "total_credits": Decimal("0"),
            },
        ]
    )

    await budget_db.reconcile_reservations_sql(db.make_conn(), "u-478", Decimal("1000"))

    assert db.reservations[run_oauth]["state"] == "settled"
    assert db.reservations[run_oauth]["settled_usd"] == Decimal("0")
    assert db.reservations[run_oauth]["settle_source"] == "usage"

    assert db.reservations[run_cash]["state"] == "settled"
    assert db.reservations[run_cash]["settled_usd"] == pytest.approx(
        expected_cash, rel=Decimal("1e-5")
    )
    assert db.reservations[run_cash]["settle_source"] == "usage"


# ===========================================================================
# Round 2: Unreserved pricing_failed floor, bounded fallback settlement,
#          authoritative client identity, per-record make_api_call identity,
#          llm_calls_started dedup, interface compat, journal helper reuse
# ===========================================================================


@pytest.mark.asyncio
async def test_r2_fetch_unreserved_usage_today_pricing_failed_adds_floor_per_call_and_daily_gate_spent_usd(
    monkeypatch,
):
    """Item 1: Unreserved usage rows carrying `pricing_failed` add one conservative floor per
    pricing-failed call in `fetch_unreserved_usage_today`, matching `compute_provider_breakdown`
    and feeding the daily gate's `spent_usd`."""
    db = _FakeBudgetAndLifecycleDB()
    floor = budget_svc._conservative_pricing_floor_usd()
    day_start = db.now - timedelta(hours=1)
    day_end = db.now + timedelta(hours=1)

    priced_rec = _make_call_record(
        "gpt-6.1-terra",
        billing_type="byok",
        pricing_model_id="gpt-6.1-terra",
        pricing_provider="openai",
        input_tokens=10_000,
        output_tokens=2_000,
    )
    unpriced_rec_1 = _make_call_record(
        "fake-unreserved-mystery",
        billing_type="byok",
        pricing_model_id="no-such-unreserved-1",
        pricing_provider="openai",
    )
    unpriced_rec_2 = _make_call_record(
        "fake-unreserved-mystery",
        billing_type="byok",
        pricing_model_id="no-such-unreserved-2",
        pricing_provider="openai",
    )
    mixed_tu = calculate_cost_from_per_call_records([priced_rec, unpriced_rec_1, unpriced_rec_2])
    priced_cost = Decimal(str(mixed_tu["per_call_costs"][0]["cost"]))

    # Same model name: one priced call + one pricing_failed call -> only the failed call gets the floor.
    same_model_tu = {
        "pricing_failed": True,
        "pricing_failed_models": ["dual-route-model"],
        "per_call_costs": [
            {
                "model_name": "dual-route-model",
                "billing_type": "byok",
                "pricing_provider": "openai",
                "cost": 0.12,
                "input_tokens": 5_000,
                "output_tokens": 1_000,
            },
            {
                "model_name": "dual-route-model",
                "billing_type": "byok",
                "pricing_provider": "openai",
                "cost": 0.0,
                "pricing_failed": True,
                "input_tokens": 5_000,
                "output_tokens": 1_000,
            },
        ],
    }

    # Row-level pricing_failed without per_call_costs -> adds one floor.
    row_level_only_tu = {"pricing_failed": True, "by_model": {"legacy-unpriced": {"total_cost": 0.0}}}

    db.usages.extend(
        [
            {
                "conversation_usage_id": str(uuid.uuid4()),
                "conversation_response_id": "unresv-mixed",
                "user_id": "u-unresv",
                "msg_type": "ptc",
                "is_byok": True,
                "token_usage": mixed_tu,
                "infrastructure_credits": Decimal("0"),
                "total_credits": Decimal("0"),
                "created_at": db.now,
            },
            {
                "conversation_usage_id": str(uuid.uuid4()),
                "conversation_response_id": "unresv-same-model",
                "user_id": "u-unresv",
                "msg_type": "ptc",
                "is_byok": True,
                "token_usage": same_model_tu,
                "infrastructure_credits": Decimal("0"),
                "total_credits": Decimal("0"),
                "created_at": db.now,
            },
            {
                "conversation_usage_id": str(uuid.uuid4()),
                "conversation_response_id": "unresv-row-only",
                "user_id": "u-unresv",
                "msg_type": "ptc",
                "is_byok": True,
                "token_usage": row_level_only_tu,
                "infrastructure_credits": Decimal("0"),
                "total_credits": Decimal("0"),
                "created_at": db.now,
            },
        ]
    )

    unreserved_usd = await budget_db.fetch_unreserved_usage_today(
        db.make_conn(), "u-unresv", day_start, day_end, Decimal("1000")
    )
    expected_total = priced_cost + Decimal("0.12") + (floor * 4)
    assert unreserved_usd == pytest.approx(expected_total, rel=Decimal("1e-5"))

    # Provider breakdown agrees on the per-call rows (1 priced + 2 failed + 0.12 priced + 1 failed).
    bd = budget_svc.compute_provider_breakdown(
        [{"token_usage": mixed_tu, "is_byok": True}, {"token_usage": same_model_tu, "is_byok": True}],
        [],
    )
    assert bd["openai"]["pricing_failed_calls"] == 3
    assert bd["openai"]["cash_usd"] == pytest.approx(
        float(priced_cost + Decimal("0.12") + floor * 3), rel=1e-5
    )

    # Daily gate `reconcile_and_summarize_day` reflects the full unreserved floor in `spent_usd`.
    monkeypatch.setattr(
        budget_svc,
        "resolve_user_budget_config",
        create_autospec(
            budget_svc.resolve_user_budget_config,
            return_value={
                "user_id": "u-unresv",
                "daily_cap_usd": Decimal("10.00"),
                "default_task_cap_usd": None,
                "timezone": "UTC",
                "configured": True,
            },
        ),
    )
    monkeypatch.setattr(
        budget_svc,
        "acquire_budget_advisory_lock",
        create_autospec(budget_svc.acquire_budget_advisory_lock, return_value=None),
    )
    monkeypatch.setattr(
        budget_svc,
        "reconcile_reservations_sql",
        create_autospec(budget_svc.reconcile_reservations_sql, return_value=None),
    )
    monkeypatch.setattr(
        budget_svc,
        "fetch_day_reservations",
        create_autospec(budget_svc.fetch_day_reservations, return_value=[]),
    )
    monkeypatch.setattr(
        budget_svc,
        "fetch_journal_cash_today",
        create_autospec(budget_svc.fetch_journal_cash_today, return_value=Decimal("0")),
    )
    monkeypatch.setattr(
        budget_svc,
        "fetch_today_token_usages",
        create_autospec(
            budget_svc.fetch_today_token_usages,
            return_value=[{"token_usage": mixed_tu, "is_byok": True}],
        ),
    )
    monkeypatch.setattr(
        budget_svc,
        "fetch_journal_rows_in_window",
        create_autospec(budget_svc.fetch_journal_rows_in_window, return_value=[]),
    )

    summary = await budget_svc.reconcile_and_summarize_day(
        db.make_conn(), "u-unresv", now_utc=db.now
    )
    assert summary["_decimals"]["unreserved_usage_usd"] == pytest.approx(
        expected_total, rel=Decimal("1e-5")
    )
    assert summary["_decimals"]["spent_usd"] == pytest.approx(
        expected_total, rel=Decimal("1e-5")
    )


def _seed_in_progress_run_with_credits(
    db: _FakeBudgetAndLifecycleDB,
    *,
    run_id: str,
    thread_id: str,
    user_id: str,
    in_flight_credits: Decimal,
    reserved_usd: Decimal = Decimal("1.00"),
    resv_state: str = "released",
) -> None:
    db.responses[run_id] = {
        "conversation_response_id": run_id,
        "conversation_thread_id": thread_id,
        "turn_index": 0,
        "run_seq": 1,
        "status": "in_progress",
        "interrupt_reason": None,
        "metadata": {"workspace_id": "ws-1", "user_id": user_id},
        "created_at": db.now,
        "execution_time": None,
        "in_flight_credits": in_flight_credits,
        "user_id": user_id,
        "usage_settled_at": None,
        "cancel_requested_at": None,
    }
    db.reservations[run_id] = {
        "reservation_id": f"res-{run_id}",
        "user_id": user_id,
        "run_ref": run_id,
        "state": resv_state,
        "reserved_usd": reserved_usd,
        "observed_usd": Decimal("0"),
        "settled_usd": None,
        "settle_source": None,
    }


@pytest.mark.asyncio
async def test_r2_fallback_settle_crash_recovery_platform_and_byok_and_child_unblock(monkeypatch):
    """Item 2 (crash recovery path — recovery.py:428 -> coordinator.py:577):
    Both platform (`in_flight_credits > 0`) and BYOK (`in_flight_credits = 0`) runs recovered
    without a `ptc` usage row stay unsettled inside `_ABANDONED_SETTLE_GRACE`, then after 30m
    the abandoned sweep stamps `usage_settled_at`, settles reservations conservatively at
    `reserved_usd`, and unblocks child `subagent_runs` abandoned settlement."""
    _patch_finalize_side_effects(monkeypatch)
    db = _FakeBudgetAndLifecycleDB()

    @asynccontextmanager
    async def _fake_pool_conn():
        yield db.make_conn()

    monkeypatch.setattr("src.server.database.pool.get_db_connection", _fake_pool_conn)
    monkeypatch.setattr(
        "src.server.services.report_back.steer.confirm_checkpointed_holds",
        create_autospec(
            __import__("src.server.services.report_back.steer", fromlist=["confirm_checkpointed_holds"]).confirm_checkpointed_holds,
            return_value=0,
        ),
    )

    from src.server.services.runs import executor as executor_mod

    exec_inst = create_autospec(executor_mod.LocalRunExecutor, instance=True)
    monkeypatch.setattr(
        executor_mod.LocalRunExecutor,
        "get_instance",
        create_autospec(executor_mod.LocalRunExecutor.get_instance, return_value=exec_inst),
    )

    coord = RunCoordinator()
    monkeypatch.setattr(
        coord,
        "post_finalize_tail",
        create_autospec(coord.post_finalize_tail, return_value=None),
    )
    monkeypatch.setattr(
        RunCoordinator,
        "get_instance",
        create_autospec(RunCoordinator.get_instance, return_value=coord),
    )
    from src.server.services.runs import coordinator as coord_mod

    orig_spawn_protected = coord_mod.spawn_protected

    def _selective_spawn(coro, name, **kwargs):
        if str(name).startswith("model-handoff-"):
            coro.close()
            return asyncio.create_task(asyncio.sleep(0))
        return orig_spawn_protected(coro, name, **kwargs)

    monkeypatch.setattr(coord_mod, "spawn_protected", _selective_spawn)

    run_plat = "run-crash-platform"
    run_byok = "run-crash-byok"
    _seed_in_progress_run_with_credits(
        db, run_id=run_plat, thread_id="th-crash-p", user_id="u-rec", in_flight_credits=Decimal("5.0"), resv_state="open"
    )
    _seed_in_progress_run_with_credits(
        db, run_id=run_byok, thread_id="th-crash-b", user_id="u-rec", in_flight_credits=Decimal("0"), resv_state="released"
    )

    # Child subagent under the BYOK crashed parent (also in_flight_credits=0).
    child_id = str(uuid.uuid4())
    db.subagent_runs.append(
        {
            "task_run_id": child_id,
            "thread_id": "th-crash-b",
            "parent_run_id": run_byok,
            "status": "error",
            "cause": "init",
            "replay_facts": None,
            "finalized_at": db.now - timedelta(minutes=35),
            "in_flight_credits": Decimal("0"),
            "usage_settled_at": None,
        }
    )

    scanner = RecoveryScanner()
    monkeypatch.setattr(
        scanner,
        "_classify",
        create_autospec(
            scanner._classify,
            return_value=("error", None, ["worker_lost"], None, {}),
        ),
    )
    monkeypatch.setattr(
        scanner,
        "_salvage",
        create_autospec(scanner._salvage, return_value=(None, "none")),
    )
    monkeypatch.setattr(
        scanner,
        "_salvage_images",
        create_autospec(scanner._salvage_images, return_value={}),
    )

    assert await scanner._recover_run(db.responses[run_plat], run_plat, "th-crash-p") is True
    assert await scanner._recover_run(db.responses[run_byok], run_byok, "th-crash-b") is True

    # Right after recovery (within 30m grace): neither parent nor child is stamped or settled.
    assert db.responses[run_plat]["usage_settled_at"] is None
    assert db.responses[run_byok]["usage_settled_at"] is None
    assert "dalton_finalized_at" in db.responses[run_plat]["metadata"]
    assert "dalton_finalized_at" in db.responses[run_byok]["metadata"]

    assert await credit_ledger_db.settle_abandoned("run") == 0
    assert await credit_ledger_db.settle_abandoned("task") == 0
    await budget_db.reconcile_reservations_sql(db.make_conn(), "u-rec", Decimal("1000"))
    assert db.reservations[run_plat]["state"] == "open"
    assert db.reservations[run_byok]["state"] == "released"
    assert db.subagent_runs[0]["usage_settled_at"] is None

    # Advance past _ABANDONED_SETTLE_GRACE (31 minutes).
    db.now = db.now + timedelta(minutes=31)
    await budget_db.reconcile_reservations_sql(db.make_conn(), "u-rec", Decimal("1000"))

    # Both platform and BYOK parents are stamped and conservatively settled at reserved_usd=$1.00.
    assert db.responses[run_plat]["usage_settled_at"] is not None
    assert db.responses[run_byok]["usage_settled_at"] is not None
    for rid in (run_plat, run_byok):
        assert db.reservations[rid]["state"] == "settled"
        assert db.reservations[rid]["settle_source"] == "conservative"
        assert db.reservations[rid]["settled_usd"] == Decimal("1.00")

    # Item 3: once parent is fallback-stamped past its own dalton_finalized_at grace,
    # the already-aged child settles immediately without waiting a second 30m window.
    assert await credit_ledger_db.settle_abandoned("task") == 1
    assert db.subagent_runs[0]["usage_settled_at"] is not None


@pytest.mark.asyncio
async def test_r2_fallback_settle_dispatch_reconcile_platform_and_byok(monkeypatch):
    """Item 2 (dispatch reconcile path — coordinator.py:609 -> 577):
    Orphaned dispatched runs reconciled as error without a `ptc` usage row get fallback-stamped
    and conservatively settled at `reserved_usd` after `_ABANDONED_SETTLE_GRACE` for both
    platform and BYOK (`in_flight_credits=0`)."""
    _patch_finalize_side_effects(monkeypatch)
    db = _FakeBudgetAndLifecycleDB()

    @asynccontextmanager
    async def _fake_pool_conn():
        yield db.make_conn()

    monkeypatch.setattr("src.server.database.pool.get_db_connection", _fake_pool_conn)
    monkeypatch.setattr(
        "src.server.services.report_back.steer.confirm_checkpointed_holds",
        create_autospec(
            __import__("src.server.services.report_back.steer", fromlist=["confirm_checkpointed_holds"]).confirm_checkpointed_holds,
            return_value=0,
        ),
    )

    from src.server.services.runs import executor as executor_mod

    exec_inst = create_autospec(executor_mod.LocalRunExecutor, instance=True)
    monkeypatch.setattr(
        executor_mod.LocalRunExecutor,
        "get_instance",
        create_autospec(executor_mod.LocalRunExecutor.get_instance, return_value=exec_inst),
    )
    from src.server.services.runs import coordinator as coord_mod

    orig_spawn_protected = coord_mod.spawn_protected

    def _selective_spawn(coro, name, **kwargs):
        if str(name).startswith("model-handoff-"):
            coro.close()
            return asyncio.create_task(asyncio.sleep(0))
        return orig_spawn_protected(coro, name, **kwargs)

    monkeypatch.setattr(coord_mod, "spawn_protected", _selective_spawn)

    coord = RunCoordinator()
    monkeypatch.setattr(
        coord,
        "_detached_input_stamp",
        create_autospec(coord._detached_input_stamp, return_value=(None, {})),
    )
    monkeypatch.setattr(
        coord,
        "post_finalize_tail",
        create_autospec(coord.post_finalize_tail, return_value=None),
    )

    run_plat = "run-dispatch-plat"
    run_byok = "run-dispatch-byok"
    _seed_in_progress_run_with_credits(
        db, run_id=run_plat, thread_id="th-disp-p", user_id="u-disp", in_flight_credits=Decimal("2.5"), reserved_usd=Decimal("0.75")
    )
    _seed_in_progress_run_with_credits(
        db, run_id=run_byok, thread_id="th-disp-b", user_id="u-disp", in_flight_credits=Decimal("0"), reserved_usd=Decimal("0.50")
    )

    assert await coord.reconcile_orphaned_dispatch("th-disp-p", run_plat) == "error"
    assert await coord.reconcile_orphaned_dispatch("th-disp-b", run_byok) == "error"
    assert db.responses[run_plat]["usage_settled_at"] is None
    assert db.responses[run_byok]["usage_settled_at"] is None

    # Inside grace: nothing settles yet.
    await budget_db.reconcile_reservations_sql(db.make_conn(), "u-disp", Decimal("1000"))
    assert db.reservations[run_plat]["state"] == "released"
    assert db.reservations[run_byok]["state"] == "released"

    # Past 30m grace: RecoveryScanner.settle_abandoned_usage() stamps both parents and
    # conservatively settles both reservations at their reserved_usd floors.
    db.now = db.now + timedelta(minutes=31)
    scanner = RecoveryScanner()
    settled = await scanner.settle_abandoned_usage()
    assert settled["responses"] == 2
    assert db.responses[run_plat]["usage_settled_at"] is not None
    assert db.responses[run_byok]["usage_settled_at"] is not None
    assert db.reservations[run_plat]["state"] == "settled"
    assert db.reservations[run_plat]["settle_source"] == "conservative"
    assert db.reservations[run_plat]["settled_usd"] == Decimal("0.75")
    assert db.reservations[run_byok]["state"] == "settled"
    assert db.reservations[run_byok]["settle_source"] == "conservative"
    assert db.reservations[run_byok]["settled_usd"] == Decimal("0.50")


@pytest.mark.asyncio
async def test_r2_fallback_settle_all_model_calls_errored_platform_and_byok(monkeypatch):
    """Item 2 (all-calls-errored turn path — finalization.py:221 -> coordinator.py:426):
    When every model call errors (`llm_calls_started() > 0`, `per_call_records == []`),
    `finalize_run` withholds `usage_settled_at`; after `_ABANDONED_SETTLE_GRACE`, fallback
    settlement stamps `usage_settled_at` and settles at `reserved_usd` for both platform and BYOK."""
    _patch_finalize_side_effects(monkeypatch)
    db = _FakeBudgetAndLifecycleDB()

    for run_id, thread_id, in_flight, reserved in (
        ("run-all-err-plat", "th-err-p", Decimal("1.0"), Decimal("1.00")),
        ("run-all-err-byok", "th-err-b", Decimal("0"), Decimal("0.60")),
    ):
        _seed_in_progress_run_with_credits(
            db,
            run_id=run_id,
            thread_id=thread_id,
            user_id="u-err",
            in_flight_credits=in_flight,
            reserved_usd=reserved,
        )
        tracker = PerCallTokenTracker()
        llm_rid = uuid.uuid4()
        tracker.on_chat_model_start({}, [], run_id=llm_rid, metadata={"billing_type": "byok"})
        tracker.on_llm_error(RuntimeError("upstream 500"), run_id=llm_rid)
        assert tracker.llm_calls_started() == 1
        assert tracker.get_per_call_records() == []

        with patch.object(
            finalization,
            "_delivered_steering_inputs",
            create_autospec(finalization._delivered_steering_inputs, return_value=[]),
        ):
            outcome = await finalization.assemble_finalize_artifacts(
                (thread_id, run_id),
                metadata={"token_callback": tracker, "is_byok": in_flight == 0},
                status="error",
                phase="error",
                interrupt_reason=None,
                pause_message=None,
                error="upstream 500",
                handler=None,
                cancelled_by_user=False,
                workspace_id="ws-1",
                recorded_as="u-err",
            )
        assert outcome.no_model_calls is False
        res = await finalize_run(
            run_id=run_id,
            thread_id=thread_id,
            outcome=outcome,
            usage_writer=None,
            conn=db.make_conn(),
        )
        assert res.applied is True
        assert res.run["usage_settled_at"] is None

    # Inside grace: stays released, not stamped.
    await budget_db.reconcile_reservations_sql(db.make_conn(), "u-err", Decimal("1000"))
    assert db.reservations["run-all-err-plat"]["state"] == "released"
    assert db.reservations["run-all-err-byok"]["state"] == "released"

    # Past 30m grace: both platform and BYOK settle conservatively at reserved_usd and get stamped.
    db.now = db.now + timedelta(minutes=31)
    await budget_db.reconcile_reservations_sql(db.make_conn(), "u-err", Decimal("1000"))
    for rid, expected_floor in (("run-all-err-plat", Decimal("1.00")), ("run-all-err-byok", Decimal("0.60"))):
        assert db.responses[rid]["usage_settled_at"] is not None
        assert db.reservations[rid]["state"] == "settled"
        assert db.reservations[rid]["settle_source"] == "conservative"
        assert db.reservations[rid]["settled_usd"] == expected_floor


@pytest.mark.asyncio
async def test_r2_dalton_complete_unconditionally_overrides_upfront_identity_on_done_error_and_cancel(
    monkeypatch,
):
    """Item 3: Upfront derivation is only used as temporary claim metadata; on done, error,
    AND cancellation, the actual client identity captured in the bucket unconditionally
    overrides `provider`, `billing_type`, `pricing_model_id`, and `pricing_provider`."""
    store = _FakeJournalDB()

    @asynccontextmanager
    async def _fake_db_conn():
        yield store.conn()

    monkeypatch.setattr(journal_db, "get_db_connection", _fake_db_conn)
    monkeypatch.setattr(
        complete_mod,
        "_check_cash_budget_admission",
        create_autospec(
            complete_mod._check_cash_budget_admission,
            return_value=(True, None, None),
        ),
    )
    monkeypatch.setattr(
        complete_mod,
        "record_provider_event",
        create_autospec(complete_mod.record_provider_event, return_value=1),
    )
    # Upfront derivation claims a non-unknown wrong identity ("openai", "byok").
    monkeypatch.setattr(
        complete_mod,
        "_resolve_model_pricing_identity",
        create_autospec(
            complete_mod._resolve_model_pricing_identity,
            return_value=("byok", "openai", "upfront-wrong-id", "openai"),
        ),
    )

    class _ActualClient:
        def __init__(self, metadata: dict[str, Any], mode: str) -> None:
            self.metadata = metadata
            self._mode = mode

        async def ainvoke(self, _messages: Any) -> AIMessage:
            if self._mode == "cancel":
                raise asyncio.CancelledError()
            if self._mode == "error":
                raise TimeoutError("upstream timed out")
            return AIMessage(
                content="ok",
                usage_metadata={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
            )

    actual_meta = {
        "pricing_model_id": "claude-sonnet-4-5",
        "pricing_provider": "anthropic",
        "billing_type": "platform",
    }

    async def _run_with_mode(mode: str, prompt: str):
        async def _complete_impl(**_kw: Any):
            client = _ActualClient(actual_meta, mode)
            return await make_api_call(
                client,
                system_prompt="",
                user_prompt=prompt,
                return_token_usage=True,
                disable_tracing=True,
            )

        svc = create_autospec(LLMService, instance=True)
        svc.complete.side_effect = _complete_impl
        return await dalton_complete(
            svc,
            user_id="u-override",
            user_prompt=prompt,
            model="custom-aliased-model",
            purpose=f"override_{mode}",
        )

    # 1. Done path
    r_done = await _run_with_mode("done", "p-done")
    assert r_done.ok is True
    assert r_done.provider == "anthropic"
    assert r_done.billing_type == "platform"
    assert r_done.usage["pricing_model_id"] == "claude-sonnet-4-5"
    assert r_done.usage["pricing_provider"] == "anthropic"
    assert store.rows[r_done.invocation_id]["provider"] == "anthropic"
    assert store.rows[r_done.invocation_id]["billing_type"] == "platform"
    assert store.attempts[(r_done.invocation_id, 1, 1)]["provider"] == "anthropic"
    assert store.attempts[(r_done.invocation_id, 1, 1)]["billing_type"] == "platform"

    # 2. Error path
    r_err = await _run_with_mode("error", "p-err")
    assert r_err.ok is False
    assert r_err.provider == "anthropic"
    assert r_err.billing_type == "platform"
    assert store.rows[r_err.invocation_id]["provider"] == "anthropic"
    assert store.rows[r_err.invocation_id]["billing_type"] == "platform"
    assert store.attempts[(r_err.invocation_id, 1, 1)]["provider"] == "anthropic"
    assert store.attempts[(r_err.invocation_id, 1, 1)]["billing_type"] == "platform"

    # 3. Cancellation path
    before_ids = set(store.rows)
    with pytest.raises(asyncio.CancelledError):
        await _run_with_mode("cancel", "p-cancel")
    cancel_id = next(iter(set(store.rows) - before_ids))
    assert store.rows[cancel_id]["provider"] == "anthropic"
    assert store.rows[cancel_id]["billing_type"] == "platform"
    assert store.attempts[(cancel_id, 1, 1)]["provider"] == "anthropic"
    assert store.attempts[(cancel_id, 1, 1)]["billing_type"] == "platform"


@pytest.mark.asyncio
async def test_r2_make_api_call_per_record_pricing_identity_isolated_across_concurrent_and_fallback_calls():
    """Item 4: `make_api_call` passes each call's `llm.metadata` identity locally to
    `_track_upstream_call` and binds it directly on that record without mutating a shared
    bucket singleton, preventing cross-contamination across concurrent or fallback calls."""
    gate = asyncio.Event()

    class _ConcurrentClient:
        def __init__(self, metadata: dict[str, Any], *, wait_gate: bool = False) -> None:
            self.metadata = metadata
            self._wait_gate = wait_gate

        async def ainvoke(self, _messages: Any) -> AIMessage:
            if self._wait_gate:
                await gate.wait()
            else:
                gate.set()
            return AIMessage(
                content="ok",
                usage_metadata={"input_tokens": 500, "output_tokens": 100, "total_tokens": 600},
            )

    client_a = _ConcurrentClient(
        {
            "pricing_model_id": "gpt-6.1-terra",
            "pricing_provider": "openai",
            "billing_type": "byok",
        },
        wait_gate=True,
    )
    client_b = _ConcurrentClient(
        {
            "pricing_model_id": "claude-opus-5-5-oauth",
            "pricing_provider": "claude-oauth",
            "billing_type": "oauth",
        },
        wait_gate=False,
    )

    with collect_api_call_usage() as bucket:
        await asyncio.gather(
            make_api_call(client_a, "", "call-a", return_token_usage=True, disable_tracing=True),
            make_api_call(client_b, "", "call-b", return_token_usage=True, disable_tracing=True),
        )

    assert len(bucket.call_records) == 2
    by_pid = {r["pricing_model_id"]: r for r in bucket.call_records}
    assert by_pid["gpt-6.1-terra"]["pricing_provider"] == "openai"
    assert by_pid["gpt-6.1-terra"]["billing_type"] == "byok"
    assert by_pid["claude-opus-5-5-oauth"]["pricing_provider"] == "claude-oauth"
    assert by_pid["claude-opus-5-5-oauth"]["billing_type"] == "oauth"


def test_r2_per_call_token_tracker_llm_calls_started_deduplicates_after_end_and_error_until_reset():
    """Item 5: `PerCallTokenTracker.llm_calls_started()` deduplicates `run_id` across the
    entire turn via a dedicated set (even if a duplicate start callback arrives after
    `on_llm_end` or `on_llm_error` drained `_run_attribution`), and clears on `reset()`."""
    tracker = PerCallTokenTracker()
    rid_1 = uuid.uuid4()
    rid_2 = uuid.uuid4()

    tracker.on_chat_model_start({}, [], run_id=rid_1)
    tracker.on_llm_start({}, ["p"], run_id=rid_1)
    assert tracker.llm_calls_started() == 1

    tracker.on_llm_end(
        LLMResult(generations=[[ChatGeneration(message=AIMessage(content="ok"))]]),
        run_id=rid_1,
    )
    # Late duplicate start callback for the already-ended run_id must NOT increment.
    tracker.on_chat_model_start({}, [], run_id=rid_1)
    assert tracker.llm_calls_started() == 1

    tracker.on_chat_model_start({}, [], run_id=rid_2)
    tracker.on_llm_error(RuntimeError("boom"), run_id=rid_2)
    tracker.on_llm_start({}, ["p"], run_id=rid_2)
    assert tracker.llm_calls_started() == 2

    tracker.reset()
    assert tracker.llm_calls_started() == 0
    tracker.on_chat_model_start({}, [], run_id=rid_1)
    assert tracker.llm_calls_started() == 1


def test_r2_interface_compat_compute_call_costs_signature_and_run_outcome_kw_only():
    """Item 6: `compute_call_costs` keeps its original 3-param signature; new pricing params
    live only on `compute_call_costs_detailed`; `RunOutcome.no_model_calls` is keyword-only
    at the end of the dataclass; `_price_complete_usage` delegates directly to
    `compute_call_costs_detailed` without identity-checking `compute_call_costs`."""
    sig_legacy = inspect.signature(compute_call_costs)
    assert list(sig_legacy.parameters.keys()) == ["model_key", "billing_type", "token_usage"]

    sig_detailed = inspect.signature(compute_call_costs_detailed)
    assert "pricing_model_id" in sig_detailed.parameters
    assert "pricing_provider" in sig_detailed.parameters

    assert not hasattr(complete_mod, "_REAL_COMPUTE_CALL_COSTS")

    # Positional RunOutcome construction preserves record & images positions; no_model_calls is kw-only.
    rec = lifecycle_db.ProducerRecord()
    imgs = {"a.png": "https://example.com/a.png"}
    outcome = RunOutcome("completed", None, {}, None, None, 1.5, None, None, rec, imgs)
    assert outcome.record is rec
    assert outcome.images == imgs
    assert outcome.no_model_calls is False

    with pytest.raises(TypeError):
        RunOutcome("completed", None, {}, None, None, 1.5, None, None, rec, imgs, True)  # type: ignore[misc]

    outcome_kw = RunOutcome("completed", no_model_calls=True)
    assert outcome_kw.no_model_calls is True


@pytest.mark.asyncio
async def test_r2_budget_service_reuses_is_pricing_failed_journal_row(monkeypatch):
    """Item 7: Both journal pricing-failure checks in `budget/service.py`
    (`compute_provider_breakdown` and `reconcile_and_summarize_day`) reuse
    `is_pricing_failed_journal_row`, including rows carrying top-level `pricing_failed=True`."""
    spy = create_autospec(
        journal_db.is_pricing_failed_journal_row,
        side_effect=journal_db.is_pricing_failed_journal_row,
    )
    monkeypatch.setattr(budget_svc, "is_pricing_failed_journal_row", spy)

    top_level_failed_row = {
        "invocation_id": "inv-top-pf",
        "model_key": "fake-top-pf",
        "provider": "openai",
        "billing_type": "byok",
        "state": "done",
        "cash_usd": Decimal("0"),
        "notional_usd": Decimal("0.50"),
        "pricing_failed": True,
        "usage": {},
    }

    bd = budget_svc.compute_provider_breakdown([], [top_level_failed_row])
    assert bd["openai"]["pricing_failed"] is True
    assert bd["openai"]["pricing_failed_calls"] == 1
    assert bd["openai"]["cash_usd"] == pytest.approx(float(budget_svc._conservative_pricing_floor_usd()))
    assert spy.call_count >= 1

    before = spy.call_count
    monkeypatch.setattr(
        budget_svc,
        "resolve_user_budget_config",
        create_autospec(
            budget_svc.resolve_user_budget_config,
            return_value={
                "user_id": "u-pf",
                "daily_cap_usd": Decimal("10.00"),
                "default_task_cap_usd": None,
                "timezone": "UTC",
                "configured": True,
            },
        ),
    )
    monkeypatch.setattr(
        budget_svc,
        "acquire_budget_advisory_lock",
        create_autospec(budget_svc.acquire_budget_advisory_lock, return_value=None),
    )
    monkeypatch.setattr(
        budget_svc,
        "reconcile_reservations_sql",
        create_autospec(budget_svc.reconcile_reservations_sql, return_value=None),
    )
    monkeypatch.setattr(
        budget_svc,
        "fetch_day_reservations",
        create_autospec(budget_svc.fetch_day_reservations, return_value=[]),
    )
    monkeypatch.setattr(
        budget_svc,
        "fetch_unreserved_usage_today",
        create_autospec(budget_svc.fetch_unreserved_usage_today, return_value=Decimal("0")),
    )
    monkeypatch.setattr(
        budget_svc,
        "fetch_journal_cash_today",
        create_autospec(budget_svc.fetch_journal_cash_today, return_value=Decimal("0.25")),
    )
    monkeypatch.setattr(
        budget_svc,
        "fetch_today_token_usages",
        create_autospec(budget_svc.fetch_today_token_usages, return_value=[]),
    )
    monkeypatch.setattr(
        budget_svc,
        "fetch_journal_rows_in_window",
        create_autospec(
            budget_svc.fetch_journal_rows_in_window,
            return_value=[top_level_failed_row],
        ),
    )

    summary = await budget_svc.reconcile_and_summarize_day(
        _FakeBudgetAndLifecycleDB().make_conn(), "u-pf", now_utc=datetime.now(UTC)
    )
    # Called both in reconcile_and_summarize_day's journal_notional_usd filter AND in compute_provider_breakdown.
    assert spy.call_count >= before + 2
    assert summary["subscription_notional_usd"] == 0.0


# ===========================================================================
# Round 3 fixes (Items 1, 2, 3, 4, 6, 7)
# ===========================================================================


@pytest.mark.asyncio
async def test_r3_item1_proven_zero_model_calls_settles_without_reserved_floor_across_all_three_paths(
    monkeypatch,
):
    """R3 Item 1: `no_model_calls=True` persists `metadata.dalton_no_model_calls=true`
    alongside `dalton_finalized_at` and settles at actual tool/subagent usage (never
    `reserved_usd`), across all three paths:
      1. Cancelled before first model call (`assemble_finalize_artifacts`, with and without tool/subagent usage);
      2. Sandbox boot failure (`error_handling.py` / `token_callback is None`);
      3. `RunCoordinator.fail_open_run`."""
    _patch_finalize_side_effects(monkeypatch)
    db = _FakeBudgetAndLifecycleDB()

    @asynccontextmanager
    async def _fake_pool_conn():
        yield db.make_conn()

    monkeypatch.setattr("src.server.database.pool.get_db_connection", _fake_pool_conn)
    from src.server.services.runs import coordinator as coord_mod

    orig_spawn_protected = coord_mod.spawn_protected

    def _selective_spawn(coro, name, **kwargs):
        if str(name).startswith("model-handoff-"):
            coro.close()
            return asyncio.create_task(asyncio.sleep(0))
        return orig_spawn_protected(coro, name, **kwargs)

    monkeypatch.setattr(coord_mod, "spawn_protected", _selective_spawn)

    user_id = "u-r3-zero"

    # --- Path 1a: Cancelled before first model call (0 usage rows) ---
    run_cancel_zero = "run-r3-cancel-zero"
    _seed_in_progress_run_with_credits(
        db, run_id=run_cancel_zero, thread_id="th-c0", user_id=user_id, in_flight_credits=Decimal("0"), reserved_usd=Decimal("1.00")
    )
    tracker_zero = PerCallTokenTracker()
    assert tracker_zero.llm_calls_started() == 0
    with patch.object(
        finalization,
        "_delivered_steering_inputs",
        create_autospec(finalization._delivered_steering_inputs, return_value=[]),
    ):
        cancel_outcome = await finalization.assemble_finalize_artifacts(
            ("th-c0", run_cancel_zero),
            metadata={"token_callback": tracker_zero, "is_byok": True},
            status="cancelled",
            phase="cancellation",
            interrupt_reason=None,
            pause_message=None,
            error=None,
            handler=None,
            cancelled_by_user=True,
            workspace_id="ws-1",
            recorded_as=user_id,
        )
    assert cancel_outcome.no_model_calls is True
    res_c0 = await finalize_run(
        run_id=run_cancel_zero, thread_id="th-c0", outcome=cancel_outcome, usage_writer=None, conn=db.make_conn()
    )
    assert res_c0.applied is True
    assert db.responses[run_cancel_zero]["metadata"]["dalton_no_model_calls"] is True
    assert "dalton_finalized_at" in db.responses[run_cancel_zero]["metadata"]

    # --- Path 1b: Cancelled before first model call WITH tool + subagent usage ---
    run_cancel_tools = "run-r3-cancel-tools"
    _seed_in_progress_run_with_credits(
        db, run_id=run_cancel_tools, thread_id="th-ct", user_id=user_id, in_flight_credits=Decimal("0"), reserved_usd=Decimal("1.00")
    )
    sub_tr = str(uuid.uuid4())
    db.subagent_runs.append(
        {
            "task_run_id": sub_tr,
            "thread_id": "th-ct",
            "task_id": "t-sub",
            "parent_run_id": run_cancel_tools,
            "predecessor_run_id": None,
            "status": "completed",
            "cause": "init",
            "replay_facts": None,
            "finalized_at": db.now,
            "usage_settled_at": db.now,
            "in_flight_credits": Decimal("0"),
            "user_id": user_id,
        }
    )
    db.usages.append(
        {
            "conversation_response_id": run_cancel_tools,
            "msg_type": "ptc",
            "is_byok": False,
            "token_usage": None,
            "total_credits": Decimal("25"),  # $0.025 tool cost
            "infrastructure_credits": Decimal("25"),
        }
    )
    db.usages.append(
        {
            "conversation_response_id": run_cancel_tools,
            "msg_type": "task",
            "is_byok": True,
            "token_usage": {
                "task_run_id": sub_tr,
                "per_call_costs": [{"model_name": "gpt-6.1-terra", "billing_type": "byok", "cost": 0.04}],
            },
            "total_credits": Decimal("0"),
            "infrastructure_credits": Decimal("0"),
        }
    )
    await finalize_run(
        run_id=run_cancel_tools,
        thread_id="th-ct",
        outcome=RunOutcome(status="cancelled", no_model_calls=True),
        usage_writer=None,
        conn=db.make_conn(),
    )
    assert db.responses[run_cancel_tools]["metadata"]["dalton_no_model_calls"] is True

    # --- Path 2: Sandbox boot failure (error_handling.py: token_callback is None -> no_model_calls=True) ---
    run_sandbox_fail = "run-r3-sandbox-fail"
    _seed_in_progress_run_with_credits(
        db, run_id=run_sandbox_fail, thread_id="th-sb", user_id=user_id, in_flight_credits=Decimal("0"), reserved_usd=Decimal("1.00")
    )
    from src.server.handlers.chat import error_handling as err_mod

    coord = RunCoordinator()
    monkeypatch.setattr(
        coord, "post_finalize_tail", create_autospec(coord.post_finalize_tail, return_value=None)
    )
    monkeypatch.setattr(
        RunCoordinator, "get_instance", create_autospec(RunCoordinator.get_instance, return_value=coord)
    )

    handle_sb = RunHandle(
        run_id=run_sandbox_fail,
        thread_id="th-sb",
        turn_index=0,
        attempt_no=1,
        workspace_id="ws-1",
        recorded_as=user_id,
    )

    async def _noop_release_slot() -> None:
        return None

    fake_scope = SimpleNamespace(
        handed_off=False,
        owned_run_handle=handle_sb,
        release_slot=_noop_release_slot,
    )
    fake_request = SimpleNamespace(
        request_key=None,
        workspace_id="ws-1",
        locale=None,
        messages=[],
        hitl_response=None,
        additional_context=None,
        checkpoint_id=None,
    )
    events = []
    async for ev in err_mod.handle_workflow_error(
        RuntimeError("sandbox boot failed"),
        thread_id="th-sb",
        user_id=user_id,
        workspace_id="ws-1",
        handler=None,
        token_callback=None,
        scope=fake_scope,
        start_time=0.0,
        request=fake_request,
        is_byok=True,
    ):
        events.append(ev)
    assert db.responses[run_sandbox_fail]["status"] == "error"
    assert db.responses[run_sandbox_fail]["metadata"]["dalton_no_model_calls"] is True
    assert "dalton_finalized_at" in db.responses[run_sandbox_fail]["metadata"]

    # --- Path 3: fail_open_run ---
    run_fail_open = "run-r3-fail-open"
    _seed_in_progress_run_with_credits(
        db, run_id=run_fail_open, thread_id="th-fo", user_id=user_id, in_flight_credits=Decimal("0"), reserved_usd=Decimal("1.00")
    )
    handle_fo = RunHandle(
        run_id=run_fail_open,
        thread_id="th-fo",
        turn_index=0,
        attempt_no=1,
        workspace_id="ws-1",
        recorded_as=user_id,
    )
    await coord.fail_open_run(handle_fo, "failed before handoff", no_model_calls=True)
    assert db.responses[run_fail_open]["status"] == "error"
    assert db.responses[run_fail_open]["metadata"]["dalton_no_model_calls"] is True
    assert "dalton_finalized_at" in db.responses[run_fail_open]["metadata"]

    # --- Also test direct fallback SQL (`settle_abandoned_runs_without_ptc_sql`) on a fail_open_run ---
    run_fail_open_fb = "run-r3-fail-open-fb"
    _seed_in_progress_run_with_credits(
        db, run_id=run_fail_open_fb, thread_id="th-fofb", user_id="u-r3-zero-fb", in_flight_credits=Decimal("0"), reserved_usd=Decimal("1.00")
    )
    handle_fofb = RunHandle(
        run_id=run_fail_open_fb,
        thread_id="th-fofb",
        turn_index=0,
        attempt_no=1,
        workspace_id="ws-1",
        recorded_as="u-r3-zero-fb",
    )
    await coord.fail_open_run(
        handle_fofb, "failed before handoff (fallback path)", no_model_calls=True
    )

    # Reconcile all three paths immediately via Step 1 — none should retain $1.00 reserved_usd!
    await budget_db.reconcile_reservations_sql(db.make_conn(), user_id, Decimal("1000"))

    assert db.reservations[run_cancel_zero]["state"] == "settled"
    assert db.reservations[run_cancel_zero]["settle_source"] == "usage"
    assert db.reservations[run_cancel_zero]["settled_usd"] == Decimal("0")

    assert db.reservations[run_cancel_tools]["state"] == "settled"
    assert db.reservations[run_cancel_tools]["settle_source"] == "usage"
    assert db.reservations[run_cancel_tools]["settled_usd"] == Decimal("0.065")

    assert db.reservations[run_sandbox_fail]["state"] == "settled"
    assert db.reservations[run_sandbox_fail]["settle_source"] == "usage"
    assert db.reservations[run_sandbox_fail]["settled_usd"] == Decimal("0")

    assert db.reservations[run_fail_open]["state"] == "settled"
    assert db.reservations[run_fail_open]["settle_source"] == "usage"
    assert db.reservations[run_fail_open]["settled_usd"] == Decimal("0")

    # Direct fallback SQL (e.g. RecoveryScanner running after 30m before reconcile Step 1)
    # must also settle proven-zero runs at actual usage ($0), never reserved_usd.
    db.now = db.now + timedelta(minutes=31)
    settled_fb = await budget_db.settle_abandoned_runs_without_ptc_sql(
        db.make_conn(), Decimal("1000"), user_id="u-r3-zero-fb"
    )
    assert settled_fb == 1
    assert db.reservations[run_fail_open_fb]["state"] == "settled"
    assert db.reservations[run_fail_open_fb]["settle_source"] == "usage"
    assert db.reservations[run_fail_open_fb]["settled_usd"] == Decimal("0")


@pytest.mark.asyncio
async def test_r3_item2_same_parent_resumed_subagent_chain_merges_usage_check():
    """R3 Item 2: When a subagent is resumed within the same parent turn (`predecessor_run_id`
    chain + same `(thread_id, task_id)`), the merged `msg_type='task'` usage row carries only
    the latest `task_run_id` while all executions in the chain are stamped `usage_settled_at`.
    Budget settlement must treat the whole chain as having usage whenever any run in that
    same-parent chain has a usage row, while an unrelated subagent missing usage stays conservative."""
    db = _FakeBudgetAndLifecycleDB()
    user_id = "u-r3-resume"

    # Case 1: 3-round resume chain (tr1 -> tr2 -> tr3) under run_ok; single merged usage row with tr3
    run_ok = "run-r3-resumed-ok"
    db.responses[run_ok] = {
        "conversation_response_id": run_ok,
        "status": "completed",
        "usage_settled_at": db.now,
        "metadata": {"dalton_finalized_at": db.now.isoformat().replace("+00:00", "Z")},
    }
    db.reservations[run_ok] = {
        "reservation_id": "res-ok",
        "user_id": user_id,
        "run_ref": run_ok,
        "state": "released",
        "reserved_usd": Decimal("1.00"),
        "observed_usd": Decimal("0"),
        "settled_usd": None,
        "settle_source": None,
    }
    tr1, tr2, tr3 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    for tr_id, pred_id, cause in ((tr1, None, "init"), (tr2, tr1, "resume"), (tr3, tr2, "resume")):
        db.subagent_runs.append(
            {
                "task_run_id": tr_id,
                "thread_id": "th-res",
                "task_id": "task-chain-1",
                "parent_run_id": run_ok,
                "predecessor_run_id": pred_id,
                "cause": cause,
                "status": "completed",
                "replay_facts": None,
                "finalized_at": db.now,
                "usage_settled_at": db.now,
            }
        )
    db.usages.append(
        {
            "conversation_response_id": run_ok,
            "msg_type": "ptc",
            "is_byok": True,
            "token_usage": {"per_call_costs": [{"model_name": "gpt-6.1-terra", "billing_type": "byok", "cost": 0.04}]},
            "total_credits": Decimal("0"),
            "infrastructure_credits": Decimal("0"),
        }
    )
    db.usages.append(
        {
            "conversation_response_id": run_ok,
            "msg_type": "task",
            "is_byok": True,
            "token_usage": {
                "task_run_id": tr3,
                "per_call_costs": [
                    {"model_name": "gpt-6.1-terra", "billing_type": "byok", "cost": 0.03},
                    {"model_name": "gpt-6.1-terra", "billing_type": "byok", "cost": 0.05},
                ],
            },
            "total_credits": Decimal("0"),
            "infrastructure_credits": Decimal("0"),
        }
    )

    # Case 2: Resumed chain has usage, BUT an unrelated sibling subagent has NO usage row
    # (even if a legacy untagged task row with token_usage=None exists) -> must stay conservative!
    run_missing = "run-r3-resumed-missing-sibling"
    db.responses[run_missing] = {
        "conversation_response_id": run_missing,
        "status": "completed",
        "usage_settled_at": db.now,
        "metadata": {"dalton_finalized_at": db.now.isoformat().replace("+00:00", "Z")},
    }
    db.reservations[run_missing] = {
        "reservation_id": "res-miss",
        "user_id": user_id,
        "run_ref": run_missing,
        "state": "released",
        "reserved_usd": Decimal("1.00"),
        "observed_usd": Decimal("0"),
        "settled_usd": None,
        "settle_source": None,
    }
    tr4, tr5, tr_orphan = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    db.subagent_runs.extend(
        [
            {
                "task_run_id": tr4,
                "thread_id": "th-res",
                "task_id": "task-chain-2",
                "parent_run_id": run_missing,
                "predecessor_run_id": None,
                "cause": "init",
                "status": "completed",
                "replay_facts": None,
                "finalized_at": db.now,
                "usage_settled_at": db.now,
            },
            {
                "task_run_id": tr5,
                "thread_id": "th-res",
                "task_id": "task-chain-2",
                "parent_run_id": run_missing,
                "predecessor_run_id": tr4,
                "cause": "resume",
                "status": "completed",
                "replay_facts": None,
                "finalized_at": db.now,
                "usage_settled_at": db.now,
            },
            {
                "task_run_id": tr_orphan,
                "thread_id": "th-res",
                "task_id": "task-other",
                "parent_run_id": run_missing,
                "predecessor_run_id": None,
                "cause": "init",
                "status": "error",
                "replay_facts": None,
                "finalized_at": db.now,
                "usage_settled_at": db.now,
            },
        ]
    )
    db.usages.extend(
        [
            {
                "conversation_response_id": run_missing,
                "msg_type": "ptc",
                "is_byok": True,
                "token_usage": {"per_call_costs": [{"model_name": "gpt-6.1-terra", "billing_type": "byok", "cost": 0.04}]},
                "total_credits": Decimal("0"),
                "infrastructure_credits": Decimal("0"),
            },
            {
                "conversation_response_id": run_missing,
                "msg_type": "task",
                "is_byok": True,
                "token_usage": {
                    "task_run_id": tr5,
                    "per_call_costs": [{"model_name": "gpt-6.1-terra", "billing_type": "byok", "cost": 0.06}],
                },
                "total_credits": Decimal("0"),
                "infrastructure_credits": Decimal("0"),
            },
            # An untagged task row (token_usage=None) must NOT falsely satisfy tr_orphan.
            {
                "conversation_response_id": run_missing,
                "msg_type": "task",
                "is_byok": False,
                "token_usage": None,
                "total_credits": Decimal("0"),
                "infrastructure_credits": Decimal("0"),
            },
        ]
    )

    await budget_db.reconcile_reservations_sql(db.make_conn(), user_id, Decimal("1000"))

    assert db.reservations[run_ok]["state"] == "settled"
    assert db.reservations[run_ok]["settle_source"] == "usage"
    assert db.reservations[run_ok]["settled_usd"] == Decimal("0.12")

    assert db.reservations[run_missing]["state"] == "settled"
    assert db.reservations[run_missing]["settle_source"] == "conservative"
    assert db.reservations[run_missing]["settled_usd"] == Decimal("1.00")

    # Verify the generated SQL walks the resume chain via WITH RECURSIVE + predecessor_run_id
    step1_sql = next(s for s in db.executed_sql if "WITH run_usage AS" in s)
    assert "WITH RECURSIVE chain AS" in step1_sql
    assert "predecessor_run_id" in step1_sql
    assert "NOT (scu.token_usage::jsonb ? 'task_run_id')" not in step1_sql


@pytest.mark.asyncio
async def test_r3_item3_and_item4_independent_grace_and_historical_scope(monkeypatch):
    """R3 Items 3 & 4:
    - Item 3: After a parent is fallback-stamped past its own `dalton_finalized_at` grace,
      an already-aged child settles immediately (no second 30m wait), while a child finalized
      only 10m ago waits out its own 30m grace; parent `usage_settled_at IS NULL` still blocks.
    - Item 4: `settle_abandoned("run")` and `settle_abandoned("task")` never touch pre-deploy
      historical rows (`in_flight_credits=0`, no `dalton_finalized_at`, no reservation), while
      still settling in-flight (`in_flight_credits > 0`), `dalton_finalized_at`, and reserved rows."""
    db = _FakeBudgetAndLifecycleDB()

    @asynccontextmanager
    async def _fake_pool_conn():
        yield db.make_conn()

    monkeypatch.setattr("src.server.database.pool.get_db_connection", _fake_pool_conn)

    old = db.now - timedelta(minutes=35)
    recent = db.now - timedelta(minutes=10)
    ancient = db.now - timedelta(days=60)

    # 1. Historical pre-033 row (no in_flight_credits, no dalton_finalized_at, no reservation) -> untouched!
    db.responses["run-historical"] = {
        "conversation_response_id": "run-historical",
        "status": "completed",
        "user_id": None,
        "in_flight_credits": Decimal("0"),
        "usage_settled_at": None,
        "created_at": ancient,
        "execution_time": 1.0,
        "metadata": {},
    }
    db.subagent_runs.append(
        {
            "task_run_id": "tr-historical",
            "thread_id": "th-h",
            "task_id": "t-h",
            "parent_run_id": "run-historical",
            "status": "completed",
            "cause": "init",
            "replay_facts": None,
            "finalized_at": ancient,
            "in_flight_credits": Decimal("0"),
            "user_id": None,
            "usage_settled_at": None,
        }
    )

    # 2. Post-deploy BYOK parent finalized 35m ago with 2 children: one aged 35m, one aged 10m
    db.responses["run-byok-post"] = {
        "conversation_response_id": "run-byok-post",
        "status": "error",
        "user_id": "u-scope",
        "in_flight_credits": Decimal("0"),
        "usage_settled_at": None,
        "created_at": old,
        "execution_time": 1.0,
        "metadata": {"dalton_finalized_at": old.isoformat().replace("+00:00", "Z")},
    }
    db.subagent_runs.extend(
        [
            {
                "task_run_id": "tr-aged",
                "thread_id": "th-s",
                "task_id": "t-aged",
                "parent_run_id": "run-byok-post",
                "status": "error",
                "cause": "init",
                "replay_facts": None,
                "finalized_at": old,
                "in_flight_credits": Decimal("0"),
                "user_id": "u-scope",
                "usage_settled_at": None,
            },
            {
                "task_run_id": "tr-recent",
                "thread_id": "th-s",
                "task_id": "t-recent",
                "parent_run_id": "run-byok-post",
                "status": "error",
                "cause": "init",
                "replay_facts": None,
                "finalized_at": recent,
                "in_flight_credits": Decimal("0"),
                "user_id": "u-scope",
                "usage_settled_at": None,
            },
        ]
    )

    # 3. Reserved legacy row (no dalton_finalized_at, but has reservation with reserved_usd)
    db.responses["run-reserved-legacy"] = {
        "conversation_response_id": "run-reserved-legacy",
        "status": "error",
        "user_id": "u-scope",
        "in_flight_credits": Decimal("0"),
        "usage_settled_at": None,
        "created_at": old,
        "execution_time": 0.0,
        "metadata": {},
    }
    db.reservations["run-reserved-legacy"] = {
        "reservation_id": "res-leg",
        "user_id": "u-scope",
        "run_ref": "run-reserved-legacy",
        "state": "released",
        "reserved_usd": Decimal("0.50"),
        "observed_usd": Decimal("0"),
        "settled_usd": None,
        "settle_source": None,
    }

    # Before parent is settled, NO child settles (parent-settled prerequisite retained)
    assert await credit_ledger_db.settle_abandoned("task") == 0

    # Sweep runs: stamps run-byok-post and run-reserved-legacy, skips run-historical!
    assert await credit_ledger_db.settle_abandoned("run") == 2
    assert db.responses["run-historical"]["usage_settled_at"] is None
    assert db.responses["run-byok-post"]["usage_settled_at"] is not None
    assert db.responses["run-reserved-legacy"]["usage_settled_at"] is not None

    # Immediately after parent fallback stamp (NO 30m time advance!):
    # tr-aged settles immediately (its own finalized_at & parent's dalton_finalized_at both >30m),
    # tr-recent waits for its own 30m grace, tr-historical is never touched.
    assert await credit_ledger_db.settle_abandoned("task") == 1
    by_tid = {s["task_run_id"]: s["usage_settled_at"] for s in db.subagent_runs}
    assert by_tid["tr-aged"] is not None
    assert by_tid["tr-recent"] is None
    assert by_tid["tr-historical"] is None

    # Advance 21 minutes so tr-recent crosses its own 30m finalized_at grace -> settles now!
    db.now = db.now + timedelta(minutes=21)
    assert await credit_ledger_db.settle_abandoned("task") == 1
    by_tid = {s["task_run_id"]: s["usage_settled_at"] for s in db.subagent_runs}
    assert by_tid["tr-recent"] is not None
    assert by_tid["tr-historical"] is None
    assert db.responses["run-historical"]["usage_settled_at"] is None

    # Verify SQL predicates explicitly carry the scope guard & parent dalton_finalized_at grace
    run_sql = next(s for s in db.executed_sql if "UPDATE conversation_responses" in s)
    task_sql = next(s for s in db.executed_sql if "UPDATE subagent_runs" in s)
    assert "in_flight_credits > 0 AND user_id IS NOT NULL" in run_sql
    assert "br.reserved_usd IS NOT NULL" in run_sql
    assert "dalton_finalized_at" in run_sql
    assert "in_flight_credits > 0 AND user_id IS NOT NULL" in task_sql
    assert "br.reserved_usd IS NOT NULL" in task_sql
    assert "r.usage_settled_at IS NULL" in task_sql
    assert "r.metadata->>'dalton_finalized_at'" in task_sql


@pytest.mark.asyncio
async def test_r3_item6_legacy_pricing_failed_unreserved_row_takes_max_not_sum():
    """R3 Item 6: In `fetch_unreserved_usage_today`, a legacy row with `pricing_failed=true`
    and no `per_call_costs` array takes `max(floor, recorded_usd)` instead of `floor + recorded_usd`."""
    db = _FakeBudgetAndLifecycleDB()
    user_id = "u-r3-legacy-pf"
    floor = Decimal("0.250000")

    # Row A: recorded $0.10 (< floor $0.25) -> should contribute $0.25 (NOT $0.35)
    db.usages.append(
        {
            "conversation_response_id": "run-leg-low",
            "user_id": user_id,
            "created_at": db.now,
            "is_byok": False,
            "total_credits": Decimal("100"),  # $0.10 at rate=1000
            "infrastructure_credits": Decimal("0"),
            "token_usage": {"pricing_failed": True, "by_model": {"old-m": {"total_cost": 0.10}}},
        }
    )
    # Row B: recorded $0.40 (> floor $0.25) -> should contribute $0.40 (NOT $0.65)
    db.usages.append(
        {
            "conversation_response_id": "run-leg-high",
            "user_id": user_id,
            "created_at": db.now,
            "is_byok": False,
            "total_credits": Decimal("400"),  # $0.40 at rate=1000
            "infrastructure_credits": Decimal("0"),
            "token_usage": {"pricing_failed": True, "by_model": {"old-m": {"total_cost": 0.40}}},
        }
    )

    got = await budget_db.fetch_unreserved_usage_today(
        db.make_conn(),
        user_id,
        db.now - timedelta(hours=1),
        db.now + timedelta(hours=1),
        Decimal("1000"),
        conservative_floor_usd=floor,
    )
    assert got == Decimal("0.650000")
    unresv_sql = next(s for s in db.executed_sql if "AS unreserved_usd" in s)
    assert "GREATEST(%s::numeric, u.cash_usd)" in unresv_sql


@pytest.mark.asyncio
async def test_r3_item7_per_call_records_do_not_overwrite_journal_top_level_provider(monkeypatch):
    """R3 Item 7: When per-call records span multiple providers (e.g. primary `openai`/`byok`
    then fallback `anthropic`/`platform`), the top-level `dalton_llm_journal.provider` is NOT
    overwritten by the fallback's re-pricing identity, while each row in
    `dalton_llm_journal_attempts` records its own provider and billing_type."""
    store = _FakeJournalDB()

    @asynccontextmanager
    async def _fake_db_conn():
        yield store.conn()

    monkeypatch.setattr(journal_db, "get_db_connection", _fake_db_conn)
    monkeypatch.setattr(
        complete_mod,
        "_check_cash_budget_admission",
        create_autospec(
            complete_mod._check_cash_budget_admission,
            return_value=(True, None, None),
        ),
    )
    monkeypatch.setattr(
        complete_mod,
        "_resolve_model_pricing_identity",
        create_autospec(
            complete_mod._resolve_model_pricing_identity,
            return_value=("byok", "openai", "gpt-6.1-terra", "openai"),
        ),
    )

    class _Client:
        def __init__(self, metadata: dict[str, Any]) -> None:
            self.metadata = metadata

        async def ainvoke(self, _messages: Any) -> AIMessage:
            return AIMessage(
                content="ok",
                usage_metadata={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200},
            )

    primary_client = _Client(
        {"pricing_model_id": "gpt-6.1-terra", "pricing_provider": "openai", "billing_type": "byok"}
    )
    fallback_client = _Client(
        {"pricing_model_id": "claude-sonnet-5-5", "pricing_provider": "anthropic", "billing_type": "platform"}
    )

    async def _complete_with_fallback(**_kw: Any):
        _, u1 = await make_api_call(primary_client, "", "call-1", return_token_usage=True, disable_tracing=True)
        out2, u2 = await make_api_call(fallback_client, "", "call-2", return_token_usage=True, disable_tracing=True)
        return out2, {
            "input_tokens": u1["input_tokens"] + u2["input_tokens"],
            "output_tokens": u1["output_tokens"] + u2["output_tokens"],
            "total_tokens": u1["total_tokens"] + u2["total_tokens"],
        }

    svc = create_autospec(LLMService, instance=True)
    svc.complete.side_effect = _complete_with_fallback

    res = await dalton_complete(
        svc,
        user_id="u-r3-id",
        user_prompt="multi-provider fallback",
        model="custom-multi-provider",
        purpose="r3_item7",
    )

    assert res.ok is True
    # Top-level journal provider stays "openai", NOT overwritten by fallback's "anthropic"
    assert res.provider == "openai"
    assert store.rows[res.invocation_id]["provider"] == "openai"
    assert store.rows[res.invocation_id]["billing_type"] == "byok"
    assert "pricing_provider" not in res.usage
    assert "pricing_failed" not in res.usage

    # Each per-call attempt row retains its own actual client provider & billing_type
    att1 = store.attempts[(res.invocation_id, 1, 1)]
    att2 = store.attempts[(res.invocation_id, 1, 2)]
    assert att1["provider"] == "openai"
    assert att1["billing_type"] == "byok"
    assert att1["usage"]["pricing_provider"] == "openai"
    assert att1["usage"]["pricing_model_id"] == "gpt-6.1-terra"

    assert att2["provider"] == "anthropic"
    assert att2["billing_type"] == "platform"
    assert att2["usage"]["pricing_provider"] == "anthropic"
    assert att2["usage"]["pricing_model_id"] == "claude-sonnet-5-5"


# ==============================================================================
# R4 Tests: Items 1-5
# ==============================================================================


@pytest.mark.asyncio
async def test_r4_item1_integration_db_guards_never_bypass_on_dsn(monkeypatch):
    """R4 Item 1:
    - `tests/integration/dalton/conftest.py` never skips `_ensure_safe_test_db_name()`
      when `DALTON_TEST_PG_DSN` is set (mixed runs cannot wipe `TEST_DB_*`).
    - `tests/integration/conftest.py`'s `test_db_pool` checks the target DB name ends
      with `_test` BEFORE calling `psycopg.AsyncConnection.connect`."""
    import importlib.util
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]

    # 1. Dalton integration conftest refuses non-_test DB even when DALTON_TEST_PG_DSN is set.
    dalton_conf_path = repo_root / "tests" / "integration" / "dalton" / "conftest.py"
    spec_d = importlib.util.spec_from_file_location("_r4_dalton_int_conftest", dalton_conf_path)
    assert spec_d and spec_d.loader
    dalton_conf = importlib.util.module_from_spec(spec_d)
    spec_d.loader.exec_module(dalton_conf)

    monkeypatch.setenv("DALTON_TEST_PG_DSN", "postgresql://postgres:postgres@127.0.0.1:5432/postgres")
    monkeypatch.setenv("TEST_DB_NAME", "langalpha")

    from _pytest.outcomes import Exit as PytestExit

    with pytest.raises(PytestExit) as exc_start:
        dalton_conf.pytest_sessionstart(None)
    assert exc_start.value.returncode == 2
    assert "langalpha" in str(exc_start.value)

    with pytest.raises(PytestExit) as exc_col:
        dalton_conf.pytest_collection_modifyitems(None, None, [])
    assert exc_col.value.returncode == 2

    # 2. Upstream integration conftest `test_db_pool` fails BEFORE psycopg.AsyncConnection.connect.
    int_conf_path = repo_root / "tests" / "integration" / "conftest.py"
    spec_i = importlib.util.spec_from_file_location("_r4_int_conftest", int_conf_path)
    assert spec_i and spec_i.loader
    int_conf = importlib.util.module_from_spec(spec_i)
    spec_i.loader.exec_module(int_conf)

    connect_spy = AsyncMock(side_effect=AssertionError("connect must NOT be called for non-_test DB"))
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect_spy)

    # Unwrap pytest-asyncio fixture function to invoke the async generator directly.
    raw_pool_fn = int_conf.test_db_pool.__wrapped__
    gen = raw_pool_fn("postgresql://postgres:postgres@localhost:5432/langalpha?sslmode=prefer")
    with pytest.raises(RuntimeError, match="non-test database"):
        await gen.__anext__()
    connect_spy.assert_not_awaited()

    # 2b. Query-param `dbname` override (e.g. URI path ends with `_test` but `?dbname=langalpha`)
    # is caught by `psycopg.conninfo.conninfo_to_dict` BEFORE connecting.
    gen_override = raw_pool_fn(
        "postgresql://postgres:postgres@localhost:5432/langalpha_test?sslmode=prefer&dbname=langalpha"
    )
    with pytest.raises(RuntimeError, match="non-test database"):
        await gen_override.__anext__()
    connect_spy.assert_not_awaited()

    # 2c. Post-connect `SELECT current_database()` guard stops BEFORE `DROP SCHEMA public CASCADE`.
    executed_after_connect: list[str] = []

    class _FakeGuardCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, sql: str):
            executed_after_connect.append(sql)

        async def fetchone(self):
            return ("postgres",)

    class _FakeGuardConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def cursor(self):
            return _FakeGuardCursor()

        async def commit(self):
            pass

    monkeypatch.setattr(
        psycopg.AsyncConnection, "connect", AsyncMock(return_value=_FakeGuardConn())
    )
    gen_server_db = raw_pool_fn(
        "postgresql://postgres:postgres@localhost:5432/langalpha_test?sslmode=prefer"
    )
    with pytest.raises(RuntimeError, match="non-test database"):
        await gen_server_db.__anext__()
    assert executed_after_connect == ["SELECT current_database()"]
    assert all("DROP SCHEMA" not in s for s in executed_after_connect)


@pytest.mark.asyncio
async def test_r4_item2_fail_open_run_explicit_no_model_calls_across_callers(monkeypatch):
    """R4 Item 2:
    - `fail_open_run` defaults conservatively to `no_model_calls=False`.
    - `LocalRunExecutor._cleanup_abandoned_tasks` passes `no_model_calls=False` for dead
      RUNNING tasks that may have already invoked LLMs, keeping the `reserved_usd` floor.
    - Pre-handoff callers (`RunScope.fail_open`, `admission._settle_after_start`, and
      `error_handling.handle_workflow_error` 409 branch) explicitly pass `no_model_calls=True`."""
    from fastapi import HTTPException

    from src.server.handlers.chat import error_handling as err_mod
    from src.server.services.runs import admission as admission_mod
    from src.server.services.runs import coordinator as coord_mod
    from src.server.services.runs.executor import LocalRunExecution, LocalRunExecutor, LocalRunStatus

    _patch_finalize_side_effects(monkeypatch)
    db = _FakeBudgetAndLifecycleDB()

    @asynccontextmanager
    async def _fake_pool_conn():
        yield db.make_conn()

    monkeypatch.setattr("src.server.database.pool.get_db_connection", _fake_pool_conn)
    orig_spawn_protected = coord_mod.spawn_protected

    def _selective_spawn(coro, name, **kwargs):
        if str(name).startswith("model-handoff-"):
            coro.close()
            return asyncio.create_task(asyncio.sleep(0))
        return orig_spawn_protected(coro, name, **kwargs)

    monkeypatch.setattr(coord_mod, "spawn_protected", _selective_spawn)

    # 1. Executor dead-RUNNING cleanup -> no_model_calls=False -> conservative settlement (never $0)
    run_dead = "run-r4-dead-exec"
    user_id = "u-r4-exec"
    _seed_in_progress_run_with_credits(
        db,
        run_id=run_dead,
        thread_id="th-dead",
        user_id=user_id,
        in_flight_credits=Decimal("0"),
        reserved_usd=Decimal("1.25"),
    )
    dead_handle = RunHandle(
        run_id=run_dead,
        thread_id="th-dead",
        turn_index=0,
        attempt_no=1,
        workspace_id="ws-1",
        recorded_as=user_id,
    )
    coord_real = RunCoordinator()
    monkeypatch.setattr(
        coord_real, "post_finalize_tail", create_autospec(coord_real.post_finalize_tail, return_value=None)
    )
    monkeypatch.setattr(
        RunCoordinator, "get_instance", create_autospec(RunCoordinator.get_instance, return_value=coord_real)
    )

    executor = LocalRunExecutor()
    executor.abandoned_timeout = 60
    done_task = asyncio.create_task(asyncio.sleep(0))
    await done_task
    old_ts = datetime.now() - timedelta(hours=2)
    info = LocalRunExecution(
        thread_id="th-dead",
        run_id=run_dead,
        task=done_task,
        created_at=old_ts,
        status=LocalRunStatus.RUNNING,
        metadata={"run_handle": dead_handle},
    )
    info.active_connections = 0
    info.last_access_at = old_ts
    executor.executions[("th-dead", run_dead)] = info

    monkeypatch.setattr("src.server.services.writer_guard.guard_enabled", lambda: False)
    monkeypatch.setattr(
        "src.server.services.runs.executor.stream_writer.remote_consumer_signal",
        AsyncMock(return_value=0),
    )
    await executor._cleanup_abandoned_tasks()

    assert db.responses[run_dead]["status"] == "error"
    assert db.responses[run_dead]["usage_settled_at"] is None
    assert "dalton_no_model_calls" not in (db.responses[run_dead]["metadata"] or {})

    # Immediate reconcile must NOT settle at $0; after 30m fallback settles conservatively at $1.25.
    await budget_db.reconcile_reservations_sql(db.make_conn(), user_id, Decimal("1000"))
    assert db.reservations[run_dead]["state"] == "released"

    db.now = db.now + timedelta(minutes=31)
    await budget_db.reconcile_reservations_sql(db.make_conn(), user_id, Decimal("1000"))
    assert db.reservations[run_dead]["state"] == "settled"
    assert db.reservations[run_dead]["settle_source"] == "conservative"
    assert db.reservations[run_dead]["settled_usd"] == Decimal("1.25")

    # 2. Verify all three pre-handoff callers pass no_model_calls=True
    spy_fail_open = AsyncMock(return_value=None)
    spy_coord = SimpleNamespace(fail_open_run=spy_fail_open)
    monkeypatch.setattr(RunCoordinator, "get_instance", classmethod(lambda _cls: spy_coord))
    monkeypatch.setattr(
        "src.server.dependencies.usage_limits.release_burst_slot",
        AsyncMock(return_value=None),
    )

    # 2a. RunScope.fail_open
    scope_handle = RunHandle(run_id="run-scope", thread_id="th-scope", turn_index=0, attempt_no=1)
    scope = admission_mod.RunScope(user_id=user_id, burst_slot_id="slot-1")
    scope.attach_run(scope_handle)
    await scope.fail_open("client disconnected during setup")
    assert spy_fail_open.await_args.kwargs["no_model_calls"] is True

    # 2b. admission._settle_after_start
    spy_fail_open.reset_mock()
    start_handle = RunHandle(run_id="run-start", thread_id="th-start", turn_index=0, attempt_no=1)
    start_task = asyncio.create_task(asyncio.sleep(0, result=start_handle))
    await admission_mod._settle_after_start(start_task, [], "run-start")
    assert spy_fail_open.await_args.kwargs["no_model_calls"] is True

    # 2c. error_handling.handle_workflow_error admission conflict 409 after START
    spy_fail_open.reset_mock()
    err_handle = RunHandle(run_id="run-err409", thread_id="th-err409", turn_index=0, attempt_no=1)

    async def _noop_release() -> None:
        return None

    fake_scope = SimpleNamespace(
        handed_off=False,
        owned_run_handle=err_handle,
        release_slot=_noop_release,
    )
    fake_request = SimpleNamespace(
        request_key=None,
        workspace_id="ws-1",
        locale=None,
        messages=[],
        hitl_response=None,
        additional_context=None,
        checkpoint_id=None,
    )
    ev_list = []
    async for ev in err_mod.handle_workflow_error(
        HTTPException(status_code=409, detail={"code": "running", "message": "busy"}),
        thread_id="th-err409",
        user_id=user_id,
        workspace_id="ws-1",
        handler=None,
        token_callback=None,
        scope=fake_scope,
        start_time=0.0,
        request=fake_request,
        is_byok=True,
    ):
        ev_list.append(ev)
    assert spy_fail_open.await_args.kwargs["no_model_calls"] is True


@pytest.mark.asyncio
async def test_r4_item3_upstream_only_deployment_skips_dalton_tables_cleanly(monkeypatch):
    """R4 Item 3:
    - When `is_dalton_enabled()` is False, neither `credit_ledger.settle_abandoned` nor
      `settle_abandoned_runs_without_ptc_sql` nor `RecoveryScanner.settle_abandoned_usage`
      queries `dalton_budget_reservations` (or even `to_regclass`).
    - When `is_dalton_enabled()` is True but `dalton_budget_reservations` does not exist
      (upstream-only migrations), both sweeps skip the Dalton reservation SQL cleanly
      without raising UndefinedTable."""
    from src.server.services.runs.recovery import RecoveryScanner

    db = _FakeBudgetAndLifecycleDB(has_dalton_reservations_table=False)

    @asynccontextmanager
    async def _fake_pool_conn():
        yield db.make_conn()

    monkeypatch.setattr("src.server.database.pool.get_db_connection", _fake_pool_conn)
    old = db.now - timedelta(minutes=35)
    db.responses["run-up-only"] = {
        "conversation_response_id": "run-up-only",
        "status": "error",
        "user_id": "u-up",
        "in_flight_credits": Decimal("5"),
        "usage_settled_at": None,
        "created_at": old,
        "execution_time": 1.0,
        "metadata": {},
    }

    # Capture the real function BEFORE replacing the module attribute for the scanner spy.
    real_fallback = budget_db.settle_abandoned_runs_without_ptc_sql

    # Case A: Dalton disabled (`is_dalton_enabled() == False`) -> verbatim upstream SQL
    monkeypatch.setattr("src.dalton.settings.is_dalton_enabled", lambda: False)
    recent = db.now - timedelta(minutes=5)
    db.responses["run-up-recent"] = {
        "conversation_response_id": "run-up-recent",
        "status": "error",
        "user_id": "u-up",
        "in_flight_credits": Decimal("2"),
        "usage_settled_at": None,
        "created_at": recent,
        "execution_time": 1.0,
        "metadata": {"dalton_finalized_at": recent.isoformat().replace("+00:00", "Z")},
    }
    db.responses["run-up-zero"] = {
        "conversation_response_id": "run-up-zero",
        "status": "error",
        "user_id": "u-up",
        "in_flight_credits": Decimal("0"),
        "usage_settled_at": None,
        "created_at": old,
        "execution_time": 1.0,
        "metadata": {"dalton_finalized_at": old.isoformat().replace("+00:00", "Z")},
    }
    db.executed_sql.clear()
    # Verbatim upstream run sweep settles both positive-credit responses immediately (no 30m wait)
    # and ignores zero-credit rows.
    assert await credit_ledger_db.settle_abandoned("run") == 2
    assert db.responses["run-up-recent"]["usage_settled_at"] is not None
    assert db.responses["run-up-zero"]["usage_settled_at"] is None
    assert await credit_ledger_db.settle_abandoned("task") == 0
    assert await real_fallback(db.make_conn(), Decimal("1000")) == 0
    assert all("dalton_budget_reservations" not in s for s in db.executed_sql)
    assert all("dalton_finalized_at" not in s for s in db.executed_sql)
    assert all("to_regclass" not in s for s in db.executed_sql)

    # RecoveryScanner also skips settle_abandoned_runs_without_ptc_sql when Dalton is disabled.
    # Scope the spy strictly to this scanner assertion so `budget_db` is immediately restored.
    fallback_spy = AsyncMock(return_value=0)
    with monkeypatch.context() as mp:
        mp.setattr(budget_db, "settle_abandoned_runs_without_ptc_sql", fallback_spy)
        scanner = RecoveryScanner()
        await scanner.settle_abandoned_usage()
    fallback_spy.assert_not_awaited()
    assert budget_db.settle_abandoned_runs_without_ptc_sql is real_fallback

    # Case B: Dalton enabled (`is_dalton_enabled() == True`), but `dalton_budget_reservations` table absent
    # -> schema-qualified `to_regclass` returns None and both sweeps fall back to verbatim upstream SQL.
    monkeypatch.setattr("src.dalton.settings.is_dalton_enabled", lambda: True)
    db.responses["run-up-only"]["usage_settled_at"] = None
    db.responses["run-up-recent"]["usage_settled_at"] = None
    db.executed_sql.clear()
    assert await credit_ledger_db.settle_abandoned("run") == 2
    assert await credit_ledger_db.settle_abandoned("task") == 0
    reg_sqls = [s for s in db.executed_sql if "to_regclass" in s]
    assert len(reg_sqls) == 2
    assert all("to_regclass('dalton_budget_reservations')" in s for s in reg_sqls)
    update_sqls = [s for s in db.executed_sql if "UPDATE " in s]
    assert len(update_sqls) == 2
    assert all("dalton_budget_reservations" not in s for s in update_sqls)
    assert all("dalton_finalized_at" not in s for s in update_sqls)
    assert "r.usage_settled_at" in update_sqls[1]

    # Direct real fallback SQL checks schema-qualified `to_regclass` and returns 0 without running the CTE.
    db.executed_sql.clear()
    assert await real_fallback(db.make_conn(), Decimal("1000")) == 0
    assert any("to_regclass('dalton_budget_reservations')" in s for s in db.executed_sql)
    assert not any("WITH abandoned_no_ptc_parents" in s for s in db.executed_sql)


@pytest.mark.asyncio
async def test_r4_item4_and_item5_historical_workflow_child_and_legacy_parent_grace(monkeypatch):
    """R4 Items 4 & 5:
    - Item 4: Workflow internal children (`parent_run_id IS NULL`, `workflow_driver_run_id` set)
      are only stamped when their owning parent response has `dalton_finalized_at` or a budget
      reservation (`reserved_usd IS NOT NULL`) AND is terminal+settled; historical unreserved
      workflow children are never stamped.
    - Item 5: A legacy parent without `dalton_finalized_at` whose `usage_settled_at` was just
      stamped NOW does not block an already-aged child (`finalized_at` > 30m) for a second 30m
      window in either `credit_ledger.settle_abandoned("task")` or `budget.reconcile_reservations_sql`
      Step 2, while a young child (`finalized_at` 10m ago) still waits out its own 30m grace,
      and a post-deploy parent with recent `dalton_finalized_at` still blocks."""
    db = _FakeBudgetAndLifecycleDB()

    @asynccontextmanager
    async def _fake_pool_conn():
        yield db.make_conn()

    monkeypatch.setattr("src.server.database.pool.get_db_connection", _fake_pool_conn)

    old = db.now - timedelta(minutes=35)
    recent = db.now - timedelta(minutes=10)
    ancient = db.now - timedelta(days=60)

    # --- Item 4 setup ---
    # 4a. Historical workflow (no dalton_finalized_at, no reservation) -> child MUST stay unstamped
    db.responses["run-wf-hist"] = {
        "conversation_response_id": "run-wf-hist",
        "status": "completed",
        "user_id": None,
        "in_flight_credits": Decimal("0"),
        "usage_settled_at": ancient,
        "created_at": ancient,
        "execution_time": 1.0,
        "metadata": {},
    }
    db.subagent_runs.extend(
        [
            {
                "task_run_id": "drv-hist",
                "thread_id": "th-wf",
                "task_id": "t-drv-h",
                "parent_run_id": "run-wf-hist",
                "status": "completed",
                "cause": "init",
                "replay_facts": {"workflow_driver": True},
                "finalized_at": ancient,
                "in_flight_credits": Decimal("0"),
                "user_id": None,
                "usage_settled_at": None,
            },
            {
                "task_run_id": "wf-child-hist",
                "thread_id": "th-wf",
                "task_id": "t-ch-h",
                "parent_run_id": None,
                "status": "completed",
                "cause": "init",
                "replay_facts": {"workflow_driver_run_id": "drv-hist"},
                "finalized_at": ancient,
                "in_flight_credits": Decimal("0"),
                "user_id": None,
                "usage_settled_at": None,
            },
        ]
    )

    # 4b. Post-deploy workflow (dalton_finalized_at > 30m, settled) -> child SHOULD be stamped
    db.responses["run-wf-post"] = {
        "conversation_response_id": "run-wf-post",
        "status": "completed",
        "user_id": "u-wf",
        "in_flight_credits": Decimal("0"),
        "usage_settled_at": old,
        "created_at": old,
        "execution_time": 1.0,
        "metadata": {"dalton_finalized_at": old.isoformat().replace("+00:00", "Z")},
    }
    db.subagent_runs.extend(
        [
            {
                "task_run_id": "drv-post",
                "thread_id": "th-wf",
                "task_id": "t-drv-p",
                "parent_run_id": "run-wf-post",
                "status": "completed",
                "cause": "init",
                "replay_facts": {"workflow_driver": True},
                "finalized_at": old,
                "in_flight_credits": Decimal("0"),
                "user_id": "u-wf",
                "usage_settled_at": None,
            },
            {
                "task_run_id": "wf-child-post",
                "thread_id": "th-wf",
                "task_id": "t-ch-p",
                "parent_run_id": None,
                "status": "completed",
                "cause": "init",
                "replay_facts": {"workflow_driver_run_id": "drv-post"},
                "finalized_at": old,
                "in_flight_credits": Decimal("0"),
                "user_id": "u-wf",
                "usage_settled_at": None,
            },
        ]
    )

    # --- Item 5 setup ---
    # 5a. Legacy reserved parent (NO dalton_finalized_at, usage_settled_at = NOW) with aged + recent child
    db.responses["run-leg-grace"] = {
        "conversation_response_id": "run-leg-grace",
        "status": "error",
        "user_id": "u-leg-grace",
        "in_flight_credits": Decimal("0"),
        "usage_settled_at": db.now,
        "created_at": old,
        "execution_time": 1.0,
        "metadata": {},
    }
    db.reservations["run-leg-grace"] = {
        "reservation_id": "res-leg-grace",
        "user_id": "u-leg-grace",
        "run_ref": "run-leg-grace",
        "state": "released",
        "reserved_usd": Decimal("0.80"),
        "observed_usd": Decimal("0"),
        "settled_usd": None,
        "settle_source": None,
    }
    db.subagent_runs.extend(
        [
            {
                "task_run_id": "leg-child-aged",
                "thread_id": "th-leg",
                "task_id": "t-la",
                "parent_run_id": "run-leg-grace",
                "status": "error",
                "cause": "init",
                "replay_facts": None,
                "finalized_at": old,
                "in_flight_credits": Decimal("0"),
                "user_id": "u-leg-grace",
                "usage_settled_at": None,
            },
            {
                "task_run_id": "leg-child-recent",
                "thread_id": "th-leg",
                "task_id": "t-lr",
                "parent_run_id": "run-leg-grace",
                "status": "error",
                "cause": "init",
                "replay_facts": None,
                "finalized_at": recent,
                "in_flight_credits": Decimal("0"),
                "user_id": "u-leg-grace",
                "usage_settled_at": None,
            },
        ]
    )

    # 5b. Post-deploy parent with RECENT dalton_finalized_at (10m ago) -> MUST still block aged child
    db.responses["run-post-recent"] = {
        "conversation_response_id": "run-post-recent",
        "status": "completed",
        "user_id": "u-leg-grace",
        "in_flight_credits": Decimal("0"),
        "usage_settled_at": recent,
        "created_at": old,
        "execution_time": 1.0,
        "metadata": {"dalton_finalized_at": recent.isoformat().replace("+00:00", "Z")},
    }
    db.subagent_runs.append(
        {
            "task_run_id": "post-child-aged",
            "thread_id": "th-pr",
            "task_id": "t-pa",
            "parent_run_id": "run-post-recent",
            "status": "completed",
            "cause": "init",
            "replay_facts": None,
            "finalized_at": old,
            "in_flight_credits": Decimal("0"),
            "user_id": "u-leg-grace",
            "usage_settled_at": None,
        }
    )

    # Sweep tasks immediately (parent run-leg-grace usage_settled_at is NOW, 0m old):
    # wf-child-post + leg-child-aged settle immediately (2);
    # wf-child-hist (historical), leg-child-recent (10m old), post-child-aged (parent 10m old) do NOT!
    assert await credit_ledger_db.settle_abandoned("task") == 2
    by_tid = {s["task_run_id"]: s["usage_settled_at"] for s in db.subagent_runs}
    assert by_tid["wf-child-post"] is not None
    assert by_tid["leg-child-aged"] is not None
    assert by_tid["wf-child-hist"] is None
    assert by_tid["leg-child-recent"] is None
    assert by_tid["post-child-aged"] is None

    # Reconcile Step 2 on legacy parent: still blocked while leg-child-recent is only 10m old.
    await budget_db.reconcile_reservations_sql(db.make_conn(), "u-leg-grace", Decimal("1000"))
    assert db.reservations["run-leg-grace"]["state"] == "released"

    # Once leg-child-recent's own finalized_at is >30m old (even while parent usage_settled_at is STILL `db.now`!),
    # reconcile Step 2 settles conservatively immediately without waiting 30m from parent usage_settled_at.
    for sr in db.subagent_runs:
        if sr["task_run_id"] == "leg-child-recent":
            sr["finalized_at"] = old
    await budget_db.reconcile_reservations_sql(db.make_conn(), "u-leg-grace", Decimal("1000"))
    assert db.reservations["run-leg-grace"]["state"] == "settled"
    assert db.reservations["run-leg-grace"]["settle_source"] == "conservative"
    assert db.reservations["run-leg-grace"]["settled_usd"] == Decimal("0.80")

    # Verify generated SQL no longer references `usage_settled_at` in parent grace comparisons.
    step2_sql = next(s for s in db.executed_sql if "WITH abandoned_usage AS" in s)
    assert "cr.usage_settled_at <" not in step2_sql
    assert "cr.metadata->>'dalton_finalized_at'" in step2_sql
    task_sql = next(s for s in db.executed_sql if "UPDATE subagent_runs" in s)
    assert "r.usage_settled_at >" not in task_sql
    assert "r_wf.conversation_response_id = drv.parent_run_id" in task_sql


