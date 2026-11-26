"""Unit tests for Channel Phase 2 Batch 3 (Steps 1 & 2):
- Step 1: d058 migration (single head, offline SQL upgrade/downgrade, 3 tables,
  7 additive columns on dalton_channel_deliveries, 10 indexes, CHECK constraints,
  NOT VALID FK) + static/DB AND runtime barrier config resolution.
- Step 2: risk classification & managed-cloud create_workspace snapshot check,
  platform allowlist & capability gate before epoch, button-only toggle vs
  3-switch drain barrier, late-bind A (confirm_bound vs click self-heal + 3-way
  gate + START fail/succeed), late-bind B (settled/drain late_patch_status,
  send_in_flight > 90s, send_unknown in drain), 2 drain edge cases,
  credit_supplement pending_resubmit preservation, and BT2 live re-verification,
  dual-branch verify-dm-recipient, active-source skip, sent-source recovery,
  explicit independent confirmation, and stale attempt reclaim.

All test doubles use ``create_autospec``.
"""

from __future__ import annotations

import ast
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from src.dalton import channel_action_tokens as cat
from src.dalton import channel_deliverables as cd
from src.dalton.app.channels_router import channels_router
from src.dalton.capabilities import (
    CAPABILITY_SPECS,
    CapabilityId,
)
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database import weixin_multi_account as wx_db
from src.dalton.settings import (
    get_dalton_config,
    is_channel_bt2_link_recovery_enabled,
    is_channel_interactive_actions_enabled,
    is_channel_interrupt_ledger_enabled,
)
from src.server.database import pool as db_pool

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_D058_PATH = (
    REPO_ROOT / "src" / "dalton" / "migrations" / "versions" / "d058_channel_action_tokens.py"
)
SERVICE_TOKEN = "s" * 40


# ---------------------------------------------------------------------------
# In-memory DB harness covering d058 tables + channel_gateway / computers / workspaces
# ---------------------------------------------------------------------------


class _InMemoryBatch3DB:
    """Deterministic in-memory PostgreSQL model for Batch 3 unit tests."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
        self.runtime_config: dict[str, Any] = {
            "singleton_id": 1,
            "interactive_actions_enabled": True,
            "interrupt_ledger_enabled": True,
            "bt2_link_recovery_enabled": True,
            "config_revision": 1,
            "updated_at": self.now,
        }
        self.tokens: dict[str, dict[str, Any]] = {}
        self.resumes: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.deliveries: dict[str, dict[str, Any]] = {}
        self.inbounds: dict[tuple[str, str], dict[str, Any]] = {}
        self.background_runs: dict[str, dict[str, Any]] = {}
        self.thread_watches: dict[str, dict[str, Any]] = {}
        self.session_generations: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.identities: dict[tuple[str, str], dict[str, Any]] = {}
        self.user_settings: dict[str, dict[str, Any]] = {}
        self.chat_bindings: dict[tuple[str, str], dict[str, Any]] = {}
        self.bot_topics: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.responses: dict[str, dict[str, Any]] = {}
        self.computers: dict[str, dict[str, Any]] = {}
        self.workspaces: dict[str, dict[str, Any]] = {}
        self.weixin_accounts: dict[str, dict[str, Any]] = {}
        self.automation_runs: dict[str, dict[str, Any]] = {}
        self.lease_epoch: int = 1
        self.sql_log: list[str] = []

    def make_conn(self) -> Any:
        return _FakeConn(self)

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


def _unwrap_jsonb(val: Any) -> Any:
    return val.obj if hasattr(val, "obj") else val


class _FakeCursor:
    def __init__(self, db: _InMemoryBatch3DB) -> None:
        self.db = db
        self._one: dict[str, Any] | None = None
        self._all: list[dict[str, Any]] = []
        self.rowcount: int = 0

    async def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> None:
        norm = " ".join(sql.split()).upper()
        self.db.sql_log.append(norm)
        self._one = None
        self._all = []
        self.rowcount = 0
        p = tuple(params) if params else ()

        if norm.startswith("SET LOCAL ") or "PG_ADVISORY_XACT_LOCK" in norm:
            return
        if norm == "SELECT CLOCK_TIMESTAMP() AS DB_NOW":
            self._one = {"db_now": self.db.now}
            return

        # --- gateway lease ---
        if "FROM DALTON_CHANNEL_GATEWAY_LEASE" in norm:
            self._one = {
                "epoch": self.db.lease_epoch,
                "expires_at": self.db.now + timedelta(seconds=60),
                "db_now": self.db.now,
            }
            return

        # --- runtime config (7b-0) ---
        if "FROM DALTON_CHANNEL_INTERRUPT_RUNTIME_CONFIG" in norm:
            self._one = dict(self.db.runtime_config)
            return
        if "INSERT INTO DALTON_CHANNEL_INTERRUPT_RUNTIME_CONFIG" in norm:
            return
        if "UPDATE DALTON_CHANNEL_INTERRUPT_RUNTIME_CONFIG" in norm:
            if "INTERACTIVE_ACTIONS_ENABLED = FALSE" in norm and "BT2_LINK_RECOVERY_ENABLED = FALSE" in norm:
                self.db.runtime_config["interactive_actions_enabled"] = False
                self.db.runtime_config["interrupt_ledger_enabled"] = False
                self.db.runtime_config["bt2_link_recovery_enabled"] = False
            else:
                self.db.runtime_config["interactive_actions_enabled"] = bool(p[0])
                self.db.runtime_config["interrupt_ledger_enabled"] = bool(p[1])
                self.db.runtime_config["bt2_link_recovery_enabled"] = bool(p[2])
            self.db.runtime_config["config_revision"] += 1
            self.db.runtime_config["updated_at"] = self.db.now
            self._one = dict(self.db.runtime_config)
            self.rowcount = 1
            return

        # --- computers & workspaces ---
        if "FROM COMPUTERS" in norm:
            cid = str(p[0])
            row = self.db.computers.get(cid)
            self._one = dict(row) if row else None
            return
        if "FROM WORKSPACES" in norm:
            wid = str(p[0])
            row = self.db.workspaces.get(wid)
            self._one = dict(row) if row else None
            return

        # --- identities, settings, bindings, topics, session_generations ---
        if "FROM DALTON_CHANNEL_IDENTITIES" in norm:
            if len(p) == 1:
                uid = str(p[0])
                self._all = [
                    dict(v) for (u, _), v in self.db.identities.items() if u == uid
                ]
            else:
                uid, plat = str(p[0]), str(p[1])
                row = self.db.identities.get((uid, plat))
                self._one = dict(row) if row else None
            return
        if "FROM DALTON_CHANNEL_USER_SETTINGS" in norm:
            row = self.db.user_settings.get(str(p[0]))
            self._one = dict(row) if row else None
            return
        if "FROM DALTON_CHANNEL_CHAT_BINDINGS" in norm:
            row = self.db.chat_bindings.get((str(p[0]), str(p[1])))
            self._one = dict(row) if row else None
            return
        if "FROM DALTON_CHANNEL_BOT_TOPICS" in norm:
            row = self.db.bot_topics.get((str(p[0]), str(p[1]), str(p[2])))
            if row and ("STATUS = 'CONFIRMED'" not in norm or row.get("status") == "confirmed"):
                self._one = dict(row)
            return
        if "FROM DALTON_CHANNEL_SESSION_GENERATIONS" in norm:
            row = self.db.session_generations.get((str(p[0]), str(p[1]), str(p[2])))
            self._one = dict(row) if row else None
            return
        if "UPDATE DALTON_CHANNEL_SESSION_GENERATIONS" in norm:
            if "PENDING_INTERRUPT = NULL" in norm:
                plat, scope, uid, iid, rid = str(p[0]), str(p[1]), str(p[2]), str(p[3]), str(p[4])
                row = self.db.session_generations.get((plat, scope, uid))
                if row and isinstance(row.get("pending_interrupt"), dict):
                    pi = row["pending_interrupt"]
                    if pi.get("interrupt_id") == iid and pi.get("run_id") == rid:
                        row["pending_interrupt"] = None
                        self.rowcount = 1
            elif "SET PENDING_INTERRUPT = %S" in norm:
                pi_val, plat, scope, uid, gen = _unwrap_jsonb(p[0]), str(p[1]), str(p[2]), str(p[3]), int(p[4])
                row = self.db.session_generations.get((plat, scope, uid))
                if row and int(row.get("generation", 0)) == gen:
                    row["pending_interrupt"] = pi_val
                    self.rowcount = 1
            return

        # --- weixin accounts ---
        if "FROM DALTON_CHANNEL_WEIXIN_ACCOUNTS" in norm:
            if "ACCOUNT_ID = ANY" in norm:
                aids = set(str(x) for x in p[0])
                self._all = [dict(v) for k, v in sorted(self.db.weixin_accounts.items()) if k in aids]
            elif "ORDER BY ACCOUNT_ID ASC" in norm:
                self._all = [dict(v) for _, v in sorted(self.db.weixin_accounts.items())]
            elif len(p) == 2:
                aid, uid = str(p[0]), str(p[1])
                row = self.db.weixin_accounts.get(aid)
                if row and row.get("user_id") == uid:
                    self._one = dict(row)
            else:
                uid = str(p[0])
                matches = [dict(v) for v in self.db.weixin_accounts.values() if v.get("user_id") == uid]
                self._all = matches
                self._one = matches[0] if matches else None
            return

        # --- thread watches ---
        if "FROM DALTON_CHANNEL_THREAD_WATCHES" in norm:
            if "WHERE THREAD_ID = %S" in norm:
                row = self.db.thread_watches.get(str(p[0]))
                if row:
                    d = dict(row)
                    d["weixin_account_id_text"] = d.get("weixin_account_id")
                    self._one = d
            else:
                self._all = [dict(v) for v in self.db.thread_watches.values() if v.get("watch_active")]
            return
        if "UPDATE DALTON_CHANNEL_THREAD_WATCHES" in norm:
            if "PENDING_INTERRUPT = NULL" in norm:
                row = self.db.thread_watches.get(str(p[0]))
                if row:
                    row["pending_interrupt"] = None
                    self.rowcount = 1
            elif "SET PENDING_INTERRUPT = %S" in norm:
                pi_val, tid = _unwrap_jsonb(p[0]), str(p[1])
                row = self.db.thread_watches.get(tid)
                if row:
                    row["pending_interrupt"] = pi_val
                    self.rowcount = 1
            return

        # --- inbound events ---
        if "FROM DALTON_CHANNEL_INBOUND_EVENTS" in norm:
            if "WHERE PLATFORM = %S AND MESSAGE_ID = %S" in norm:
                row = self.db.inbounds.get((str(p[0]), str(p[1])))
                if row:
                    d = dict(row)
                    d["weixin_account_id_text"] = d.get("weixin_account_id")
                    self._one = d
            elif "RAW_PAYLOAD->>'CHAT_ID'" in norm:
                uid = str(p[0])
                for r in self.db.inbounds.values():
                    if r.get("platform") == "feishu" and r.get("dalton_user_id") == uid and r.get("chat_type") == "dm":
                        cid = (r.get("raw_payload") or {}).get("chat_id")
                        if cid:
                            self._one = {"p2p_chat_id": cid}
                            break
            else:
                self._all = [dict(v) for v in self.db.inbounds.values()]
            return
        if "UPDATE DALTON_CHANNEL_INBOUND_EVENTS" in norm:
            if "SET FALLBACK_PENDING = FALSE, RAW_PAYLOAD =" in norm and "WHERE RUN_ID = %S" in norm:
                err_code, rid = str(p[0]), str(p[1])
                for r in self.db.inbounds.values():
                    if r.get("run_id") == rid and r.get("fallback_pending"):
                        r["fallback_pending"] = False
                        rp = dict(r.get("raw_payload") or {})
                        rp["deliverables_links_terminal_error"] = err_code
                        r["raw_payload"] = rp
                        self.rowcount += 1
            elif "SET FALLBACK_PENDING = FALSE" in norm and "WHERE RUN_ID = %S" in norm:
                rid = str(p[0])
                for r in self.db.inbounds.values():
                    if r.get("run_id") == rid and r.get("fallback_pending"):
                        r["fallback_pending"] = False
                        self.rowcount += 1
            elif "HITL_RESUME_SETTLED" in norm:
                plat, mid = str(p[0]), str(p[1])
                r = self.db.inbounds.get((plat, mid))
                if r:
                    rp = dict(r.get("raw_payload") or {})
                    rp["hitl_resume_settled"] = True
                    r["raw_payload"] = rp
                    self.rowcount = 1
            elif "STATUS = 'HITL_CLAIMED'" in norm:
                cid, plat, mid = str(p[0]), str(p[1]), str(p[2])
                r = self.db.inbounds.get((plat, mid))
                if r and ("STATUS <> 'PENDING_RESUBMIT'" not in norm or r.get("status") != "pending_resubmit"):
                    r["status"] = "hitl_claimed"
                    r["fallback_pending"] = False
                    rp = dict(r.get("raw_payload") or {})
                    rp["managed_by_interrupt_resume"] = True
                    rp["claim_id"] = cid
                    r["raw_payload"] = rp
                    self.rowcount = 1
            elif "HITL_SUPERSEDED_BY" in norm:
                plat, mid = str(p[0]), str(p[1])
                r = self.db.inbounds.get((plat, mid))
                if r:
                    r["status"] = "completed"
                    r["fallback_pending"] = False
                    rp = dict(r.get("raw_payload") or {})
                    rp["hitl_superseded_by"] = "web_or_button_or_newer_text"
                    r["raw_payload"] = rp
                    self.rowcount = 1
            elif "SET STATUS = %S" in norm and "WHERE PLATFORM = %S AND MESSAGE_ID = %S" in norm:
                st, plat, mid = str(p[0]), str(p[1]), str(p[2])
                r = self.db.inbounds.get((plat, mid))
                if r:
                    r["status"] = st
                    r["fallback_pending"] = False
                    self.rowcount = 1
            elif "SET STATUS = 'FAILED'" in norm and "WHERE PLATFORM = %S AND MESSAGE_ID = %S" in norm:
                plat, mid = str(p[0]), str(p[1])
                r = self.db.inbounds.get((plat, mid))
                if r:
                    r["status"] = "failed"
                    r["fallback_pending"] = False
                    self.rowcount = 1
            return

        # --- background runs ---
        if "UPDATE DALTON_CHANNEL_BACKGROUND_RUNS" in norm and "WHERE RUN_ID = %S" in norm:
            rid = str(p[0])
            for r in self.db.background_runs.values():
                if r.get("run_id") == rid and r.get("fallback_pending"):
                    r["fallback_pending"] = False
                    self.rowcount += 1
            return
        if "FROM DALTON_CHANNEL_BACKGROUND_RUNS" in norm:
            self._all = [dict(v) for v in self.db.background_runs.values() if v.get("fallback_pending")]
            return

        # --- conversation_responses ---
        if "FROM CONVERSATION_RESPONSES" in norm:
            if "WHERE CONVERSATION_RESPONSE_ID = %S" in norm:
                row = self.db.responses.get(str(p[0]))
                self._one = dict(row) if row else None
            elif "AND REQUEST_KEY = %S" in norm:
                tid, rk = str(p[0]), str(p[1])
                for r in self.db.responses.values():
                    if str(r.get("conversation_thread_id")) == tid and str(r.get("request_key")) == rk:
                        self._one = dict(r)
                        break
            elif "AND STATUS = 'IN_PROGRESS'" in norm:
                tid = str(p[0])
                for r in self.db.responses.values():
                    if str(r.get("conversation_thread_id")) == tid and r.get("status") == "in_progress":
                        self._one = {"?column?": 1}
                        break
            return

        # --- action tokens ---
        if "INSERT INTO DALTON_CHANNEL_ACTION_TOKENS" in norm:
            if "'EPHEMERAL_CONFIRM'" in norm:
                (
                    th, parent_th, uid, init_puid, oct_, oca, osk, sgen, cda, ch_id,
                    tid, rid, iid, tier, atype, asha, sct, opts, psumm, ttl_s
                ) = p
                exp = self.db.now + timedelta(seconds=int(ttl_s))
                self.db.tokens[str(th)] = {
                    "token_hash": str(th),
                    "token_role": "ephemeral_confirm",
                    "parent_token_hash": str(parent_th),
                    "platform": "discord",
                    "dalton_user_id": str(uid),
                    "initiator_platform_user_id": str(init_puid),
                    "origin_chat_type": str(oct_),
                    "origin_chat_address": str(oca),
                    "origin_scope_key": str(osk),
                    "session_generation": int(sgen),
                    "card_chat_type": "group",
                    "card_delivery_address": str(cda),
                    "card_channel_id": str(ch_id),
                    "card_message_id": None,
                    "bind_status": "ephemeral",
                    "send_attempt_started_at": None,
                    "send_gateway_epoch": None,
                    "send_reconcile_meta": None,
                    "late_patch_status": None,
                    "late_patch_attempts": 0,
                    "late_patch_next_retry_at": None,
                    "late_patch_last_error": None,
                    "gate_verified_at": None,
                    "thread_id": str(tid),
                    "run_id": str(rid),
                    "interrupt_id": str(iid),
                    "risk_tier": str(tier),
                    "action_type": str(atype),
                    "args_sha256": str(asha),
                    "server_confirm_token": sct,
                    "options_snapshot": _unwrap_jsonb(opts),
                    "prompt_summary": _unwrap_jsonb(psumm),
                    "expires_at": exp,
                    "consumed_at": None,
                    "consumed_via": None,
                    "created_at": self.db.now,
                }
                self._one = {"expires_at": exp}
            else:
                (
                    th, role, plat, uid, init_puid, oct_, oca, osk, sgen, cct, cda,
                    tid, rid, iid, tier, atype, asha, sct, opts, psumm, ttl_s
                ) = p
                exp = self.db.now + timedelta(seconds=int(ttl_s))
                self.db.tokens[str(th)] = {
                    "token_hash": str(th),
                    "token_role": str(role),
                    "parent_token_hash": None,
                    "platform": str(plat),
                    "dalton_user_id": str(uid),
                    "initiator_platform_user_id": str(init_puid),
                    "origin_chat_type": str(oct_),
                    "origin_chat_address": str(oca),
                    "origin_scope_key": str(osk),
                    "session_generation": int(sgen),
                    "card_chat_type": str(cct),
                    "card_delivery_address": str(cda),
                    "card_channel_id": None,
                    "card_message_id": None,
                    "bind_status": "pending_bind",
                    "send_attempt_started_at": None,
                    "send_gateway_epoch": None,
                    "send_reconcile_meta": None,
                    "late_patch_status": None,
                    "late_patch_attempts": 0,
                    "late_patch_next_retry_at": None,
                    "late_patch_last_error": None,
                    "gate_verified_at": None,
                    "thread_id": str(tid),
                    "run_id": str(rid),
                    "interrupt_id": str(iid),
                    "risk_tier": str(tier),
                    "action_type": str(atype),
                    "args_sha256": str(asha),
                    "server_confirm_token": sct,
                    "options_snapshot": _unwrap_jsonb(opts),
                    "prompt_summary": _unwrap_jsonb(psumm),
                    "expires_at": exp,
                    "consumed_at": None,
                    "consumed_via": None,
                    "created_at": self.db.now,
                }
                self._one = {"expires_at": exp}
            self.rowcount = 1
            return

        if "WITH TARGET AS ( SELECT TOKEN_HASH FROM DALTON_CHANNEL_ACTION_TOKENS" in norm:
            if "BIND_STATUS = 'PENDING_BIND'" in norm:
                secs = int(p[0])
                cutoff = self.db.now - timedelta(seconds=secs)
                cnt = 0
                for t in self.db.tokens.values():
                    if t["token_role"] in ("direct", "gate") and t["bind_status"] == "pending_bind" and t["created_at"] < cutoff:
                        t["bind_status"] = "bind_expired"
                        t["consumed_via"] = t["consumed_via"] or "expired"
                        t["consumed_at"] = t["consumed_at"] or self.db.now
                        cnt += 1
                self.rowcount = cnt
            elif "LATE_PATCH_STATUS = 'PENDING'" in norm:
                plats = set(p[0])
                cnt = 0
                for t in self.db.tokens.values():
                    if t.get("late_patch_status") == "pending" and t["platform"] in plats:
                        t["late_patch_status"] = "skipped_platform_disabled"
                        t["late_patch_next_retry_at"] = None
                        cnt += 1
                self.rowcount = cnt
            else:
                plats = set(p[0])
                cnt = 0
                for t in self.db.tokens.values():
                    if t["token_role"] in ("direct", "gate") and t["platform"] in plats and t["bind_status"] in ("pending_bind", "send_in_flight", "send_unknown"):
                        t["bind_status"] = "skipped_platform_disabled"
                        t["consumed_via"] = t["consumed_via"] or "expired"
                        t["consumed_at"] = t["consumed_at"] or self.db.now
                        cnt += 1
                self.rowcount = cnt
            return

        if "FROM DALTON_CHANNEL_ACTION_TOKENS" in norm and not norm.startswith("UPDATE"):
            if "WHERE TOKEN_HASH = %S" in norm:
                row = self.db.tokens.get(str(p[0]))
                if row:
                    d = dict(row)
                    d["thread_id_text"] = d["thread_id"]
                    self._one = d
            elif "WHERE TOKEN_HASH = ANY" in norm:
                wanted = set(str(x) for x in p[0])
                self._all = [dict(self.db.tokens[k]) for k in sorted(self.db.tokens) if k in wanted]
            elif "WHERE THREAD_ID = %S::UUID AND RUN_ID = %S AND INTERRUPT_ID = %S" in norm:
                tid, rid, iid = str(p[0]), str(p[1]), str(p[2])
                rows = []
                for k in sorted(self.db.tokens):
                    t = self.db.tokens[k]
                    if t["thread_id"] == tid and t["run_id"] == rid and t["interrupt_id"] == iid:
                        d = dict(t)
                        d["thread_id_text"] = d["thread_id"]
                        rows.append(d)
                self._all = rows
            elif "WHERE LATE_PATCH_STATUS = 'PENDING'" in norm:
                plats = set(p[0])
                self._all = [
                    dict(self.db.tokens[k])
                    for k in sorted(self.db.tokens)
                    if self.db.tokens[k].get("late_patch_status") == "pending"
                    and self.db.tokens[k]["platform"] in plats
                ]
            elif "WHERE BIND_STATUS = 'SEND_IN_FLIGHT'" in norm:
                plats = set(p[0])
                self._all = [
                    dict(self.db.tokens[k])
                    for k in sorted(self.db.tokens)
                    if self.db.tokens[k].get("bind_status") == "send_in_flight"
                    and self.db.tokens[k]["platform"] in plats
                ]
            elif "WHERE TOKEN_ROLE IN ('DIRECT', 'GATE') AND CONSUMED_AT IS NULL" in norm:
                self._all = [
                    dict(self.db.tokens[k])
                    for k in sorted(self.db.tokens)
                    if self.db.tokens[k]["token_role"] in ("direct", "gate")
                    and self.db.tokens[k]["consumed_at"] is None
                    and self.db.tokens[k]["bind_status"] in ("bound", "pending_bind", "send_in_flight", "send_unknown")
                ]
            return

        if "UPDATE DALTON_CHANNEL_ACTION_TOKENS" in norm:
            th = str(p[-1])
            t = self.db.tokens.get(th)
            if not t:
                return
            if "SET BIND_STATUS = 'SEND_IN_FLIGHT'" in norm:
                t["bind_status"] = "send_in_flight"
                t["send_attempt_started_at"] = self.db.now
                t["send_gateway_epoch"] = p[0]
                t["send_reconcile_meta"] = _unwrap_jsonb(p[1])
                self._one = {"send_attempt_started_at": self.db.now}
            elif "SET BIND_STATUS = 'SEND_FAILED'" in norm:
                t["bind_status"] = "send_failed"
                t["consumed_via"] = t["consumed_via"] or ("expired" if "'EXPIRED'" in norm else "superseded")
                t["consumed_at"] = t["consumed_at"] or self.db.now
            elif "SET BIND_STATUS = 'SEND_UNKNOWN'" in norm:
                t["bind_status"] = "send_unknown"
                t["consumed_via"] = t["consumed_via"] or "expired"
                t["consumed_at"] = t["consumed_at"] or self.db.now
            elif "SET BIND_STATUS = 'BOUND'" in norm and "LATE_PATCH_STATUS = %S" in norm:
                t["bind_status"] = "bound"
                t["card_channel_id"] = p[0]
                t["card_message_id"] = p[1]
                t["consumed_via"] = p[2]
                t["consumed_at"] = t["consumed_at"] or self.db.now
                t["late_patch_status"] = p[3]
                t["late_patch_attempts"] = 0
                t["late_patch_next_retry_at"] = self.db.now if p[3] == "pending" else None
                self._one = {"consumed_at": t["consumed_at"]}
            elif "SET BIND_STATUS = 'BOUND'" in norm and "CONSUMED_VIA = %S" in norm:
                t["bind_status"] = "bound"
                t["card_channel_id"] = p[0]
                t["card_message_id"] = p[1]
                t["consumed_via"] = p[2]
                t["consumed_at"] = t["consumed_at"] or self.db.now
                self._one = {"consumed_at": t["consumed_at"]}
            elif "SET BIND_STATUS = 'BOUND'" in norm:
                t["bind_status"] = "bound"
                t["card_channel_id"] = p[0]
                t["card_message_id"] = p[1]
                t["late_patch_status"] = None
            elif "SET CARD_CHANNEL_ID = %S, CARD_MESSAGE_ID = %S" in norm:
                t["card_channel_id"] = p[0]
                t["card_message_id"] = p[1]
            elif "SET GATE_VERIFIED_AT = NOW()" in norm:
                t["gate_verified_at"] = self.db.now
            elif "SET CONSUMED_VIA = 'EXPIRED'" in norm and "LATE_PATCH_STATUS = COALESCE" in norm:
                t["consumed_via"] = "expired"
                t["consumed_at"] = self.db.now
                if t.get("late_patch_status") is None:
                    t["late_patch_status"] = p[0]
                    t["late_patch_attempts"] = 0
                    t["late_patch_next_retry_at"] = self.db.now if p[0] == "pending" else None
            elif "SET CONSUMED_VIA = 'EXPIRED'" in norm:
                t["consumed_via"] = "expired"
                t["consumed_at"] = self.db.now
            elif "SET CONSUMED_AT = NULL, CONSUMED_VIA = NULL" in norm:
                t["consumed_at"] = None
                t["consumed_via"] = None
            elif "SET CONSUMED_VIA = %S" in norm:
                t["consumed_via"] = p[0]
                t["consumed_at"] = self.db.now
            elif "SET LATE_PATCH_STATUS = 'FAILED_EXHAUSTED'" in norm:
                t["late_patch_status"] = "failed_exhausted"
                t["late_patch_attempts"] = int(p[0])
                t["late_patch_next_retry_at"] = None
                t["late_patch_last_error"] = p[1]
            elif "SET LATE_PATCH_STATUS = 'PENDING'" in norm:
                t["late_patch_status"] = "pending"
                t["late_patch_attempts"] = int(p[0])
                t["late_patch_next_retry_at"] = self.db.now + timedelta(seconds=int(p[1]))
                t["late_patch_last_error"] = p[2]
            elif "SET LATE_PATCH_STATUS = %S" in norm:
                t["late_patch_status"] = p[0]
                if len(p) == 3:
                    t["late_patch_next_retry_at"] = None
                    if p[1] is not None:
                        t["late_patch_last_error"] = p[1]
                else:
                    t["late_patch_attempts"] = 0
                    t["late_patch_next_retry_at"] = self.db.now if p[0] == "pending" else None
            self.rowcount = 1
            return

        # --- interrupt resumes ---
        if "FROM DALTON_CHANNEL_INTERRUPT_RESUMES" in norm and not norm.startswith(("UPDATE", "DELETE", "INSERT")):
            if "WHERE THREAD_ID = %S::UUID AND RUN_ID = %S AND INTERRUPT_ID = %S" in norm:
                row = self.db.resumes.get((str(p[0]), str(p[1]), str(p[2])))
                if row:
                    d = dict(row)
                    d["thread_id_text"] = d["thread_id"]
                    d["claim_id_text"] = d["claim_id"]
                    d["request_key_text"] = d["request_key"]
                    d["weixin_account_id_text"] = d.get("weixin_account_id")
                    self._one = d
            elif "WHERE REQUIRES_GATEWAY_ACK = TRUE AND CHANNEL_ACK_AT IS NULL" in norm:
                plats = set(p[0])
                matched: list[dict[str, Any]] = []
                for k in sorted(self.db.resumes):
                    r = self.db.resumes[k]
                    if not r.get("requires_gateway_ack") or r.get("channel_ack_at") is not None:
                        continue
                    origin_ok = r.get("platform") in plats and (
                        r["resume_status"] in ("pending_dispatch", "admitted")
                        or (r["resume_status"] == "terminal_failed" and not r.get("delivery_settled"))
                    )
                    gate_ok = bool(r.get("delivery_settled")) and (
                        r["resume_status"] == "terminal_failed"
                        or (r["resume_status"] == "admitted" and r.get("run_consumed_at") is not None)
                    ) and any(
                        isinstance(e, dict) and e.get("patch_status") == "pending" and e.get("platform") in plats
                        for e in (r.get("card_targets") or [])
                    )
                    if origin_ok or gate_ok:
                        matched.append(dict(r))
                self._all = matched
            else:
                self._all = [dict(r) for r in self.db.resumes.values()]
            return

        if "INSERT INTO DALTON_CHANNEL_INTERRUPT_RESUMES" in norm:
            tid, rid, iid, cid = str(p[0]), str(p[1]), str(p[2]), str(p[3])
            key = (tid, rid, iid)
            if key in self.db.resumes:
                self.rowcount = 0
                return
            if "'CHANNEL_BUTTON'" in norm:
                (
                    _, _, _, _, uid, plat, th, osk, sgen, oca, rk, rpay,
                    cda, cch, cmid, sup, ctg, lease_s
                ) = p
                self.db.resumes[key] = {
                    "thread_id": tid, "run_id": rid, "interrupt_id": iid, "claim_id": cid,
                    "settled_via": "channel_button", "settled_by_user_id": uid, "platform": plat,
                    "token_hash": th, "inbound_message_id": None, "legacy_direct": False,
                    "origin_scope_key": osk, "session_generation": sgen, "origin_chat_address": oca,
                    "weixin_account_id": None, "weixin_bot_id": None, "weixin_bot_generation": None,
                    "weixin_recipient_id": None, "request_key": str(rk), "resume_payload": _unwrap_jsonb(rpay),
                    "admitted_run_id": None, "card_delivery_address": cda, "card_channel_id": cch,
                    "card_message_id": cmid, "superseded_token_hashes": _unwrap_jsonb(sup),
                    "card_targets": _unwrap_jsonb(ctg), "resume_status": "pending_dispatch",
                    "requires_gateway_ack": True, "delivery_settled": False,
                    "lease_expires_at": self.db.now + timedelta(seconds=int(lease_s)),
                    "run_consumed_at": None, "channel_ack_at": None, "attempt_count": 0,
                    "next_retry_at": None, "last_error": None,
                }
            elif "'CHANNEL_TEXT'" in norm:
                (
                    _, _, _, _, uid, plat, mid, osk, sgen, oca, wx_aid, wx_bid, wx_gen, wx_rec,
                    rk, rpay, sup, ctg, lease_s
                ) = p
                self.db.resumes[key] = {
                    "thread_id": tid, "run_id": rid, "interrupt_id": iid, "claim_id": cid,
                    "settled_via": "channel_text", "settled_by_user_id": uid, "platform": plat,
                    "token_hash": None, "inbound_message_id": mid, "legacy_direct": False,
                    "origin_scope_key": osk, "session_generation": sgen, "origin_chat_address": oca,
                    "weixin_account_id": wx_aid, "weixin_bot_id": wx_bid, "weixin_bot_generation": wx_gen,
                    "weixin_recipient_id": wx_rec, "request_key": str(rk), "resume_payload": _unwrap_jsonb(rpay),
                    "admitted_run_id": None, "card_delivery_address": None, "card_channel_id": None,
                    "card_message_id": None, "superseded_token_hashes": _unwrap_jsonb(sup),
                    "card_targets": _unwrap_jsonb(ctg), "resume_status": "pending_dispatch",
                    "requires_gateway_ack": True, "delivery_settled": False,
                    "lease_expires_at": self.db.now + timedelta(seconds=int(lease_s)),
                    "run_consumed_at": None, "channel_ack_at": None, "attempt_count": 0,
                    "next_retry_at": None, "last_error": None,
                }
            else:
                # web / legacy_direct
                (
                    _, _, _, _, svia, uid, plat, leg_dir, rk, rpay, sup, ctg,
                    req_ack, deliv_s, lease_s, has_pending
                ) = p
                self.db.resumes[key] = {
                    "thread_id": tid, "run_id": rid, "interrupt_id": iid, "claim_id": cid,
                    "settled_via": svia, "settled_by_user_id": uid, "platform": plat,
                    "token_hash": None, "inbound_message_id": None, "legacy_direct": bool(leg_dir),
                    "origin_scope_key": None, "session_generation": None, "origin_chat_address": None,
                    "weixin_account_id": None, "weixin_bot_id": None, "weixin_bot_generation": None,
                    "weixin_recipient_id": None, "request_key": str(rk), "resume_payload": _unwrap_jsonb(rpay),
                    "admitted_run_id": None, "card_delivery_address": None, "card_channel_id": None,
                    "card_message_id": None, "superseded_token_hashes": _unwrap_jsonb(sup),
                    "card_targets": _unwrap_jsonb(ctg), "resume_status": "pending_dispatch",
                    "requires_gateway_ack": bool(req_ack), "delivery_settled": bool(deliv_s),
                    "lease_expires_at": self.db.now + timedelta(seconds=int(lease_s)),
                    "run_consumed_at": None,
                    "channel_ack_at": None if has_pending else self.db.now,
                    "attempt_count": 0, "next_retry_at": None, "last_error": None,
                }
            self.rowcount = 1
            return

        if "DELETE FROM DALTON_CHANNEL_INTERRUPT_RESUMES" in norm:
            tid, rid, iid, cid = str(p[0]), str(p[1]), str(p[2]), str(p[3])
            row = self.db.resumes.get((tid, rid, iid))
            if row and row["claim_id"] == cid:
                del self.db.resumes[(tid, rid, iid)]
                self.rowcount = 1
            return

        if "UPDATE DALTON_CHANNEL_INTERRUPT_RESUMES" in norm:
            if "WHERE ADMITTED_RUN_ID = %S AND RUN_CONSUMED_AT IS NULL" in norm:
                arid = str(p[0])
                cnt = 0
                for r in self.db.resumes.values():
                    if r.get("admitted_run_id") == arid and r.get("run_consumed_at") is None:
                        r["run_consumed_at"] = self.db.now
                        if r["settled_via"] == "web" or r.get("legacy_direct"):
                            r["delivery_settled"] = True
                        cnt += 1
                self.rowcount = cnt
                return
            if "SET RESUME_STATUS = 'ADMITTED', ADMITTED_RUN_ID = %S, LEASE_EXPIRES_AT = NULL, REQUIRES_GATEWAY_ACK = CASE" in norm:
                arid, tid, rid, iid, cid, rk = str(p[0]), str(p[1]), str(p[2]), str(p[3]), str(p[4]), str(p[5])
                r = self.db.resumes.get((tid, rid, iid))
                if r and r["claim_id"] == cid and r["request_key"] == rk and r["resume_status"] == "pending_dispatch":
                    has_pend = any(
                        isinstance(e, dict) and e.get("patch_status") == "pending"
                        for e in (r.get("card_targets") or [])
                    )
                    is_wl = r["settled_via"] == "web" or bool(r.get("legacy_direct"))
                    r["resume_status"] = "admitted"
                    r["admitted_run_id"] = arid
                    r["lease_expires_at"] = None
                    if is_wl and not has_pend:
                        r["requires_gateway_ack"] = False
                        r["delivery_settled"] = True
                        r["channel_ack_at"] = r["channel_ack_at"] or self.db.now
                    else:
                        r["requires_gateway_ack"] = True
                        r["delivery_settled"] = False
                        r["channel_ack_at"] = None
                    self.rowcount = 1
                return
            if "SET SUPERSEDED_TOKEN_HASHES = %S, CARD_TARGETS = %S, REQUIRES_GATEWAY_ACK = TRUE" in norm:
                sup, ctg, tid, rid, iid = _unwrap_jsonb(p[0]), _unwrap_jsonb(p[1]), str(p[2]), str(p[3]), str(p[4])
                r = self.db.resumes.get((tid, rid, iid))
                if r:
                    r["superseded_token_hashes"] = sup
                    r["card_targets"] = ctg
                    r["requires_gateway_ack"] = True
                    r["delivery_settled"] = False
                    r["channel_ack_at"] = None
                    self.rowcount = 1
                return
            if "SET SUPERSEDED_TOKEN_HASHES = %S, CARD_TARGETS = %S, UPDATED_AT" in norm:
                sup, ctg, tid, rid, iid = _unwrap_jsonb(p[0]), _unwrap_jsonb(p[1]), str(p[2]), str(p[3]), str(p[4])
                r = self.db.resumes.get((tid, rid, iid))
                if r:
                    r["superseded_token_hashes"] = sup
                    r["card_targets"] = ctg
                    self.rowcount = 1
                return
            if "SET DELIVERY_SETTLED = TRUE, CARD_TARGETS = %S" in norm:
                ctg, ack_now, _, tid, rid, iid, cid = _unwrap_jsonb(p[0]), bool(p[1]), bool(p[2]), str(p[3]), str(p[4]), str(p[5]), str(p[6])
                r = self.db.resumes.get((tid, rid, iid))
                if r and r["claim_id"] == cid:
                    r["delivery_settled"] = True
                    r["card_targets"] = ctg
                    r["channel_ack_at"] = (r["channel_ack_at"] or self.db.now) if ack_now else None
                    self._one = {"channel_ack_at": r["channel_ack_at"]}
                    self.rowcount = 1
                return
            if "SET CARD_TARGETS = %S, CHANNEL_ACK_AT = COALESCE" in norm:
                ctg, tid, rid, iid = _unwrap_jsonb(p[0]), str(p[1]), str(p[2]), str(p[3])
                r = self.db.resumes.get((tid, rid, iid))
                if r:
                    r["card_targets"] = ctg
                    r["channel_ack_at"] = r["channel_ack_at"] or self.db.now
                    self._one = {"channel_ack_at": r["channel_ack_at"]}
                    self.rowcount = 1
                return
            if "SET CARD_TARGETS = %S, DELIVERY_SETTLED = %S" in norm:
                ctg, ds, ack_now, _, tid, rid, iid, cid = _unwrap_jsonb(p[0]), bool(p[1]), bool(p[2]), bool(p[3]), str(p[4]), str(p[5]), str(p[6]), str(p[7])
                r = self.db.resumes.get((tid, rid, iid))
                if r and r["claim_id"] == cid:
                    r["card_targets"] = ctg
                    r["delivery_settled"] = ds
                    r["channel_ack_at"] = (r["channel_ack_at"] or self.db.now) if ack_now else None
                    self.rowcount = 1
                return
            if "SET CLAIM_ID = %S::UUID, REQUEST_KEY = %S::UUID" in norm:
                new_cid, new_rk, lease_s, b_s, tid, rid, iid, old_cid = p
                r = self.db.resumes.get((str(tid), str(rid), str(iid)))
                if r and r["claim_id"] == str(old_cid):
                    r["claim_id"] = str(new_cid)
                    r["request_key"] = str(new_rk)
                    r["admitted_run_id"] = None
                    r["resume_status"] = "pending_dispatch"
                    r["attempt_count"] = int(r.get("attempt_count") or 0) + 1
                    r["lease_expires_at"] = self.db.now + timedelta(seconds=int(lease_s))
                    r["next_retry_at"] = self.db.now + timedelta(seconds=int(b_s))
                    self.rowcount = 1
                return
            return

        # --- deliveries (BT2 + I-6 / I-7 / recover) ---
        if "FROM DALTON_CHANNEL_DELIVERIES" in norm and not norm.startswith(("INSERT", "UPDATE")):
            if "RECOVERY_KIND" in norm:
                plats = set(p[0])
                out_bt2: list[dict[str, Any]] = []
                for dk in sorted(self.db.deliveries):
                    d = self.db.deliveries[dk]
                    if d.get("platform") not in plats:
                        continue
                    st = str(d.get("status") or "")
                    ec = str(d.get("error_code") or "")
                    is_term_fail = st == "failed" and ec in cg_db.BT2_TERMINAL_FAILURE_CODES
                    if dk.startswith("deliverables-links:") and ":item:" not in dk:
                        if st in ("in_flight", "partial", "unknown") or (st == "failed" and not is_term_fail):
                            row_c = dict(d)
                            row_c["recovery_kind"] = "parent_links"
                            out_bt2.append(row_c)
                    elif d.get("bt2_enrolled") and not dk.startswith("deliverables-links:"):
                        man = d.get("deliverable_manifest") or []
                        if not man:
                            continue
                        pk = cg_db.derive_bt2_parent_links_key(dk)
                        if pk in self.db.deliveries:
                            continue
                        if st == "in_flight":
                            row_c = dict(d)
                            row_c["recovery_kind"] = "source_missing_parent"
                            out_bt2.append(row_c)
                        elif (st in ("sent", "partial", "unknown") or (st == "failed" and not is_term_fail)):
                            if cg_db._filter_unconfirmed_bt2_manifest(man, d.get("files_status")):
                                row_c = dict(d)
                                row_c["recovery_kind"] = "source_missing_parent"
                                out_bt2.append(row_c)
                self._all = out_bt2
                return
            if "WHERE DELIVERY_KEY = ANY" in norm:
                wanted = set(str(x) for x in p[0])
                rows = []
                for k in sorted(self.db.deliveries):
                    if k in wanted:
                        d = dict(self.db.deliveries[k])
                        d["thread_id_text"] = d.get("thread_id")
                        d["execution_id_text"] = d.get("execution_id")
                        d["weixin_account_id_text"] = d.get("weixin_account_id")
                        d["claim_attempt_id_text"] = d.get("claim_attempt_id")
                        rows.append(d)
                self._all = rows
                return
            if "WHERE DELIVERY_KEY = %S" in norm:
                row = self.db.deliveries.get(str(p[0]))
                self._one = dict(row) if row else None
                return
            if "WHERE DELIVERY_KEY LIKE %S" in norm:
                prefix = str(p[0]).rstrip("%")
                self._all = [
                    dict(v) for k, v in sorted(self.db.deliveries.items()) if k.startswith(prefix)
                ]
                return
            if "WHERE RUN_ID = %S AND IS_CURRENT = TRUE AND DELIVERY_KEY <> %S" in norm:
                rid, excl = str(p[0]), str(p[1])
                self._all = [
                    dict(v)
                    for k, v in sorted(self.db.deliveries.items())
                    if v.get("run_id") == rid and v.get("is_current") and k != excl
                ]
                return
            if "WHERE STATUS = 'IN_FLIGHT'" in norm:
                plats = set(p[0]) if p else {"feishu", "discord", "weixin"}
                self._all = [
                    {"delivery_key": k}
                    for k, v in sorted(self.db.deliveries.items())
                    if v.get("status") == "in_flight" and v.get("platform") in plats
                ]
                return
            return

        if "INSERT INTO DALTON_CHANNEL_DELIVERIES" in norm:
            dk = str(p[0])
            existing = self.db.deliveries.get(dk)
            if "'FAILED', 0, 0" in norm:
                # BT2 terminal denied upsert
                (
                    _, uid, tid, rid, exec_id, is_cur, plat, taddr, fs, ecode, emsg,
                    scope, gen, act_o, pdm, src_k, man, ameta, aid, ep
                ) = p
                self.db.deliveries[dk] = {
                    "delivery_key": dk, "kind": "fallback", "user_id": uid, "thread_id": tid,
                    "run_id": rid, "execution_id": exec_id, "is_current": is_cur,
                    "has_non_empty_text": False, "platform": plat, "target_address": taddr,
                    "status": "failed", "total_chunks": 0, "sent_chunks": 0,
                    "platform_message_ids": [], "files_status": _unwrap_jsonb(fs),
                    "error_code": ecode, "error_message": emsg,
                    "source_scope_key": scope, "source_generation": gen, "activity_origin": act_o,
                    "delivery_attempt": 1 if existing is None else existing["delivery_attempt"],
                    "dm_recipient_platform_user_id": pdm,
                    "bt2_enrolled": True, "source_delivery_key": src_k,
                    "deliverable_manifest": _unwrap_jsonb(man) if man is not None else None,
                    "authorized_target_meta": _unwrap_jsonb(ameta) if ameta is not None else None,
                    "claim_attempt_id": aid, "claim_gateway_epoch": ep,
                    "claim_lease_expires_at": None,
                }
            elif "BT2_ENROLLED" in norm and "'FALLBACK'" in norm:
                # BT2 parent/item claim upsert
                (
                    _, uid, tid, rid, exec_id, is_cur, plat, taddr, fs, scope, gen, act_o,
                    wx_aid, wx_bid, wx_gen, wx_rec, pdm, conf_th, src_k, man, ameta, aid, ep, lsec
                ) = p
                next_att = 1 if existing is None else int(existing["delivery_attempt"]) + 1
                self.db.deliveries[dk] = {
                    "delivery_key": dk, "kind": "fallback", "user_id": uid, "thread_id": tid,
                    "run_id": rid, "execution_id": exec_id, "is_current": is_cur,
                    "has_non_empty_text": False, "platform": plat, "target_address": taddr,
                    "status": "in_flight", "total_chunks": 0, "sent_chunks": 0,
                    "platform_message_ids": [],
                    "files_status": existing["files_status"] if existing and existing.get("files_status") else _unwrap_jsonb(fs),
                    "error_code": None, "error_message": None,
                    "source_scope_key": scope, "source_generation": gen, "activity_origin": act_o,
                    "delivery_attempt": next_att,
                    "weixin_account_id": wx_aid, "weixin_bot_id": wx_bid,
                    "weixin_bot_generation": wx_gen, "weixin_recipient_id": wx_rec,
                    "dm_recipient_platform_user_id": (existing.get("dm_recipient_platform_user_id") if existing else None) or pdm,
                    "confirmed_thread_address": conf_th,
                    "bt2_enrolled": True,
                    "source_delivery_key": (existing.get("source_delivery_key") if existing else None) or src_k,
                    "deliverable_manifest": (existing.get("deliverable_manifest") if existing else None) or (_unwrap_jsonb(man) if man is not None else None),
                    "authorized_target_meta": _unwrap_jsonb(ameta),
                    "claim_attempt_id": aid, "claim_gateway_epoch": ep,
                    "claim_lease_expires_at": self.db.now + timedelta(seconds=int(lsec)),
                }
            elif "BT2_ENROLLED" in norm:
                # File-carrying source delivery upsert
                (
                    _, kind, uid, tid, rid, exec_id, is_cur, has_txt, plat, taddr, tchunks, fs,
                    scope, gen, act_o, wx_aid, wx_bid, wx_gen, wx_rec, pdm,
                    bt2_enr, src_k, man, ameta, aid, ep, lsec, _replay
                ) = p
                next_att = 1 if existing is None else int(existing["delivery_attempt"]) + 1
                self.db.deliveries[dk] = {
                    "delivery_key": dk, "kind": kind, "user_id": uid, "thread_id": tid,
                    "run_id": rid, "execution_id": exec_id, "is_current": is_cur,
                    "has_non_empty_text": has_txt, "platform": plat, "target_address": taddr,
                    "status": "in_flight", "total_chunks": tchunks, "sent_chunks": 0,
                    "platform_message_ids": [],
                    "files_status": existing["files_status"] if existing and existing.get("files_status") else _unwrap_jsonb(fs),
                    "error_code": None, "error_message": None,
                    "source_scope_key": scope, "source_generation": gen, "activity_origin": act_o,
                    "delivery_attempt": next_att,
                    "weixin_account_id": wx_aid, "weixin_bot_id": wx_bid,
                    "weixin_bot_generation": wx_gen, "weixin_recipient_id": wx_rec,
                    "dm_recipient_platform_user_id": pdm,
                    "bt2_enrolled": bool(existing.get("bt2_enrolled")) if existing else bool(bt2_enr),
                    "source_delivery_key": src_k,
                    "deliverable_manifest": (existing.get("deliverable_manifest") if existing else None) or _unwrap_jsonb(man),
                    "authorized_target_meta": (existing.get("authorized_target_meta") if existing else None) or _unwrap_jsonb(ameta),
                    "claim_attempt_id": aid, "claim_gateway_epoch": ep,
                    "claim_lease_expires_at": self.db.now + timedelta(seconds=int(lsec)),
                }
            self.rowcount = 1
            return

        if "UPDATE DALTON_CHANNEL_DELIVERIES" in norm:
            if "WHERE DELIVERY_KEY = %S AND STATUS = 'IN_FLIGHT'" in norm:
                row = self.db.deliveries.get(str(p[0]))
                if row and row["status"] == "in_flight":
                    row["status"] = "unknown"
                    row["error_code"] = row.get("error_code") or "attempt_lease_expired"
                    self.rowcount = 1
                return
            if "SET STATUS = 'FAILED', ERROR_CODE = 'MAX_ATTEMPTS_EXCEEDED'" in norm:
                has_fs, fs_val, dk = bool(p[0]), _unwrap_jsonb(p[1]), str(p[2])
                row = self.db.deliveries.get(dk)
                if row:
                    row["status"] = "failed"
                    row["error_code"] = "max_attempts_exceeded"
                    if has_fs:
                        row["files_status"] = fs_val
                    self.rowcount = 1
                return
            if "CLAIM_LEASE_EXPIRES_AT = NOW() +" in norm:
                sc, tc, mids, fs, fresp, st, ec, em, lsec, dk = p
                row = self.db.deliveries.get(str(dk))
                if row:
                    row["sent_chunks"] = sc
                    row["total_chunks"] = tc
                    row["platform_message_ids"] = _unwrap_jsonb(mids)
                    row["files_status"] = _unwrap_jsonb(fs)
                    row["first_response_json"] = _unwrap_jsonb(fresp) if fresp is not None else None
                    row["status"] = st
                    if ec is not None:
                        row["error_code"] = ec
                    if em is not None:
                        row["error_message"] = em
                    row["claim_lease_expires_at"] = self.db.now + timedelta(seconds=int(lsec))
                    self.rowcount = 1
                return
            if "SET SENT_CHUNKS = %S" in norm:
                sc, tc, mids, fs, fresp, st, st2, is_bp, ec, em, dk = p
                row = self.db.deliveries.get(str(dk))
                if row:
                    row["sent_chunks"] = sc
                    row["total_chunks"] = tc
                    row["platform_message_ids"] = _unwrap_jsonb(mids)
                    row["files_status"] = _unwrap_jsonb(fs)
                    row["first_response_json"] = _unwrap_jsonb(fresp) if fresp is not None else None
                    row["status"] = st
                    if st2 == "sent" and is_bp:
                        row["error_code"] = None
                    elif ec is not None:
                        row["error_code"] = ec
                    if em is not None:
                        row["error_message"] = em
                    self.rowcount = 1
                return
            if "SET STATUS = 'UNKNOWN', ERROR_CODE = 'GATEWAY_RESTARTED'" in norm:
                keys = set(str(k) for k in p[0])
                out = []
                for k in sorted(keys):
                    row = self.db.deliveries.get(k)
                    if row and row["status"] == "in_flight":
                        row["status"] = "unknown"
                        row["error_code"] = "gateway_restarted"
                        out.append(dict(row))
                self._all = out
                self.rowcount = len(out)
                return

        if "WITH TARGET AS ( SELECT DELIVERY_KEY" in norm:
            if "ATTEMPT_LEASE_EXPIRED" in norm:
                cnt = 0
                for d in self.db.deliveries.values():
                    if (
                        d.get("status") == "in_flight"
                        and d.get("claim_lease_expires_at") is not None
                        and d["claim_lease_expires_at"] < self.db.now
                        and (d["delivery_key"].startswith("deliverables-links:") or d.get("bt2_enrolled"))
                    ):
                        d["status"] = "unknown"
                        d["error_code"] = "attempt_lease_expired"
                        cnt += 1
                self.rowcount = cnt
            elif "MAX_ATTEMPTS_EXCEEDED" in norm:
                max_att = int(p[0])
                out = []
                for d in self.db.deliveries.values():
                    if (
                        d["delivery_key"].startswith("deliverables-links:")
                        and int(d.get("delivery_attempt") or 0) >= max_att
                        and (
                            d["status"] in ("partial", "unknown")
                            or (
                                d["status"] == "in_flight"
                                and d.get("claim_lease_expires_at") is not None
                                and d["claim_lease_expires_at"] < self.db.now
                            )
                        )
                    ):
                        d["status"] = "failed"
                        d["error_code"] = "max_attempts_exceeded"
                        out.append(dict(d))
                self._all = out
                self.rowcount = len(out)
            return

    async def fetchone(self) -> dict[str, Any] | None:
        return self._one

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._all)


class _FakeConn:
    def __init__(self, db: _InMemoryBatch3DB) -> None:
        self._db = db

    @asynccontextmanager
    async def transaction(self):
        yield

    @asynccontextmanager
    async def cursor(self, **_kwargs: Any):
        yield self._db.cursor()


def _install_db(
    monkeypatch: pytest.MonkeyPatch,
    db: _InMemoryBatch3DB,
    *,
    pending_interrupts: dict[str, dict[str, Any]] | None = None,
    approval_snapshots: dict[str, Any] | None = None,
    user_tier: str = "internal",
    member_status: str = "active",
) -> None:
    @asynccontextmanager
    async def _fake_gdc(existing_conn: Any = None):
        if existing_conn is not None:
            yield existing_conn
            return
        yield db.make_conn()

    gdc_mock = create_autospec(db_pool.get_db_connection, side_effect=_fake_gdc)
    monkeypatch.setattr(db_pool, "get_db_connection", gdc_mock)
    monkeypatch.setattr(cat, "get_db_connection", gdc_mock)
    monkeypatch.setattr(cg_db, "get_db_connection", gdc_mock)
    monkeypatch.setattr(wx_db, "get_db_connection", gdc_mock)

    live_mock = create_autospec(
        cat.load_live_pending_interrupts,
        return_value=(dict(pending_interrupts or {}), dict(approval_snapshots or {})),
    )
    monkeypatch.setattr(cat, "load_live_pending_interrupts", live_mock)

    tier_mock = create_autospec(cat.get_user_tier, return_value=user_tier)
    monkeypatch.setattr(cat, "get_user_tier", tier_mock)
    monkeypatch.setattr(cg_db, "get_user_tier", tier_mock)

    roster_mock = create_autospec(
        cg_db.check_team_member_status_live, return_value=member_status
    )
    monkeypatch.setattr(cg_db, "check_team_member_status_live", roster_mock)
    monkeypatch.setattr(cat, "check_team_member_status_live", roster_mock)

    monkeypatch.setenv("APP_ENV", "dalton")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_GATEWAY", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_FEISHU", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_DISCORD", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_WEIXIN", "true")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    for var in (
        "DALTON_CHANNEL_INTERACTIVE_ACTIONS_ENABLED",
        "DALTON_CHANNEL_INTERRUPT_LEDGER_ENABLED",
        "DALTON_CHANNEL_BT2_LINK_RECOVERY_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    get_dalton_config.cache_clear()


# ===========================================================================
# STEP 1: Migration d058 + Runtime Barrier Config
# ===========================================================================


def test_d058_single_head_and_no_business_imports() -> None:
    alembic_cfg = Config(str(REPO_ROOT / "src" / "dalton" / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(REPO_ROOT / "src" / "dalton" / "migrations"))
    script = ScriptDirectory.from_config(alembic_cfg)
    heads = script.get_heads()
    assert heads == ["d058"], f"Expected single head ['d058'], got {heads}"
    rev = script.get_revision("d058")
    assert rev is not None
    assert rev.down_revision == "d055"

    tree = ast.parse(MIGRATION_D058_PATH.read_text(encoding="utf-8"))
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "src" or alias.name.startswith("src."):
                    bad.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "src" or mod.startswith("src."):
                bad.append(mod)
    assert bad == []


def test_d058_alembic_offline_sql_upgrade_and_downgrade() -> None:
    up = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "src/dalton/alembic.ini",
            "upgrade",
            "d055:d058",
            "--sql",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert up.returncode == 0, f"alembic upgrade --sql failed:\n{up.stderr}"
    sql = up.stdout
    assert "SET LOCAL lock_timeout = '5s'" in sql

    # 3 new tables
    for tbl in (
        "dalton_channel_interrupt_runtime_config",
        "dalton_channel_action_tokens",
        "dalton_channel_interrupt_resumes",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {tbl}" in sql

    # 7 additive columns on dalton_channel_deliveries
    assert "ALTER TABLE dalton_channel_deliveries" in sql
    for col in (
        "bt2_enrolled",
        "source_delivery_key",
        "deliverable_manifest",
        "authorized_target_meta",
        "claim_attempt_id",
        "claim_gateway_epoch",
        "claim_lease_expires_at",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in sql

    # 10 indexes (1 composite + 9 partial)
    expected_indexes = (
        "idx_dalton_channel_action_tokens_interrupt",
        "idx_dalton_channel_action_tokens_expires",
        "idx_dalton_channel_action_tokens_pending_bind",
        "idx_dalton_channel_action_tokens_late_patch",
        "idx_dalton_channel_interrupt_resumes_unadmitted",
        "idx_dalton_channel_interrupt_resumes_unconsumed",
        "idx_dalton_channel_interrupt_resumes_unacked",
        "idx_dalton_channel_interrupt_resumes_admitted_run",
        "idx_dalton_channel_interrupt_resumes_inbound",
        "idx_dalton_channel_deliveries_links_recovery",
    )
    assert len(expected_indexes) == 10
    for idx in expected_indexes:
        assert f"CREATE INDEX IF NOT EXISTS {idx}" in sql

    # Key CHECK constraints and NOT VALID FK
    assert "CHECK (interrupt_ledger_enabled = TRUE OR interactive_actions_enabled = FALSE)" in sql
    assert "bind_status <> 'send_in_flight'" in sql
    assert "send_attempt_started_at IS NOT NULL" in sql
    assert "late_patch_status IS NULL" in sql
    assert "NOT (card_targets @> '[{\"patch_status\": \"pending\"}]'::jsonb)" in sql
    assert "fk_dalton_interrupt_resumes_wx_account" in sql
    assert "ON DELETE SET NULL NOT VALID" in sql
    # Zero upstream table modifications
    for upstream_tbl in ("users", "workspaces", "conversation_threads", "automations"):
        assert f"ALTER TABLE {upstream_tbl}" not in sql

    down = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "src/dalton/alembic.ini",
            "downgrade",
            "d058:d055",
            "--sql",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert down.returncode == 0, f"alembic downgrade --sql failed:\n{down.stderr}"
    for idx in expected_indexes:
        assert f"DROP INDEX IF EXISTS {idx}" in down.stdout
    for tbl in (
        "dalton_channel_interrupt_resumes",
        "dalton_channel_action_tokens",
        "dalton_channel_interrupt_runtime_config",
    ):
        assert f"DROP TABLE IF EXISTS {tbl}" in down.stdout


@pytest.mark.asyncio
async def test_runtime_barrier_static_and_db_and_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _InMemoryBatch3DB()
    _install_db(monkeypatch, db)

    assert is_channel_interactive_actions_enabled() is True
    assert is_channel_interrupt_ledger_enabled() is True
    assert is_channel_bt2_link_recovery_enabled() is True

    # Static AND DB = all True
    eff = await cat.read_effective_interrupt_runtime_config_in_tx(db.cursor())
    assert eff["interactive_actions_enabled"] is True
    assert eff["interrupt_ledger_enabled"] is True
    assert eff["bt2_link_recovery_enabled"] is True

    # Static interactive=false AND DB=true -> effective False
    monkeypatch.setenv("DALTON_CHANNEL_INTERACTIVE_ACTIONS_ENABLED", "false")
    eff2 = await cat.read_effective_interrupt_runtime_config_in_tx(db.cursor())
    assert eff2["interactive_actions_enabled"] is False
    assert eff2["interrupt_ledger_enabled"] is True
    assert eff2["bt2_link_recovery_enabled"] is True

    # Static ledger=false forces interactive=false even when static interactive=true
    monkeypatch.setenv("DALTON_CHANNEL_INTERACTIVE_ACTIONS_ENABLED", "true")
    monkeypatch.setenv("DALTON_CHANNEL_INTERRUPT_LEDGER_ENABLED", "false")
    eff3 = await cat.read_effective_interrupt_runtime_config_in_tx(db.cursor())
    assert eff3["interrupt_ledger_enabled"] is False
    assert eff3["interactive_actions_enabled"] is False

    # Static bt2=false AND DB=true -> effective bt2=False
    monkeypatch.delenv("DALTON_CHANNEL_INTERRUPT_LEDGER_ENABLED", raising=False)
    monkeypatch.setenv("DALTON_CHANNEL_BT2_LINK_RECOVERY_ENABLED", "false")
    eff4 = await cat.read_effective_interrupt_runtime_config_in_tx(db.cursor())
    assert eff4["bt2_link_recovery_enabled"] is False
    assert eff4["interrupt_ledger_enabled"] is True


# ===========================================================================
# STEP 2: Risk Classification, Managed Cloud create_workspace, Platform Gates
# ===========================================================================


@pytest.mark.asyncio
async def test_risk_classification_and_managed_cloud_create_workspace_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _InMemoryBatch3DB()
    _install_db(monkeypatch, db)
    uid = "user-1"
    own_ws = str(uuid4())
    shared_ws = str(uuid4())
    cloud_cid = str(uuid4())
    ssh_cid = str(uuid4())
    db.workspaces[own_ws] = {"workspace_id": own_ws, "user_id": uid}
    db.workspaces[shared_ws] = {"workspace_id": shared_ws, "user_id": "colleague-2"}
    db.computers[cloud_cid] = {"user_id": uid, "kind": "daytona", "status": "running"}
    db.computers[ssh_cid] = {"user_id": uid, "kind": "ssh", "status": "running"}
    db.identities[(uid, "feishu")] = {"user_id": uid, "platform": "feishu", "platform_user_id": "ou_self"}

    cur = db.cursor()
    # Multi-interrupt -> L3
    tier, _, _ = await cat.classify_interrupt_for_channel(
        cur,
        interrupt_value={"kind": "ask_user_question", "action_requests": [{"type": "ask_user_question"}]},
        pending_interrupt_count=2,
        dalton_user_id=uid,
    )
    assert tier == "L3"

    # Outer credit_pause -> credit_pause
    tier, _, _ = await cat.classify_interrupt_for_channel(
        cur,
        interrupt_value={"kind": "credit_pause", "action_requests": [{"type": "credit_pause"}]},
        pending_interrupt_count=1,
        dalton_user_id=uid,
    )
    assert tier == "credit_pause"

    # Outer high-risk kinds -> L3
    for high_kind in ("order_approval", "publish_approval", "custom_unknown_kind"):
        tier, _, _ = await cat.classify_interrupt_for_channel(
            cur,
            interrupt_value={"kind": high_kind, "action_requests": [{"type": "ptc_agent", "workspace_id": own_ws}]},
            pending_interrupt_count=1,
            dalton_user_id=uid,
        )
        assert tier == "L3"

    # L0 ask_user_question
    tier, atype, _ = await cat.classify_interrupt_for_channel(
        cur,
        interrupt_value={"kind": "ask_user_question", "action_requests": [{"type": "ask_user_question", "question": "q"}]},
        pending_interrupt_count=1,
        dalton_user_id=uid,
    )
    assert (tier, atype) == ("L0", "ask_user_question")

    # L1 create_workspace (both computer_is_own_machine is False + cloud computer)
    tcid = "tc-cloud-1"
    action_cloud = {
        "type": "create_workspace",
        "tool_call_id": tcid,
        "workspace_name": "NVDA",
        "workspace_description": "",
        "computer_id": cloud_cid,
        "computer_name": "Cloud",
        "bootstrap_cloud_primary": False,
        "computer_is_own_machine": False,
    }
    snap_cloud = {
        "action": "create_workspace",
        "workspace_name": "NVDA",
        "workspace_description": "",
        "computer_id": cloud_cid,
        "computer_name": "Cloud",
        "computer_kind": "daytona",
        "bootstrap_cloud_primary": False,
        "computer_is_own_machine": False,
    }
    tier, _, _ = await cat.classify_interrupt_for_channel(
        cur,
        interrupt_value={"kind": "hitl_confirm", "action_requests": [action_cloud]},
        pending_interrupt_count=1,
        dalton_user_id=uid,
        approval_snapshots={tcid: snap_cloud},
    )
    assert tier == "L1"

    # Missing or None computer_is_own_machine -> L3 (strict literal False!)
    bad_action = {**action_cloud, "computer_is_own_machine": None}
    tier, _, _ = await cat.classify_interrupt_for_channel(
        cur,
        interrupt_value={"kind": "hitl_confirm", "action_requests": [bad_action]},
        pending_interrupt_count=1,
        dalton_user_id=uid,
        approval_snapshots={tcid: {**snap_cloud, "computer_is_own_machine": None}},
    )
    assert tier == "L3"

    # SSH own machine -> L3
    ssh_action = {**action_cloud, "computer_id": ssh_cid}
    ssh_snap = {**snap_cloud, "computer_id": ssh_cid, "computer_kind": "daytona"}
    tier, _, _ = await cat.classify_interrupt_for_channel(
        cur,
        interrupt_value={"kind": "hitl_confirm", "action_requests": [ssh_action]},
        pending_interrupt_count=1,
        dalton_user_id=uid,
        approval_snapshots={tcid: ssh_snap},
    )
    assert tier == "L3"

    # Cloud primary bootstrap (computer_id=None + both bootstrap_cloud_primary is True) -> L1
    boot_action = {**action_cloud, "computer_id": None, "bootstrap_cloud_primary": True}
    boot_snap = {**snap_cloud, "computer_id": None, "bootstrap_cloud_primary": True}
    tier, _, _ = await cat.classify_interrupt_for_channel(
        cur,
        interrupt_value={"kind": "hitl_confirm", "action_requests": [boot_action]},
        pending_interrupt_count=1,
        dalton_user_id=uid,
        approval_snapshots={tcid: boot_snap},
    )
    assert tier == "L1"

    # L1 self DM notify vs L2 group notify
    tier, _, _ = await cat.classify_interrupt_for_channel(
        cur,
        interrupt_value={"kind": "hitl_confirm", "action_requests": [{"type": "dalton_notify_test", "targets": ["feishu:@me"]}]},
        pending_interrupt_count=1,
        dalton_user_id=uid,
    )
    assert tier == "L1"
    tier, _, _ = await cat.classify_interrupt_for_channel(
        cur,
        interrupt_value={"kind": "hitl_confirm", "action_requests": [{"type": "dalton_notify_test", "targets": ["feishu:oc_grp"]}]},
        pending_interrupt_count=1,
        dalton_user_id=uid,
    )
    assert tier == "L2"


@pytest.mark.asyncio
async def test_platform_allowlist_and_capability_gate_before_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _InMemoryBatch3DB()
    _install_db(monkeypatch, db)

    app = FastAPI()
    app.include_router(channels_router, prefix="/api/v1/dalton")
    client = TestClient(app)

    # Unknown platform rejected with 400 invalid_platform even without X-Gateway-Epoch
    resp = client.post(
        "/api/v1/dalton/channels/internal/action-tokens/mint",
        headers={"X-Service-Token": SERVICE_TOKEN},
        json={
            "platform": "telegram",
            "dalton_user_id": "u1",
            "initiator_platform_user_id": "tg_1",
            "origin_chat_type": "dm",
            "origin_chat_address": "telegram:@me",
            "origin_scope_key": "dm:tg_1",
            "session_generation": 1,
            "thread_id": str(uuid4()),
            "run_id": "r1",
            "interrupt_id": "i1",
            "interrupt_value": {},
        },
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_platform"

    # Disabled Feishu sub-capability returns 404 BEFORE epoch check (malformed epoch ignored!)
    monkeypatch.setenv(CAPABILITY_SPECS[CapabilityId.CHANNEL_FEISHU].env_var, "false")
    resp2 = client.post(
        "/api/v1/dalton/channels/internal/action-tokens/mint",
        headers={"X-Service-Token": SERVICE_TOKEN, "X-Gateway-Epoch": "not-an-int"},
        json={
            "platform": "feishu",
            "dalton_user_id": "u1",
            "initiator_platform_user_id": "ou_1",
            "origin_chat_type": "dm",
            "origin_chat_address": "feishu:@me",
            "origin_scope_key": "dm:ou_1",
            "session_generation": 1,
            "thread_id": str(uuid4()),
            "run_id": "r1",
            "interrupt_id": "i1",
            "interrupt_value": {},
        },
    )
    assert resp2.status_code == 404

    # All 3 platform capabilities off -> GET /internal/recover returns 404 BEFORE epoch parsing
    monkeypatch.setenv(CAPABILITY_SPECS[CapabilityId.CHANNEL_DISCORD].env_var, "false")
    monkeypatch.setenv(CAPABILITY_SPECS[CapabilityId.CHANNEL_WEIXIN].env_var, "false")
    resp3 = client.get(
        "/api/v1/dalton/channels/internal/recover?mode=startup",
        headers={"X-Service-Token": SERVICE_TOKEN, "X-Gateway-Epoch": "bad"},
    )
    assert resp3.status_code == 404


# ===========================================================================
# STEP 2 REQUIRED TEST: Button-Only Toggle vs 3-Switch Drain Separation
# ===========================================================================


@pytest.mark.asyncio
async def test_button_only_toggle_vs_three_switch_drain_separation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _InMemoryBatch3DB()
    tid = str(uuid4())
    uid = "u-alice"
    _install_db(
        monkeypatch,
        db,
        pending_interrupts={
            "int-1": {
                "id": "int-1",
                "value": {
                    "kind": "ask_user_question",
                    "action_requests": [{"type": "ask_user_question", "question": "Pick one"}],
                },
            }
        },
    )
    db.identities[(uid, "feishu")] = {"user_id": uid, "platform": "feishu", "platform_user_id": "ou_alice"}
    db.session_generations[("feishu", "dm:ou_alice", uid)] = {"generation": 1, "pending_interrupt": None}

    # Mint & bind a token while enabled
    minted = await cat.mint_channel_action_token(
        platform="feishu",
        dalton_user_id=uid,
        initiator_platform_user_id="ou_alice",
        origin_chat_type="dm",
        origin_chat_address="feishu:@me",
        origin_scope_key="dm:ou_alice",
        session_generation=1,
        thread_id=tid,
        run_id="run-1",
        interrupt_id="int-1",
        interrupt_value={
            "kind": "ask_user_question",
            "action_requests": [{"type": "ask_user_question", "question": "Pick one"}],
        },
        gateway_epoch=1,
    )
    th = minted["token_hash"]
    await cat.bind_channel_action_card(
        approval_nonce=minted["approval_nonce"],
        phase="prepare_send",
        gateway_epoch=1,
    )
    await cat.bind_channel_action_card(
        approval_nonce=minted["approval_nonce"],
        phase="confirm_bound",
        card_channel_id="oc_dm_1",
        card_message_id="om_card_1",
        gateway_epoch=1,
    )

    # Path A: turn OFF interactive_actions_enabled ONLY -> token & BT2 stay untouched!
    res_a = await cat.set_interrupt_runtime_config(interactive_actions_enabled=False)
    assert res_a["mode"] == "config_update"
    assert res_a["interactive_actions_enabled"] is False
    assert res_a["interrupt_ledger_enabled"] is True
    assert res_a["bt2_link_recovery_enabled"] is True
    assert db.tokens[th]["consumed_at"] is None
    assert db.tokens[th]["late_patch_status"] is None

    # mint now returns text_or_web_fallback
    mint_off = await cat.mint_channel_action_token(
        platform="feishu",
        dalton_user_id=uid,
        initiator_platform_user_id="ou_alice",
        origin_chat_type="dm",
        origin_chat_address="feishu:@me",
        origin_scope_key="dm:ou_alice",
        session_generation=1,
        thread_id=tid,
        run_id="run-1",
        interrupt_id="int-1",
        interrupt_value={"kind": "ask_user_question", "action_requests": [{"type": "ask_user_question"}]},
        gateway_epoch=1,
    )
    assert mint_off["interactive_enabled"] is False
    assert mint_off["token"] is None

    # consume on unconsumed token returns 409 interactive_actions_disabled (can_patch_terminal=False)
    with pytest.raises(HTTPException) as exc_info:
        await cat.consume_channel_action_token(
            approval_nonce=minted["approval_nonce"],
            decision="approve",
            clicker_platform_user_id="ou_alice",
            channel_id="oc_dm_1",
            message_id="om_card_1",
            answer_text="A",
            gateway_epoch=1,
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "interactive_actions_disabled"
    assert exc_info.value.detail["can_patch_terminal"] is False

    # Seed pre-barrier bt2_enrolled=True source row (sent without parent) AND an in-flight token
    db.deliveries["tool:run-pre:c1"] = {
        "delivery_key": "tool:run-pre:c1",
        "kind": "agent_send",
        "user_id": uid,
        "thread_id": tid,
        "run_id": "run-pre",
        "execution_id": None,
        "is_current": False,
        "platform": "feishu",
        "target_address": "feishu:@me",
        "status": "sent",
        "delivery_attempt": 1,
        "dm_recipient_platform_user_id": "ou_alice",
        "bt2_enrolled": True,
        "source_delivery_key": "tool:run-pre:c1",
        "deliverable_manifest": [
            {
                "item_key": f"deliverables-links:tool:run-pre:c1:item:{cg_db.derive_bt2_item_hash('ws1', 'a.pdf')}",
                "item_hash": cg_db.derive_bt2_item_hash("ws1", "a.pdf"),
                "workspace_id": "ws1",
                "path": "a.pdf",
                "filename": "a.pdf",
                "reason": None,
            }
        ],
        "files_status": [{"workspace_id": "ws1", "path": "a.pdf", "status": "pending"}],
        "authorized_target_meta": {
            "auth_mode": "explicit_target",
            "platform": "feishu",
            "target_address": "feishu:@me",
            "is_current": False,
            "link_dm_recipient_open_id": "ou_alice",
        },
        "claim_attempt_id": str(uuid4()),
        "claim_gateway_epoch": 1,
        "claim_lease_expires_at": db.now + timedelta(seconds=120),
    }
    db.user_settings[uid] = {
        "user_id": uid,
        "apps": {"feishu": {"agent_messages": {"enabled": True, "allowed": ["feishu:@me"]}}},
    }

    # Path B: enter_interrupt_drain_mode -> sets all 3 switches FALSE & expires bound token to late_patch=pending
    res_b = await cat.enter_interrupt_drain_mode()
    assert res_b["mode"] == "drain"
    assert res_b["interactive_actions_enabled"] is False
    assert res_b["interrupt_ledger_enabled"] is False
    assert res_b["bt2_link_recovery_enabled"] is False
    assert res_b["expired_bound_tokens"] == 1
    assert db.tokens[th]["consumed_via"] == "expired"
    assert db.tokens[th]["late_patch_status"] == "pending"

    # claim-text during drain returns fallback_direct_dispatch and leaves inbound 'pending'
    db.inbounds[("feishu", "om_text_1")] = {
        "platform": "feishu",
        "message_id": "om_text_1",
        "dalton_user_id": uid,
        "chat_address": "feishu:@me",
        "thread_scope_key": "dm:ou_alice",
        "status": "pending",
        "raw_payload": {},
    }
    ct_drain = await cat.claim_channel_text_interrupt_resume(
        platform="feishu",
        message_id="om_text_1",
        dalton_user_id=uid,
        scope_key="dm:ou_alice",
        generation=1,
        thread_id=tid,
        run_id="run-1",
        interrupt_id="int-1",
        resume_payload={"hitl_response": {"int-1": {"answer": "yes"}}},
        gateway_epoch=1,
    )
    assert ct_drain["ledger_enabled"] is False
    assert ct_drain["fallback_direct_dispatch"] is True
    assert db.inbounds[("feishu", "om_text_1")]["status"] == "pending"

    # Post-barrier non-enrolled BT2 parent creation is denied with bt2_drain_disabled
    denied_new = await cg_db.authorize_and_claim_delivery(
        delivery_key="deliverables-links:tool:run-post:c1",
        kind="fallback",
        user_id=uid,
        thread_id=tid,
        run_id="run-post",
        platform="feishu",
        target="feishu:@me",
        requested_files=[{"workspace_id": "ws1", "path": "b.pdf"}],
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert denied_new["claim_status"] == "denied"
    assert denied_new["code"] == "bt2_drain_disabled"
    assert "deliverables-links:tool:run-post:c1" not in db.deliveries

    # Pre-barrier bt2_enrolled=True source IS still allowed to build its parent during drain!
    allowed_pre = await cg_db.authorize_and_claim_delivery(
        delivery_key="deliverables-links:tool:run-pre:c1",
        kind="fallback",
        source_delivery_key="tool:run-pre:c1",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert allowed_pre["claim_status"] == "claimed"


# ===========================================================================
# STEP 2 REQUIRED TEST: Late-Bind A (confirm_bound vs click-first self-heal + 3-way gate)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("path_kind", ["confirm_bound_first", "click_self_heal_first"])
async def test_late_bind_a_confirm_bound_vs_click_self_heal_three_way_gate_and_start_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    path_kind: str,
) -> None:
    db = _InMemoryBatch3DB()
    tid = str(uuid4())
    uid = "u-alice"
    action_req = {"type": "delete_thread", "thread_id": tid}
    _install_db(
        monkeypatch,
        db,
        pending_interrupts={
            "int-1": {"id": "int-1", "value": {"kind": "hitl_confirm", "action_requests": [action_req]}}
        },
    )
    db.identities[(uid, "feishu")] = {"user_id": uid, "platform": "feishu", "platform_user_id": "ou_alice"}
    db.session_generations[("feishu", "dm:ou_alice", uid)] = {"generation": 1, "pending_interrupt": None}

    minted = await cat.mint_channel_action_token(
        platform="feishu",
        dalton_user_id=uid,
        initiator_platform_user_id="ou_alice",
        origin_chat_type="dm",
        origin_chat_address="feishu:@me",
        origin_scope_key="dm:ou_alice",
        session_generation=1,
        thread_id=tid,
        run_id="run-1",
        interrupt_id="int-1",
        interrupt_value={"kind": "hitl_confirm", "action_requests": [action_req]},
        gateway_epoch=1,
    )
    nonce = minted["approval_nonce"]
    th = minted["token_hash"]
    await cat.bind_channel_action_card(
        approval_nonce=nonce, phase="prepare_send", gateway_epoch=1
    )

    # Web claims BEFORE card confirm_bound arrives -> initially requires_gateway_ack=False, delivery_settled=True, channel_ack_at=NOW()
    web_claim = await cat.claim_web_interrupt_resume(
        thread_id=tid,
        run_id="run-1",
        interrupt_id="int-1",
        user_id=uid,
        resume_payload={"hitl_response": {"int-1": {"decisions": [{"type": "approve"}]}}},
    )
    r_row = db.resumes[(tid, "run-1", "int-1")]
    assert r_row["requires_gateway_ack"] is False
    assert r_row["delivery_settled"] is True
    assert r_row["channel_ack_at"] is not None

    if path_kind == "confirm_bound_first":
        bind_res = await cat.bind_channel_action_card(
            approval_nonce=nonce,
            phase="confirm_bound",
            card_channel_id="oc_dm_1",
            card_message_id="om_card_1",
            gateway_epoch=1,
        )
        assert bind_res["branch"] == "branch_2_unconsumed_resume"
    else:
        # Click callback arrives BEFORE confirm_bound -> self-heal Branch 2 commits binding + card_targets!
        with pytest.raises(HTTPException) as exc_click:
            await cat.consume_channel_action_token(
                approval_nonce=nonce,
                decision="approve",
                clicker_platform_user_id="ou_alice",
                channel_id="oc_dm_1",
                message_id="om_card_1",
                gateway_epoch=1,
            )
        assert exc_click.value.status_code == 409
        assert exc_click.value.detail["code"] == "already_settled"
        assert exc_click.value.detail["can_patch_terminal"] is False

        # Late same-ID confirm_bound returns idempotently
        late_bind = await cat.bind_channel_action_card(
            approval_nonce=nonce,
            phase="confirm_bound",
            card_channel_id="oc_dm_1",
            card_message_id="om_card_1",
            gateway_epoch=1,
        )
        assert late_bind["idempotent"] is True
        assert late_bind["bind_status"] == "bound"

    # ① card_targets registered, delivery_settled reset to FALSE, channel_ack_at cleared
    r_row = db.resumes[(tid, "run-1", "int-1")]
    assert len(r_row["card_targets"]) == 1
    assert r_row["card_targets"][0]["token_hash"] == th
    assert r_row["card_targets"][0]["patch_status"] == "pending"
    assert r_row["requires_gateway_ack"] is True
    assert r_row["delivery_settled"] is False
    assert r_row["channel_ack_at"] is None

    # ② Pre-START click returns can_patch_terminal=False; card-only ack returns 409 resume_not_ready_for_card_ack; recover hides it
    with pytest.raises(HTTPException) as exc_pre:
        await cat.consume_channel_action_token(
            approval_nonce=nonce,
            decision="approve",
            clicker_platform_user_id="ou_alice",
            channel_id="oc_dm_1",
            message_id="om_card_1",
            gateway_epoch=1,
        )
    assert exc_pre.value.status_code == 409
    assert exc_pre.value.detail["can_patch_terminal"] is False

    with pytest.raises(HTTPException) as exc_ack:
        await cat.ack_channel_interrupt_resume(
            thread_id=tid,
            run_id="run-1",
            interrupt_id="int-1",
            card_results=[{"token_hash": th, "patch_status": "patched"}],
            gateway_epoch=1,
        )
    assert exc_ack.value.status_code == 409
    assert exc_ack.value.detail["code"] == "resume_not_ready_for_card_ack"

    rec_pre = await cg_db.recover_channel_state(
        mode="interrupt_resumes", gateway_epoch=1, require_gateway_epoch=True
    )
    assert rec_pre["interrupt_resumes"] == []

    # ③a If START fails -> release_unadmitted restores consumed_at=NULL so user can click again!
    rel = await cat.release_unadmitted_interrupt_claim(
        thread_id=tid, run_id="run-1", interrupt_id="int-1", claim_id=web_claim["claim_id"]
    )
    assert rel["released"] is True
    assert rel["restored_tokens"] == 1
    assert db.tokens[th]["consumed_at"] is None

    # ③b Now re-claim via web, pass START + checkpoint consumption -> can_patch_terminal flips True & ack settles!
    web_claim2 = await cat.claim_web_interrupt_resume(
        thread_id=tid,
        run_id="run-1",
        interrupt_id="int-1",
        user_id=uid,
        resume_payload={"hitl_response": {"int-1": {"decisions": [{"type": "approve"}]}}},
    )
    await cat.admit_interrupt_resume_in_start_tx(
        db.cursor(),
        thread_id=tid,
        run_id="run-1",
        interrupt_id="int-1",
        claim_id=web_claim2["claim_id"],
        request_key=web_claim2["request_key"],
        admitted_run_id="run-admitted-2",
    )
    # Still admitted with run_consumed_at=None -> gate still False!
    assert cat.compute_can_patch_terminal(db.resumes[(tid, "run-1", "int-1")]) is False

    updated_cnt = await cat.mark_interrupt_resume_consumed_by_run(admitted_run_id="run-admitted-2")
    assert updated_cnt == 1
    assert cat.compute_can_patch_terminal(db.resumes[(tid, "run-1", "int-1")]) is True

    rec_post = await cg_db.recover_channel_state(
        mode="interrupt_resumes", gateway_epoch=1, require_gateway_epoch=True
    )
    assert len(rec_post["interrupt_resumes"]) == 1

    ack_ok = await cat.ack_channel_interrupt_resume(
        thread_id=tid,
        run_id="run-1",
        interrupt_id="int-1",
        card_results=[{"token_hash": th, "patch_status": "patched"}],
        gateway_epoch=1,
    )
    assert ack_ok["acked"] is True
    assert ack_ok["channel_ack_at"] is not None
    assert db.resumes[(tid, "run-1", "int-1")]["channel_ack_at"] is not None


# ===========================================================================
# STEP 2 REQUIRED TEST: Late-Bind B + Drain Edge Cases (2) + credit_supplement
# ===========================================================================


@pytest.mark.asyncio
async def test_late_bind_b_and_two_drain_edge_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _InMemoryBatch3DB()
    tid = str(uuid4())
    uid = "u-alice"
    action_req = {"type": "delete_thread", "thread_id": tid}
    _install_db(
        monkeypatch,
        db,
        pending_interrupts={
            "int-1": {"id": "int-1", "value": {"kind": "hitl_confirm", "action_requests": [action_req]}}
        },
    )
    db.identities[(uid, "discord")] = {"user_id": uid, "platform": "discord", "platform_user_id": "dc_alice"}
    db.chat_bindings[("discord", "discord:g1/ch1")] = {"platform": "discord", "chat_address": "discord:g1/ch1", "workspace_id": None}
    db.user_settings[uid] = {"user_id": uid, "apps": {"discord": {"chats": {"discord:g1/ch1": {"name": "Research"}}}}}

    # Token 1: pending_bind > 90s expires to bind_expired; Token 2: send_in_flight > 90s stays send_in_flight
    m1 = await cat.mint_channel_action_token(
        platform="discord",
        dalton_user_id=uid,
        initiator_platform_user_id="dc_alice",
        origin_chat_type="group",
        origin_chat_address="discord:g1/ch1",
        origin_scope_key="chat:g1/ch1:topic:m1",
        session_generation=1,
        thread_id=tid,
        run_id="run-1",
        interrupt_id="int-1",
        interrupt_value={"kind": "hitl_confirm", "action_requests": [action_req]},
        gateway_epoch=1,
    )
    m2 = await cat.mint_channel_action_token(
        platform="discord",
        dalton_user_id=uid,
        initiator_platform_user_id="dc_alice",
        origin_chat_type="group",
        origin_chat_address="discord:g1/ch1",
        origin_scope_key="chat:g1/ch1:topic:m2",
        session_generation=1,
        thread_id=tid,
        run_id="run-1",
        interrupt_id="int-1",
        interrupt_value={"kind": "hitl_confirm", "action_requests": [action_req]},
        gateway_epoch=1,
    )
    assert m1["token_role"] == "gate"
    await cat.bind_channel_action_card(
        approval_nonce=m2["approval_nonce"],
        phase="prepare_send",
        send_reconcile_meta={"target_channel_id": "ch1"},
        gateway_epoch=1,
    )

    # Advance clock 120s and run RecoveryScanner -> ONLY m1 (pending_bind) expires; m2 stays send_in_flight!
    db.now = db.now + timedelta(seconds=120)
    rec_stats = await cat.reconcile_stale_interrupt_resumes()
    assert rec_stats["expired_pending_bind_tokens"] == 1
    assert db.tokens[m1["token_hash"]]["bind_status"] == "bind_expired"
    assert db.tokens[m2["token_hash"]]["bind_status"] == "send_in_flight"

    # Mint m3 for Drain Edge Case #1 (Discord send_unknown gate card clicked during drain)
    m3 = await cat.mint_channel_action_token(
        platform="discord",
        dalton_user_id=uid,
        initiator_platform_user_id="dc_alice",
        origin_chat_type="group",
        origin_chat_address="discord:g1/ch1",
        origin_scope_key="chat:g1/ch1:topic:m3",
        session_generation=1,
        thread_id=tid,
        run_id="run-1",
        interrupt_id="int-1",
        interrupt_value={"kind": "hitl_confirm", "action_requests": [action_req]},
        gateway_epoch=1,
    )
    await cat.bind_channel_action_card(
        approval_nonce=m3["approval_nonce"],
        phase="prepare_send",
        send_reconcile_meta={"target_channel_id": "ch1"},
        gateway_epoch=1,
    )
    await cat.bind_channel_action_card(
        approval_nonce=m3["approval_nonce"],
        phase="send_unknown",
        gateway_epoch=1,
    )
    assert db.tokens[m3["token_hash"]]["bind_status"] == "send_unknown"

    # Enter 3-switch drain mode
    await cat.enter_interrupt_drain_mode()

    # Drain Edge Case #1: Discord send_unknown gate card clicked during drain -> Branch 3 self-heal + late_patch_status='pending'
    with pytest.raises(HTTPException) as exc_gate:
        await cat.inspect_discord_gate_token(
            approval_nonce=m3["approval_nonce"],
            clicker_platform_user_id="dc_alice",
            channel_id="ch1",
            message_id="msg_gate_3",
            gateway_epoch=1,
        )
    assert exc_gate.value.status_code == 410
    assert exc_gate.value.detail["can_patch_terminal"] is True
    assert exc_gate.value.detail["patch_ack_mode"] == "late_card_results"
    assert db.tokens[m3["token_hash"]]["bind_status"] == "bound"
    assert db.tokens[m3["token_hash"]]["late_patch_status"] == "pending"

    # Drain Edge Case #2: prepare_send timeout re-entry during drain on m2 (already send_in_flight) stays idempotent
    re_prep = await cat.bind_channel_action_card(
        approval_nonce=m2["approval_nonce"],
        phase="prepare_send",
        send_reconcile_meta={"target_channel_id": "ch1"},
        gateway_epoch=1,
    )
    assert re_prep["ok"] is True
    assert re_prep["idempotent"] is True
    assert re_prep["abort_card_send"] is False
    assert re_prep["bind_status"] == "send_in_flight"


@pytest.mark.asyncio
async def test_credit_supplement_pending_resubmit_preserved_on_origin_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _InMemoryBatch3DB()
    tid = str(uuid4())
    uid = "u-alice"
    resub_key = str(uuid4())
    _install_db(
        monkeypatch,
        db,
        pending_interrupts={
            "int-cp": {"id": "int-cp", "value": {"kind": "credit_pause", "action_requests": [{"type": "credit_pause"}]}}
        },
    )
    db.session_generations[("feishu", "dm:ou_alice", uid)] = {"generation": 1, "pending_interrupt": None}
    db.inbounds[("feishu", "om_cp_1")] = {
        "platform": "feishu",
        "message_id": "om_cp_1",
        "dalton_user_id": uid,
        "chat_address": "feishu:@me",
        "thread_scope_key": "dm:ou_alice",
        "status": "pending",
        "fallback_pending": True,
        "resubmitted_request_key": None,
        "uploaded_attachments": [{"file_name": "note.pdf", "workspace_path": "uploads/note.pdf"}],
        "raw_payload": {},
    }

    claimed = await cat.claim_channel_text_interrupt_resume(
        platform="feishu",
        message_id="om_cp_1",
        dalton_user_id=uid,
        scope_key="dm:ou_alice",
        generation=1,
        thread_id=tid,
        run_id="run-cp",
        interrupt_id="int-cp",
        resume_payload={
            "hitl_response": {"int-cp": {"decisions": [{"type": "approve"}]}},
            "credit_supplement": {"text": "Also check Q4", "content": [{"type": "text", "text": "Also check Q4"}]},
        },
        gateway_epoch=1,
    )
    assert db.inbounds[("feishu", "om_cp_1")]["status"] == "hitl_claimed"

    # Simulate START + checkpoint consumption + credit_supplement transitioning inbound to pending_resubmit
    await cat.admit_interrupt_resume_in_start_tx(
        db.cursor(),
        thread_id=tid,
        run_id="run-cp",
        interrupt_id="int-cp",
        claim_id=claimed["claim_id"],
        request_key=claimed["request_key"],
        admitted_run_id="run-resumed",
    )
    await cat.mark_interrupt_resume_consumed_by_run(admitted_run_id="run-resumed")

    db.inbounds[("feishu", "om_cp_1")]["status"] = "pending_resubmit"
    db.inbounds[("feishu", "om_cp_1")]["resubmitted_request_key"] = resub_key
    db.inbounds[("feishu", "om_cp_1")]["fallback_pending"] = True
    db.inbounds[("feishu", "om_cp_1")]["raw_payload"]["credit_steer_state"] = "resubmitted"

    # Origin ack(settle_delivery=True) MUST preserve pending_resubmit, resubmitted_request_key, fallback_pending, attachments!
    ack_res = await cat.ack_channel_interrupt_resume(
        thread_id=tid,
        run_id="run-cp",
        interrupt_id="int-cp",
        claim_id=claimed["claim_id"],
        settle_delivery=True,
        gateway_epoch=1,
    )
    assert ack_res["delivery_settled"] is True
    assert ack_res["preserved_pending_resubmit"] is True
    inb = db.inbounds[("feishu", "om_cp_1")]
    assert inb["status"] == "pending_resubmit"
    assert inb["resubmitted_request_key"] == resub_key
    assert inb["fallback_pending"] is True
    assert inb["uploaded_attachments"] == [{"file_name": "note.pdf", "workspace_path": "uploads/note.pdf"}]
    assert inb["raw_payload"]["hitl_resume_settled"] is True


# ===========================================================================
# STEP 2 REQUIRED TEST: BT2 Complete Backend Protocol Coverage
# ===========================================================================


@pytest.mark.asyncio
async def test_bt2_active_source_skip_sent_source_recovery_revoke_dual_dm_independent_confirm_and_reclaim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _InMemoryBatch3DB()
    tid = str(uuid4())
    uid = "u-alice"
    _install_db(monkeypatch, db)
    db.identities[(uid, "feishu")] = {"user_id": uid, "platform": "feishu", "platform_user_id": "ou_alice"}
    db.identities[(uid, "discord")] = {"user_id": uid, "platform": "discord", "platform_user_id": "dc_alice"}
    db.chat_bindings[("feishu", "feishu:oc_grp")] = {"platform": "feishu", "chat_address": "feishu:oc_grp", "workspace_id": None}
    db.user_settings[uid] = {
        "user_id": uid,
        "apps": {
            "feishu": {
                "chats": {"feishu:oc_grp": {"name": "Team"}},
                "agent_messages": {"enabled": True, "allowed": ["feishu:oc_grp", "feishu:@me"]},
            }
        },
    }

    # 1) Active in_flight source -> parent I-6 returns source_still_active; once source confirms all files -> already_completed
    src_active_key = "tool:run-1:c_active"
    claim_src = await cg_db.authorize_and_claim_delivery(
        delivery_key=src_active_key,
        kind="agent_send",
        user_id=uid,
        thread_id=tid,
        run_id="run-1",
        target="feishu:oc_grp",
        requested_files=[{"workspace_id": "ws1", "path": "report.pdf"}],
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert claim_src["claim_status"] == "claimed"
    assert claim_src["bt2_enrolled"] is True

    parent_active = await cg_db.authorize_and_claim_delivery(
        delivery_key=f"deliverables-links:{src_active_key}",
        kind="fallback",
        source_delivery_key=src_active_key,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert parent_active["claim_status"] == "source_still_active"

    # Source completes and marks file 'sent' -> parent I-6 re-reads files_status -> already_completed (zero parent written!)
    await cg_db.update_delivery_progress(
        delivery_key=src_active_key,
        status="sent",
        files_status=[{"workspace_id": "ws1", "path": "report.pdf", "status": "sent"}],
        claim_attempt_id=claim_src["claim_attempt_id"],
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    parent_done = await cg_db.authorize_and_claim_delivery(
        delivery_key=f"deliverables-links:{src_active_key}",
        kind="fallback",
        source_delivery_key=src_active_key,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert parent_done["claim_status"] == "already_completed"
    assert f"deliverables-links:{src_active_key}" not in db.deliveries

    # 2) Source 0-chunk files-only I-7 marks 'sent' while preserving unconfirmed 'pending' -> recover detects source_missing_parent!
    src_zero_key = "tool:run-2:c_zero"
    claim_zero = await cg_db.authorize_and_claim_delivery(
        delivery_key=src_zero_key,
        kind="agent_send",
        user_id=uid,
        thread_id=tid,
        run_id="run-2",
        target="feishu:oc_grp",
        has_non_empty_text=False,
        total_chunks=0,
        requested_files=[{"workspace_id": "ws1", "path": "deck.pdf"}],
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    await cg_db.update_delivery_progress(
        delivery_key=src_zero_key,
        status="sent",
        sent_chunks=0,
        claim_attempt_id=claim_zero["claim_attempt_id"],
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert db.deliveries[src_zero_key]["status"] == "sent"
    assert db.deliveries[src_zero_key]["files_status"][0]["status"] == "pending"

    rec_bt2 = await cg_db.recover_channel_state(
        mode="interrupt_resumes", gateway_epoch=1, require_gateway_epoch=True
    )
    assert any(
        r["delivery_key"] == src_zero_key and r["recovery_kind"] == "source_missing_parent"
        for r in rec_bt2["bt2_link_recoveries"]
    )

    # 3) Explicit group revoke (agent_messages.enabled = False) -> parent I-6 settles terminal not_allowed ('失败，已在网页通知')
    db.inbounds[("feishu", "om_r2")] = {"platform": "feishu", "message_id": "om_r2", "run_id": "run-2", "fallback_pending": True, "raw_payload": {}}
    db.user_settings[uid]["apps"]["feishu"]["agent_messages"]["enabled"] = False
    revoked_parent = await cg_db.authorize_and_claim_delivery(
        delivery_key=f"deliverables-links:{src_zero_key}",
        kind="fallback",
        source_delivery_key=src_zero_key,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert revoked_parent["claim_status"] == "denied"
    assert revoked_parent["code"] == "not_allowed"
    assert revoked_parent["message"] == "失败，已在网页通知"
    assert db.deliveries[f"deliverables-links:{src_zero_key}"]["status"] == "failed"
    assert db.inbounds[("feishu", "om_r2")]["fallback_pending"] is False

    # Re-enable agent_messages for remaining tests
    db.user_settings[uid]["apps"]["feishu"]["agent_messages"]["enabled"] = True

    # 4) Group explicit send_message(target='feishu:@me', files=[...]) (is_current=False) -> verify-dm-recipient Branch B passes
    #    with pinned link_dm_recipient_open_id, rejects mismatched resolved_recipient_id, and binary download returns 403!
    src_dm_key = "tool:run-3:c_dm"
    claim_dm_src = await cg_db.authorize_and_claim_delivery(
        delivery_key=src_dm_key,
        kind="agent_send",
        user_id=uid,
        thread_id=tid,
        run_id="run-3",
        target="feishu:@me",
        requested_files=[{"workspace_id": "ws1", "path": "report.html"}],
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert claim_dm_src["authorized_target_meta"]["link_dm_recipient_open_id"] == "ou_alice"
    await cg_db.update_delivery_progress(
        delivery_key=src_dm_key,
        status="sent",
        claim_attempt_id=claim_dm_src["claim_attempt_id"],
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    p_dm = await cg_db.authorize_and_claim_delivery(
        delivery_key=f"deliverables-links:{src_dm_key}",
        kind="fallback",
        source_delivery_key=src_dm_key,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    item_key_dm = p_dm["deliverable_manifest"][0]["item_key"]
    c_dm = await cg_db.authorize_and_claim_delivery(
        delivery_key=item_key_dm,
        kind="fallback",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert c_dm["claim_status"] == "claimed"

    # Branch B passes with matching resolved_recipient_id
    ver_ok = await cg_db.verify_dm_delivery_recipient(
        delivery_key=item_key_dm,
        delivery_attempt=c_dm["delivery_attempt"],
        resolved_recipient_id="ou_alice",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert ver_ok["authorized"] is True
    assert ver_ok["recipient_id"] == "ou_alice"

    # Branch B rejects drifted resolved_recipient_id
    with pytest.raises(HTTPException) as exc_drift:
        await cg_db.verify_dm_delivery_recipient(
            delivery_key=item_key_dm,
            delivery_attempt=c_dm["delivery_attempt"],
            resolved_recipient_id="ou_other",
            gateway_epoch=1,
            require_gateway_epoch=True,
        )
    assert exc_drift.value.status_code == 403

    # Branch B also rejects when explicit agent_messages permission is revoked live
    db.user_settings[uid]["apps"]["feishu"]["agent_messages"]["enabled"] = False
    with pytest.raises(HTTPException) as exc_revoked_dm:
        await cg_db.verify_dm_delivery_recipient(
            delivery_key=item_key_dm,
            delivery_attempt=c_dm["delivery_attempt"],
            resolved_recipient_id="ou_alice",
            gateway_epoch=1,
            require_gateway_epoch=True,
        )
    assert exc_revoked_dm.value.status_code == 403
    db.user_settings[uid]["apps"]["feishu"]["agent_messages"]["enabled"] = True

    # Binary download rejects deliverables-links: key with 403
    with pytest.raises(HTTPException) as exc_dl:
        await cd.download_verified_deliverable(
            delivery_key=item_key_dm,
            delivery_attempt=c_dm["delivery_attempt"],
            workspace_id="ws1",
            path="report.pdf",
            gateway_epoch=1,
        )
    assert exc_dl.value.status_code == 403

    # 4b) Feishu background DM without original inbound -> passes parent/item I-6 & verify-dm-recipient Branch B with pinned open_id
    db.deliveries["deliverables:run-fs-bg"] = {
        "delivery_key": "deliverables:run-fs-bg",
        "kind": "fallback",
        "user_id": uid,
        "thread_id": tid,
        "run_id": "run-fs-bg",
        "execution_id": None,
        "is_current": True,
        "platform": "feishu",
        "target_address": "feishu:@me",
        "status": "unknown",
        "delivery_attempt": 1,
        "dm_recipient_platform_user_id": None,
        "bt2_enrolled": True,
        "deliverable_manifest": [{"item_key": "deliverables-links:run-fs-bg:item:abc", "workspace_id": "ws1", "path": "bg.pdf"}],
        "files_status": [{"workspace_id": "ws1", "path": "bg.pdf", "status": "pending"}],
        "authorized_target_meta": {"auth_mode": "current_conversation", "platform": "feishu", "target_address": "feishu:@me", "is_current": True},
    }
    p_fs_bg = await cg_db.authorize_and_claim_delivery(
        delivery_key="deliverables-links:run-fs-bg",
        kind="fallback",
        source_delivery_key="deliverables:run-fs-bg",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert p_fs_bg["claim_status"] == "claimed"
    assert p_fs_bg["authorized_target_meta"]["link_dm_recipient_open_id"] == "ou_alice"
    item_fs_bg = p_fs_bg["deliverable_manifest"][0]["item_key"]
    c_fs_bg = await cg_db.authorize_and_claim_delivery(
        delivery_key=item_fs_bg,
        kind="fallback",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert c_fs_bg["claim_status"] == "claimed"
    ver_fs_bg = await cg_db.verify_dm_delivery_recipient(
        delivery_key=item_fs_bg,
        delivery_attempt=c_fs_bg["delivery_attempt"],
        resolved_recipient_id="ou_alice",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert ver_fs_bg["authorized"] is True
    assert ver_fs_bg["recipient_id"] == "ou_alice"
    with pytest.raises(HTTPException) as exc_fs_bg_drift:
        await cg_db.verify_dm_delivery_recipient(
            delivery_key=item_fs_bg,
            delivery_attempt=c_fs_bg["delivery_attempt"],
            resolved_recipient_id="ou_other",
            gateway_epoch=1,
            require_gateway_epoch=True,
        )
    assert exc_fs_bg_drift.value.status_code == 403

    # 5) Discord background DM without original inbound -> intentional scope limit settles terminal not_allowed
    db.deliveries["deliverables:run-dc-bg"] = {
        "delivery_key": "deliverables:run-dc-bg",
        "kind": "fallback",
        "user_id": uid,
        "thread_id": tid,
        "run_id": "run-dc-bg",
        "execution_id": None,
        "is_current": True,
        "platform": "discord",
        "target_address": "discord:@me",
        "status": "unknown",
        "delivery_attempt": 1,
        "dm_recipient_platform_user_id": None,
        "bt2_enrolled": True,
        "deliverable_manifest": [{"item_key": "deliverables-links:run-dc-bg:item:abc", "workspace_id": "ws1", "path": "a.pdf"}],
        "files_status": [{"workspace_id": "ws1", "path": "a.pdf", "status": "pending"}],
        "authorized_target_meta": {"auth_mode": "current_conversation", "platform": "discord", "target_address": "discord:@me", "is_current": True},
    }
    dc_bg_res = await cg_db.authorize_and_claim_delivery(
        delivery_key="deliverables-links:run-dc-bg",
        kind="fallback",
        source_delivery_key="deliverables:run-dc-bg",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert dc_bg_res["claim_status"] == "denied"
    assert dc_bg_res["code"] == "not_allowed"
    assert dc_bg_res["message"] == "失败，已在网页通知"

    # 6) Two explicit sends of same path in same run confirm independently (no cross-borrowing!)
    for call_tag in ("c1", "c2"):
        sk = f"tool:run-dup:{call_tag}"
        cs = await cg_db.authorize_and_claim_delivery(
            delivery_key=sk,
            kind="agent_send",
            user_id=uid,
            thread_id=tid,
            run_id="run-dup",
            target="feishu:oc_grp",
            requested_files=[{"workspace_id": "ws1", "path": "same.pdf"}],
            gateway_epoch=1,
            require_gateway_epoch=True,
        )
        await cg_db.update_delivery_progress(
            delivery_key=sk, status="sent", claim_attempt_id=cs["claim_attempt_id"], gateway_epoch=1
        )
    p1 = await cg_db.authorize_and_claim_delivery(
        delivery_key="deliverables-links:tool:run-dup:c1",
        kind="fallback",
        source_delivery_key="tool:run-dup:c1",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    p2 = await cg_db.authorize_and_claim_delivery(
        delivery_key="deliverables-links:tool:run-dup:c2",
        kind="fallback",
        source_delivery_key="tool:run-dup:c2",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    # Complete p1's item child ONLY -> p1 settles 'sent', but p2 trying to settle 'sent' stays 'partial'!
    i1 = p1["deliverable_manifest"][0]["item_key"]
    ci1 = await cg_db.authorize_and_claim_delivery(delivery_key=i1, kind="fallback", gateway_epoch=1, require_gateway_epoch=True)
    await cg_db.update_delivery_progress(
        delivery_key=i1, status="sent", sent_chunks=1, claim_attempt_id=ci1["claim_attempt_id"], gateway_epoch=1
    )
    p1_upd = await cg_db.update_delivery_progress(
        delivery_key="deliverables-links:tool:run-dup:c1", status="sent", claim_attempt_id=p1["claim_attempt_id"], gateway_epoch=1
    )
    assert p1_upd["status"] == "sent"

    p2_upd = await cg_db.update_delivery_progress(
        delivery_key="deliverables-links:tool:run-dup:c2", status="sent", claim_attempt_id=p2["claim_attempt_id"], gateway_epoch=1
    )
    assert p2_upd["status"] == "partial"  # Independent! Did NOT borrow p1's sent item!

    # 7) Reclaim stuck in_flight via reclaim_stale_attempt_id while holding valid lease, and stale claim_attempt_id rejected with 409
    i2 = p2["deliverable_manifest"][0]["item_key"]
    ci2_old = await cg_db.authorize_and_claim_delivery(delivery_key=i2, kind="fallback", gateway_epoch=1, require_gateway_epoch=True)
    # Without reclaim_stale_attempt_id -> already_in_flight
    dup_i2 = await cg_db.authorize_and_claim_delivery(delivery_key=i2, kind="fallback", gateway_epoch=1, require_gateway_epoch=True)
    assert dup_i2["claim_status"] == "already_in_flight"
    # With matching reclaim_stale_attempt_id -> reclaimed with new claim_attempt_id!
    ci2_new = await cg_db.authorize_and_claim_delivery(
        delivery_key=i2,
        kind="fallback",
        reclaim_stale_attempt_id=ci2_old["claim_attempt_id"],
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert ci2_new["claim_status"] == "claimed"
    assert ci2_new["claim_attempt_id"] != ci2_old["claim_attempt_id"]
    # Old claim_attempt_id rejected by I-7 with 409 stale_delivery_attempt
    with pytest.raises(HTTPException) as exc_stale:
        await cg_db.update_delivery_progress(
            delivery_key=i2,
            status="sent",
            claim_attempt_id=ci2_old["claim_attempt_id"],
            gateway_epoch=1,
        )
    assert exc_stale.value.status_code == 409
    assert exc_stale.value.detail["code"] == "stale_delivery_attempt"
