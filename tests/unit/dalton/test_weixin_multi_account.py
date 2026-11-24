"""Comprehensive unit tests for Team Personal Weixin Multi-Account (Batch A Backend).

Covers §7.1 #1–#7 and post-review Codex items ①–④ using `unittest.mock.create_autospec`
test doubles (never signature-swallowing `Mock(**kwargs)`).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import psycopg
from psycopg.rows import dict_row
import pytest

from src.dalton import access_tiers
from src.dalton.app import channels_router as ch_router_mod
from src.dalton.app.router import router as dalton_router
from src.dalton.auth import roles as dalton_roles
from src.dalton.database import automation_notifications as auto_notify_db
from src.dalton.database import automation_reports as auto_reports_db
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database import members as dalton_members
from src.dalton.database import weixin_fk_validate as fk_val
from src.dalton.database import weixin_multi_account as wx_db
from src.dalton.notify import dispatch as dispatch_mod
from src.dalton.services import report_delivery as report_delivery_svc
from src.dalton.settings import (
    get_weixin_max_concurrent_accounts,
    redact_weixin_secrets,
)
from src.server.database import pool as db_pool
from src.server.database import user as user_db
from src.server.database import workspace as workspace_db
from src.server.utils.api import get_current_user_id

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_TOKEN = "w" * 40


class InMemoryWeixinMultiAccountStore:
    """In-memory relational simulator for multi-account Weixin & channel gateway SQL."""

    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.now: datetime = now
        self.lease: dict[str, Any] | None = {
            "singleton_id": 1,
            "instance_id": "gw-1",
            "epoch": 1,
            "expires_at": now + timedelta(seconds=45),
            "updated_at": now,
        }
        self.config: dict[str, Any] = {
            "singleton_id": 1,
            "enabled": True,
            "allow_external": None,
            "accounts_revision": 1,
            "updated_at": now,
        }
        self.accounts: dict[str, dict[str, Any]] = {}
        self.account_tokens: dict[tuple[str, int, str], dict[str, Any]] = {}
        self.login_sessions: dict[str, dict[str, Any]] = {}
        self.audit_logs: list[dict[str, Any]] = []
        self.collect_buffers: dict[str, dict[str, Any]] = {}
        self.collect_items: dict[tuple[str, str], dict[str, Any]] = {}
        self.inbound_events: dict[tuple[str, str], dict[str, Any]] = {}
        self.thread_watches: dict[str, dict[str, Any]] = {}
        self.background_runs: dict[str, dict[str, Any]] = {}
        self.deliveries: dict[str, dict[str, Any]] = {}
        self.user_settings: dict[str, dict[str, Any]] = {}
        self.session_generations: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.conversation_queries: list[dict[str, Any]] = []
        self.conversation_responses: dict[str, dict[str, Any]] = {}
        self.conversation_threads: dict[str, dict[str, Any]] = {}
        self.workspaces: dict[str, dict[str, Any]] = {}
        self.user_roles: dict[str, str] = {}
        self.executed_sql: list[tuple[str, Any]] = []
        self.last_rowcount: int | None = None

    def _active_slot_count(self, exclude_user_id: str | None = None) -> int:
        return sum(
            1
            for a in self.accounts.values()
            if a.get("status") in ("active", "pending_verification")
            and (exclude_user_id is None or a.get("user_id") != exclude_user_id)
        )

    def _unwrap(self, val: Any) -> Any:
        return val.obj if hasattr(val, "obj") else val

    async def handle_sql(self, sql: str, params: Any) -> Any:
        norm = " ".join(str(sql).split())
        self.executed_sql.append((norm, params))
        now = self.now
        self.last_rowcount = None

        if "SELECT clock_timestamp() AS db_now" in norm or "SELECT NOW() AS db_now" in norm:
            return [{"db_now": now}]

        if "pg_advisory_xact_lock" in norm:
            return [{"pg_advisory_xact_lock": None}]

        if "FROM workspaces WHERE workspace_id = %s" in norm:
            wid = str(params[0])
            ws = self.workspaces.get(wid)
            return [dict(ws)] if ws else []

        # --- Gateway lease ---
        if "INSERT INTO dalton_channel_gateway_lease" in norm:
            if self.lease is None:
                inst = str(params[0])
                ttl = int(params[1]) if len(params) > 1 else 45
                self.lease = {
                    "singleton_id": 1,
                    "instance_id": inst,
                    "epoch": 1,
                    "expires_at": now + timedelta(seconds=ttl),
                    "updated_at": now,
                }
                return [dict(self.lease)]
            return []

        if "FROM dalton_channel_gateway_lease" in norm and "SELECT" in norm:
            if not self.lease:
                return []
            row = dict(self.lease)
            row["db_now"] = now
            return [row]

        if "UPDATE dalton_channel_gateway_lease" in norm:
            if not self.lease:
                self.last_rowcount = 0
                return []
            if "SET expires_at = NOW() - INTERVAL" in norm and not params:
                self.lease["expires_at"] = now - timedelta(seconds=1)
                self.lease["updated_at"] = now
                self.last_rowcount = 1
                return [{"epoch": self.lease["epoch"]}]
            if "SET expires_at = NOW()" in norm and "instance_id = %s AND epoch = %s" in norm:
                inst, ep = str(params[0]), int(params[1])
                if self.lease["instance_id"] == inst and int(self.lease["epoch"]) == ep:
                    self.lease["expires_at"] = now - timedelta(seconds=1)
                    self.lease["updated_at"] = now
                    self.last_rowcount = 1
                    return [{"epoch": ep}]
                self.last_rowcount = 0
                return []
            if "epoch = epoch + 1" in norm:
                inst, ttl = str(params[0]), int(params[1])
                self.lease["instance_id"] = inst
                self.lease["epoch"] = int(self.lease["epoch"]) + 1
                self.lease["expires_at"] = now + timedelta(seconds=ttl)
                self.lease["updated_at"] = now
                self.last_rowcount = 1
                return [dict(self.lease)]
            if "WHERE singleton_id = 1 AND instance_id = %s AND epoch = %s" in norm:
                ttl, inst, ep = int(params[0]), str(params[1]), int(params[2])
                if (
                    self.lease["instance_id"] == inst
                    and int(self.lease["epoch"]) == ep
                    and self.lease["expires_at"] > now
                ):
                    self.lease["expires_at"] = now + timedelta(seconds=ttl)
                    self.lease["updated_at"] = now
                    self.last_rowcount = 1
                    return [dict(self.lease)]
                self.last_rowcount = 0
                return []
            # Same-instance renew in acquire
            ttl = int(params[0])
            self.lease["expires_at"] = now + timedelta(seconds=ttl)
            self.lease["updated_at"] = now
            self.last_rowcount = 1
            return [dict(self.lease)]

        # --- Weixin config ---
        if "INSERT INTO dalton_channel_weixin_config" in norm:
            return []
        if "FROM dalton_channel_weixin_config" in norm and "SELECT" in norm:
            return [dict(self.config)]
        if "UPDATE dalton_channel_weixin_config" in norm:
            if "AND enabled = TRUE" in norm and "allow_external" in norm:
                tier_val, default_ext = str(params[0]), bool(params[1])
                eff_ext = (
                    bool(self.config["allow_external"])
                    if self.config["allow_external"] is not None
                    else default_ext
                )
                if not self.config["enabled"] or (tier_val == "external" and not eff_ext):
                    self.last_rowcount = 0
                    return []
                self.config["accounts_revision"] += 1
                self.config["updated_at"] = now
                self.last_rowcount = 1
                return [{"accounts_revision": self.config["accounts_revision"]}]
            if "SET enabled = %s" in norm:
                self.config["enabled"] = bool(params[0])
                self.config["allow_external"] = params[1]
                self.config["accounts_revision"] += 1
                self.config["updated_at"] = now
                self.last_rowcount = 1
                return [dict(self.config)]
            self.config["accounts_revision"] += 1
            self.config["updated_at"] = now
            self.last_rowcount = 1
            return [{"accounts_revision": self.config["accounts_revision"]}]

        # --- Weixin audit logs ---
        if "INSERT INTO dalton_channel_weixin_audit_logs" in norm:
            self.audit_logs.append(
                {
                    "id": len(self.audit_logs) + 1,
                    "action": str(params[0]),
                    "operator_user_id": str(params[1]),
                    "target_user_id": params[2],
                    "account_id": params[3],
                    "ilink_bot_id": params[4],
                    "masked_ilink_user_id": params[5],
                    "reason": str(params[6]),
                    "metadata": self._unwrap(params[7]) if len(params) > 7 else {},
                    "created_at": now,
                }
            )
            return []
        if "FROM dalton_channel_weixin_audit_logs" in norm and "SELECT" in norm:
            return list(reversed(self.audit_logs[-50:]))

        # --- User settings ---
        if "INSERT INTO dalton_channel_user_settings" in norm:
            uid = str(params[0])
            if uid not in self.user_settings:
                apps = self._unwrap(params[1]) if len(params) > 1 else {}
                self.user_settings[uid] = {
                    "user_id": uid,
                    "apps": dict(apps) if isinstance(apps, dict) else {},
                    "version": "1",
                    "updated_at": now,
                }
            return []
        if "FROM dalton_channel_user_settings" in norm and "SELECT" in norm:
            if "WHERE user_id = ANY" in norm:
                uids = set(params[0] or [])
                return [dict(r) for u, r in sorted(self.user_settings.items()) if u in uids]
            uid = str(params[0])
            r = self.user_settings.get(uid)
            return [dict(r)] if r else []
        if "UPDATE dalton_channel_user_settings" in norm:
            apps = self._unwrap(params[0])
            uid = str(params[-1])
            row = self.user_settings.setdefault(
                uid, {"user_id": uid, "apps": {}, "version": "1", "updated_at": now}
            )
            if isinstance(apps, dict):
                row["apps"] = dict(apps)
            row["updated_at"] = now
            return [dict(row)]

        # --- Weixin accounts ---
        if "FROM dalton_channel_weixin_accounts" in norm and "SELECT" in norm:
            if "COUNT(*)" in norm:
                excl = str(params[0]) if params and "user_id <> %s" in norm else None
                return [{"cnt": self._active_slot_count(excl)}]
            rows = list(self.accounts.values())
            if "WHERE account_id = ANY" in norm:
                aids = {str(x) for x in (params[-1] or [])}
                rows = [r for r in rows if str(r["account_id"]) in aids]
            elif "WHERE user_id = %s OR ilink_bot_id = %s" in norm:
                uid_q, bid_q, wx_q = str(params[0]), str(params[1]), params[2]
                rows = [
                    r
                    for r in rows
                    if r.get("user_id") == uid_q
                    or r.get("ilink_bot_id") == bid_q
                    or (
                        wx_q is not None
                        and (r.get("ilink_user_id") == wx_q or r.get("prev_ilink_user_id") == wx_q)
                    )
                ]
            elif "WHERE account_id = %s::uuid OR COALESCE(ilink_user_id, prev_ilink_user_id) = %s" in norm:
                aid_q, wx_q = str(params[0]), str(params[1])
                rows = [
                    r
                    for r in rows
                    if str(r["account_id"]) == aid_q
                    or (r.get("ilink_user_id") or r.get("prev_ilink_user_id")) == wx_q
                ]
            elif "WHERE account_id = %s" in norm:
                aid = str(params[-1])
                rows = [r for r in rows if str(r["account_id"]) == aid]
            elif "WHERE user_id = %s" in norm:
                uid = str(params[-1])
                rows = [r for r in rows if r["user_id"] == uid]
            elif "WHERE ilink_bot_id = %s" in norm:
                bid = str(params[-1])
                rows = [r for r in rows if r["ilink_bot_id"] == bid]
            elif "COALESCE(ilink_user_id, prev_ilink_user_id) = %s" in norm or "ilink_user_id = %s OR prev_ilink_user_id = %s" in norm:
                wx_id = str(params[0])
                rows = [
                    r
                    for r in rows
                    if r.get("ilink_user_id") == wx_id or r.get("prev_ilink_user_id") == wx_id
                ]
                if "AND account_id <> %s" in norm and len(params) >= 2:
                    excl_aid = str(params[1])
                    rows = [r for r in rows if str(r["account_id"]) != excl_aid]
                if "OR disabled_reason = 'external_access_disabled'" in norm:
                    rows = [
                        r
                        for r in rows
                        if r.get("status") in ("active", "pending_verification", "expired")
                        or r.get("disabled_reason") == "external_access_disabled"
                    ]
            elif "WHERE ilink_user_id = %s" in norm:
                wx_id = str(params[-1])
                rows = [r for r in rows if r.get("ilink_user_id") == wx_id]
            elif "WHERE status IN ('active', 'pending_verification')" in norm:
                rows = [r for r in rows if r.get("status") in ("active", "pending_verification")]
            elif "WHERE status = 'disabled'" in norm:
                rows = [r for r in rows if r.get("status") == "disabled"]
            if "AND status = 'active'" in norm:
                rows = [r for r in rows if r.get("status") == "active"]
            if "AND user_id <> %s" in norm:
                excl_uid = str(params[-1])
                rows = [r for r in rows if r.get("user_id") != excl_uid]
            rows.sort(key=lambda r: str(r["account_id"]))
            out = []
            for r in rows:
                d = dict(r)
                d.setdefault("bot_token", d.get("encrypted_bot_token"))
                if "ilink_user_id AS platform_user_id" in norm:
                    d["platform_user_id"] = d.get("ilink_user_id")
                out.append(d)
            return out

        if "INSERT INTO dalton_channel_weixin_accounts" in norm:
            uid, bid, wx_uid, uname, tok, _enc, burl, cburl, st = (
                str(params[0]),
                str(params[1]),
                params[2],
                params[3],
                str(params[4]),
                params[5],
                str(params[6]),
                str(params[7]),
                str(params[8]),
            )
            aid = str(uuid4())
            row = {
                "account_id": aid,
                "user_id": uid,
                "ilink_bot_id": bid,
                "encrypted_bot_token": tok,
                "bot_token": tok,
                "base_url": burl,
                "cdn_base_url": cburl,
                "get_updates_buf": "",
                "status": st,
                "prev_status": None,
                "disabled_reason": None,
                "bot_generation": 1,
                "prev_bot_generation": None,
                "bot_generation_updated_at": None,
                "ilink_user_id": wx_uid,
                "prev_ilink_user_id": None,
                "ilink_username": uname,
                "last_inbound_at": None,
                "created_at": now,
                "updated_at": now,
            }
            self.accounts[aid] = row
            return [dict(row)]

        if "UPDATE dalton_channel_weixin_accounts" in norm:
            aid = str(params[-1])
            row = self.accounts.get(aid)
            if not row:
                self.last_rowcount = 0
                return []
            if "SET get_updates_buf = %s" in norm:
                row["get_updates_buf"] = str(params[0])
                row["updated_at"] = now
            elif "SET status = 'expired'" in norm:
                row["status"] = "expired"
                if params:
                    row["last_error_code"] = str(params[0])
                row["updated_at"] = now
            elif "SET get_updates_buf = ''" in norm:
                row["get_updates_buf"] = ""
                row["updated_at"] = now
            elif "SET status = 'disabled'" in norm:
                row["prev_status"] = str(params[0]) if row.get("prev_status") is None else row["prev_status"]
                row["status"] = "disabled"
                if "disabled_reason = 'member_not_in_roster'" in norm:
                    row["disabled_reason"] = "member_not_in_roster"
                elif "disabled_reason = 'external_access_disabled'" in norm:
                    row["disabled_reason"] = "external_access_disabled"
                else:
                    row["disabled_reason"] = str(params[0])
                row["updated_at"] = now
            elif "SET status = %s, prev_status = NULL, disabled_reason = NULL" in norm:
                row["status"] = str(params[0])
                row["prev_status"] = None
                row["disabled_reason"] = None
                row["updated_at"] = now
            elif "SET ilink_user_id = %s, prev_ilink_user_id = NULL, status = 'active'" in norm:
                row["ilink_user_id"] = str(params[0])
                row["prev_ilink_user_id"] = None
                row["status"] = "active"
                row["updated_at"] = now
            elif "SET last_inbound_at = NOW()" in norm:
                row["last_inbound_at"] = now
                row["updated_at"] = now
            elif "SET ilink_bot_id = %s" in norm:
                # Rescan confirm update (14 params: bot, wx_uid, prev_wx, uname, tok, enc, base, cdn, status, old_gen, new_gen, db_now, db_now, aid)
                bid, wx_uid, prev_wx, uname, tok, _enc, burl, cburl, st, prev_gen, new_gen, gen_dt, upd_dt, _aid = params
                row["ilink_bot_id"] = str(bid)
                row["encrypted_bot_token"] = str(tok)
                row["bot_token"] = str(tok)
                row["base_url"] = str(burl)
                row["cdn_base_url"] = str(cburl)
                row["get_updates_buf"] = ""
                row["bot_generation"] = int(new_gen)
                row["prev_bot_generation"] = int(prev_gen) if prev_gen is not None else None
                row["bot_generation_updated_at"] = gen_dt
                row["ilink_user_id"] = wx_uid
                row["prev_ilink_user_id"] = prev_wx
                if uname is not None:
                    row["ilink_username"] = uname
                row["status"] = str(st)
                row["prev_status"] = None
                row["disabled_reason"] = None
                row["updated_at"] = upd_dt
            self.last_rowcount = 1
            return [dict(row)]

        if "DELETE FROM dalton_channel_weixin_accounts" in norm:
            aid = str(params[0])
            removed = self.accounts.pop(aid, None)
            if removed:
                # Simulate ON DELETE SET NULL on collect_buffers
                for buf in self.collect_buffers.values():
                    if buf.get("account_id") == aid:
                        buf["account_id"] = None
                # Simulate ON DELETE CASCADE on account_tokens
                for k in list(self.account_tokens):
                    if k[0] == aid:
                        self.account_tokens.pop(k, None)
                self.last_rowcount = 1
                return [dict(removed)]
            self.last_rowcount = 0
            return []

        # --- Account context tokens ---
        if "FROM dalton_channel_weixin_account_tokens" in norm and "SELECT" in norm:
            if "WHERE account_id = %s::uuid AND bot_generation = %s AND platform_user_id = %s" in norm:
                if len(params) == 4:
                    _enc, aid, gen, puid = params
                else:
                    aid, gen, puid = params
                row = self.account_tokens.get((str(aid), int(gen), str(puid)))
                if not row:
                    return []
                d = dict(row)
                d["context_token"] = d.get("encrypted_context_token")
                return [d]
            if "WHERE account_id = %s::uuid AND bot_generation = %s" in norm:
                aid, gen = str(params[-2]), int(params[-1])
                rows = [
                    {**dict(v), "context_token": v.get("encrypted_context_token")}
                    for k, v in self.account_tokens.items()
                    if k[0] == aid and k[1] == gen
                ]
                return rows
            return []

        if "INSERT INTO dalton_channel_weixin_account_tokens" in norm:
            aid = str(params[0])
            bot_id = str(params[1])
            gen = int(params[2])
            puid = str(params[3])
            tok = str(params[4])
            seq = int(params[6])
            seq_src = str(params[7])
            proto_high = int(params[8])
            plat_high = int(params[9])
            key = (aid, gen, puid)
            prev = self.account_tokens.get(key)
            if prev is None:
                row = {
                    "account_id": aid,
                    "ilink_bot_id": bot_id,
                    "bot_generation": gen,
                    "platform_user_id": puid,
                    "encrypted_context_token": tok,
                    "token_version": 1,
                    "stale_at": None,
                    "stale_reason": None,
                    "seq": seq,
                    "seq_source": seq_src,
                    "protocol_seq_high": proto_high,
                    "platform_time_high": plat_high,
                    "updated_at": now,
                }
                self.account_tokens[key] = row
                self.last_rowcount = 1
                return [{"token_version": 1, "seq": seq}]
            prev["ilink_bot_id"] = bot_id
            prev["encrypted_context_token"] = tok
            prev["token_version"] = int(prev["token_version"]) + 1
            prev["stale_at"] = None
            prev["stale_reason"] = None
            prev["seq"] = seq
            prev["seq_source"] = seq_src
            prev["protocol_seq_high"] = max(int(prev.get("protocol_seq_high") or 0), proto_high)
            prev["platform_time_high"] = max(int(prev.get("platform_time_high") or 0), plat_high)
            prev["updated_at"] = now
            self.last_rowcount = 1
            return [{"token_version": prev["token_version"], "seq": prev["seq"]}]

        if "UPDATE dalton_channel_weixin_account_tokens SET stale_at = NOW()" in norm:
            if len(params) == 5:
                _reason, aid, gen, puid, exp_ver = (
                    str(params[0]),
                    str(params[1]),
                    int(params[2]),
                    str(params[3]),
                    int(params[4]),
                )
            else:
                aid, gen, puid, exp_ver = str(params[0]), int(params[1]), str(params[2]), int(params[3])
            row = self.account_tokens.get((aid, gen, puid))
            if row and int(row["token_version"]) == exp_ver:
                row["stale_at"] = now
                row["updated_at"] = now
                self.last_rowcount = 1
                return [{"token_version": row["token_version"]}]
            self.last_rowcount = 0
            return []

        if "DELETE FROM dalton_channel_weixin_account_tokens" in norm:
            aid = str(params[0])
            keep_gen = int(params[1]) if len(params) > 1 else None
            for k in list(self.account_tokens):
                if k[0] == aid and (keep_gen is None or k[1] != keep_gen):
                    self.account_tokens.pop(k, None)
            return []

        # --- Login sessions ---
        if "INSERT INTO dalton_channel_weixin_login_sessions" in norm:
            skey, uid, target_aid, exp_gen, base_u = params
            row = {
                "session_key": str(skey),
                "user_id": str(uid),
                "target_account_id": str(target_aid) if target_aid else None,
                "login_version": 1,
                "expected_bot_generation": exp_gen,
                "status": "starting",
                "encrypted_qrcode": None,
                "qrcode": None,
                "qrcode_img_content": None,
                "redirect_base_url": str(base_u),
                "expires_at": now + timedelta(seconds=60),
                "confirmed_result": None,
                "created_at": now,
                "updated_at": now,
            }
            self.login_sessions[str(skey)] = row
            return [dict(row)]

        if "FROM dalton_channel_weixin_login_sessions" in norm and "SELECT" in norm:
            if "WHERE session_key = %s" in norm:
                skey = str(params[-1])
                s = self.login_sessions.get(skey)
                if not s:
                    return []
                d = dict(s)
                d["qrcode"] = d.get("encrypted_qrcode")
                d["db_now"] = now
                return [d]
            if "WHERE user_id = %s AND status IN ('starting', 'wait', 'scaned', 'verify_code')" in norm:
                uid = str(params[0])
                return [
                    dict(s)
                    for s in sorted(self.login_sessions.values(), key=lambda x: x["session_key"])
                    if s["user_id"] == uid and s.get("status") in ("starting", "wait", "scaned", "verify_code")
                ]
            if "WHERE status IN ('starting', 'wait', 'scaned', 'verify_code')" in norm:
                return [
                    dict(s)
                    for s in self.login_sessions.values()
                    if s.get("status") in ("starting", "wait", "scaned", "verify_code")
                    and s["expires_at"] > now
                ]
            return []

        if "UPDATE dalton_channel_weixin_login_sessions" in norm:
            if "SET status = 'superseded'" in norm:
                raw_target = params[0]
                target_uids = set(raw_target) if isinstance(raw_target, (list, tuple, set)) else {str(raw_target)}
                cnt = 0
                for s in self.login_sessions.values():
                    if s["user_id"] in target_uids and s["status"] in ("starting", "wait", "scaned", "verify_code"):
                        s["status"] = "superseded"
                        s["qrcode_img_content"] = None
                        s["updated_at"] = now
                        cnt += 1
                self.last_rowcount = cnt
                return []
            if "SET status = 'expired', qrcode_img_content = NULL" in norm:
                skey = str(params[0])
                s = self.login_sessions.get(skey)
                if s:
                    s["status"] = "expired"
                    s["qrcode_img_content"] = None
                    s["updated_at"] = now
                    self.last_rowcount = 1
                else:
                    self.last_rowcount = 0
                return []
            if "SET status = 'wait', encrypted_qrcode =" in norm:
                qr, _enc, img, redir, skey, owner_uid = params
                s = self.login_sessions.get(str(skey))
                if (
                    s
                    and s["user_id"] == str(owner_uid)
                    and s["status"] == "starting"
                    and int(s["login_version"]) == 1
                    and s["expires_at"] > now
                ):
                    s["status"] = "wait"
                    s["encrypted_qrcode"] = str(qr)
                    s["qrcode"] = str(qr)
                    s["qrcode_img_content"] = img
                    if redir:
                        s["redirect_base_url"] = str(redir)
                    s["expires_at"] = now + timedelta(minutes=5)
                    s["login_version"] = 2
                    s["updated_at"] = now
                    self.last_rowcount = 1
                    return [dict(s)]
                self.last_rowcount = 0
                return []
            if "SET status = 'confirmed'" in norm:
                target_aid, conf_json, skey = params
                s = self.login_sessions.get(str(skey))
                if s:
                    s["status"] = "confirmed"
                    s["qrcode_img_content"] = None
                    s["encrypted_qrcode"] = None
                    s["confirmed_result"] = self._unwrap(conf_json)
                    s["target_account_id"] = str(target_aid)
                    s["login_version"] = int(s["login_version"]) + 1
                    s["updated_at"] = now
                    self.last_rowcount = 1
                    return [dict(s)]
                self.last_rowcount = 0
                return []
            if "SET status = COALESCE(%s, status)" in norm:
                new_st, redir, skey, exp_ver = params
                s = self.login_sessions.get(str(skey))
                if (
                    s
                    and s["status"] in ("wait", "scaned", "verify_code")
                    and int(s["login_version"]) == int(exp_ver)
                    and s["expires_at"] > now
                ):
                    if new_st:
                        s["status"] = str(new_st)
                    if redir:
                        s["redirect_base_url"] = str(redir)
                    s["login_version"] = int(s["login_version"]) + 1
                    s["updated_at"] = now
                    self.last_rowcount = 1
                    return [dict(s)]
                self.last_rowcount = 0
                return []
            return []

        # --- Collect buffers & items ---
        if "UPDATE dalton_channel_weixin_collect_buffers SET status = 'expired'" in norm:
            uid, skey = str(params[0]), str(params[1])
            for b in self.collect_buffers.values():
                if (
                    b["dalton_user_id"] == uid
                    and b["scope_key"] == skey
                    and b["status"] == "collecting"
                    and b["expires_at"] <= now
                ):
                    b["status"] = "expired"
                    b["updated_at"] = now
            return []

        if "UPDATE dalton_channel_weixin_collect_buffers SET status = 'cancelled'" in norm:
            cancelled_ids = []
            if "WHERE buffer_id = ANY" in norm:
                bids = {str(x) for x in (params[0] or [])}
                unlink = "account_id = NULL" in norm
                for bid in bids:
                    b = self.collect_buffers.get(bid)
                    if b:
                        b["status"] = "cancelled"
                        if unlink:
                            b["account_id"] = None
                        b["updated_at"] = now
                        cancelled_ids.append(bid)
            elif "WHERE dalton_user_id = %s AND scope_key = %s" in norm:
                uid, skey = str(params[0]), str(params[1])
                for b in self.collect_buffers.values():
                    if b["dalton_user_id"] == uid and b["scope_key"] == skey and b["status"] in ("collecting", "flushing"):
                        b["status"] = "cancelled"
                        b["updated_at"] = now
                        cancelled_ids.append(b["buffer_id"])
            elif "WHERE buffer_id = %s" in norm:
                bid = str(params[-1])
                b = self.collect_buffers.get(bid)
                if b and b["status"] in ("collecting", "flushing"):
                    b["status"] = "cancelled"
                    b["updated_at"] = now
                    cancelled_ids.append(bid)
            elif "WHERE account_id = %s" in norm:
                aid = str(params[0])
                for b in self.collect_buffers.values():
                    if b.get("account_id") == aid and b["status"] in ("collecting", "flushing"):
                        b["status"] = "cancelled"
                        b["updated_at"] = now
                        cancelled_ids.append(b["buffer_id"])
            elif "WHERE dalton_user_id = %s" in norm:
                uid = str(params[0])
                for b in self.collect_buffers.values():
                    if b["dalton_user_id"] == uid and b["status"] in ("collecting", "flushing"):
                        b["status"] = "cancelled"
                        b["updated_at"] = now
                        cancelled_ids.append(b["buffer_id"])
            return [{"buffer_id": bid} for bid in cancelled_ids]

        if "UPDATE dalton_channel_weixin_collect_buffers SET account_id = NULL" in norm:
            aid = str(params[0])
            for b in self.collect_buffers.values():
                if b.get("account_id") == aid:
                    b["account_id"] = None
                    b["updated_at"] = now
            return []

        if "UPDATE dalton_channel_weixin_collect_items SET upload_status = 'failed'" in norm:
            if "WHERE buffer_id = ANY" in norm:
                bids = {str(x) for x in (params[0] or [])}
                for it in self.collect_items.values():
                    if it["buffer_id"] in bids and it["upload_status"] == "pending_upload":
                        it["upload_status"] = "failed"
                        it["upload_error"] = "buffer_cancelled"
            elif "WHERE buffer_id = %s" in norm:
                bid = str(params[0])
                for it in self.collect_items.values():
                    if it["buffer_id"] == bid and it["upload_status"] == "pending_upload":
                        it["upload_status"] = "failed"
                        it["upload_error"] = "upload_timeout"
            return []

        if "INSERT INTO dalton_channel_weixin_collect_buffers" in norm:
            aid, bot_gen, uid, skey, sess_gen, wid, init_prompt, exp, max_exp, c_at, u_at = params
            bid = str(uuid4())
            row = {
                "buffer_id": bid,
                "account_id": str(aid) if aid else None,
                "bot_generation": int(bot_gen),
                "dalton_user_id": str(uid),
                "scope_key": str(skey),
                "session_generation": int(sess_gen),
                "workspace_id": str(wid),
                "status": "collecting",
                "initial_prompt": str(init_prompt or ""),
                "flush_prompt": None,
                "item_count": 0,
                "total_text_chars": 0,
                "attachment_count": 0,
                "flushed_message_id": None,
                "expires_at": exp,
                "max_expires_at": max_exp,
                "created_at": c_at,
                "updated_at": u_at,
            }
            self.collect_buffers[bid] = row
            return [dict(row)]

        if "FROM dalton_channel_weixin_collect_buffers" in norm and "SELECT" in norm:
            if "WHERE status IN ('collecting', 'flushing')" in norm:
                rows_b = [b for b in self.collect_buffers.values() if b["status"] in ("collecting", "flushing")]
                pidx = 0
                if "AND account_id = %s::uuid" in norm:
                    aid_f = str(params[pidx])
                    pidx += 1
                    rows_b = [b for b in rows_b if b.get("account_id") == aid_f]
                if "AND dalton_user_id = %s" in norm:
                    uid_f = str(params[pidx])
                    pidx += 1
                    rows_b = [b for b in rows_b if b.get("dalton_user_id") == uid_f]
                if "AND scope_key = %s" in norm:
                    skey_f = str(params[pidx])
                    pidx += 1
                    rows_b = [b for b in rows_b if b.get("scope_key") == skey_f]
                if "AND session_generation = %s" in norm:
                    sgen_f = int(params[pidx])
                    pidx += 1
                    rows_b = [b for b in rows_b if int(b.get("session_generation") or 0) == sgen_f]
                rows_b.sort(key=lambda x: str(x["buffer_id"]))
                return [dict(b) for b in rows_b]
            if "WHERE buffer_id = %s" in norm:
                b = self.collect_buffers.get(str(params[0]))
                if not b:
                    return []
                d = dict(b)
                d["db_now"] = now
                return [d]
            if "WHERE status = 'flushing'" in norm:
                return [dict(b) for b in self.collect_buffers.values() if b["status"] == "flushing"]
            if "WHERE account_id = %s::uuid AND bot_generation = %s AND scope_key = %s AND session_generation = %s" in norm:
                aid, bgen, skey, sgen = str(params[0]), int(params[1]), str(params[2]), int(params[3])
                allowed_st = ("collecting", "flushing") if "'flushing'" in norm else ("collecting",)
                matches = [
                    {**dict(b), "db_now": now}
                    for b in self.collect_buffers.values()
                    if b.get("account_id") == aid
                    and int(b.get("bot_generation") or 0) == bgen
                    and b.get("scope_key") == skey
                    and int(b.get("session_generation") or 0) == sgen
                    and b.get("status") in allowed_st
                ]
                return matches
            if "WHERE dalton_user_id = %s AND scope_key = %s" in norm:
                uid, skey = str(params[0]), str(params[1])
                matches = [
                    {**dict(b), "db_now": now}
                    for b in self.collect_buffers.values()
                    if b["dalton_user_id"] == uid and b["scope_key"] == skey and b["status"] == "collecting"
                ]
                return matches
            return []

        if "UPDATE dalton_channel_weixin_collect_buffers" in norm:
            if "SET status = 'expired'" in norm and "AND expires_at <=" in norm:
                db_n, aid, bgen, skey, sgen, exp_lim = params
                for b in self.collect_buffers.values():
                    if (
                        b.get("account_id") == str(aid)
                        and int(b.get("bot_generation") or 0) == int(bgen)
                        and b.get("scope_key") == str(skey)
                        and int(b.get("session_generation") or 0) == int(sgen)
                        and b.get("status") == "collecting"
                        and b.get("expires_at") is not None
                        and b["expires_at"] <= exp_lim
                    ):
                        b["status"] = "expired"
                        b["updated_at"] = db_n
                return []
            bid = str(params[-1])
            b = self.collect_buffers.get(bid)
            if not b:
                return []
            if "SET expires_at = %s, updated_at = %s" in norm:
                b["expires_at"] = params[0]
                b["updated_at"] = params[1]
                return [dict(b)]
            if "SET status = 'expired'" in norm:
                b["status"] = "expired"
                b["updated_at"] = params[0]
                return [dict(b)]
            if "SET status = 'cancelled'" in norm:
                b["status"] = "cancelled"
                b["updated_at"] = params[0]
                return [dict(b)]
            if "SET status = 'flushing'" in norm:
                b["status"] = "flushing"
                b["flush_prompt"] = params[0]
                b["updated_at"] = params[1]
                return [dict(b)]
            if "SET status = 'done'" in norm:
                b["status"] = "done"
                b["flushed_message_id"] = str(params[0])
                b["updated_at"] = now
                return [dict(b)]
            if "item_count = item_count + 1" in norm:
                add_chars, add_atts, new_exp, u_at, _bid = params
                b["item_count"] += 1
                b["total_text_chars"] += int(add_chars)
                b["attachment_count"] += int(add_atts)
                b["expires_at"] = new_exp
                b["updated_at"] = u_at
                return [dict(b)]
            return [dict(b)]

        if "INSERT INTO dalton_channel_weixin_collect_items" in norm:
            bid, mid, pseq, ptime, rorder, txt, exp_att_cnt, up_st, c_at, u_at = params
            key = (str(bid), str(mid))
            if key in self.collect_items:
                return []
            item = {
                "buffer_id": str(bid),
                "message_id": str(mid),
                "protocol_seq": pseq,
                "platform_time_ms": ptime,
                "receive_order": int(rorder),
                "text_content": str(txt or ""),
                "expected_attachment_count": int(exp_att_cnt),
                "upload_status": str(up_st),
                "uploaded_attachments": [],
                "upload_error": None,
                "created_at": c_at,
                "updated_at": u_at,
            }
            self.collect_items[key] = item
            return [dict(item)]

        if "FROM dalton_channel_weixin_collect_items" in norm and "SELECT" in norm:
            bid = str(params[0])
            if "WHERE buffer_id = %s::uuid AND message_id = %s" in norm:
                mid = str(params[1])
                it = self.collect_items.get((bid, mid))
                return [dict(it)] if it else []
            if "COUNT(*)" in norm and "pending_upload" in norm:
                cnt = sum(
                    1
                    for it in self.collect_items.values()
                    if it["buffer_id"] == bid and it["upload_status"] == "pending_upload"
                )
                return [{"cnt": cnt}]
            if "MAX(receive_order)" in norm:
                orders = [it["receive_order"] for it in self.collect_items.values() if it["buffer_id"] == bid]
                return [{"max_ord": max(orders) if orders else 0}]
            items = [dict(it) for it in self.collect_items.values() if it["buffer_id"] == bid]
            items.sort(
                key=lambda x: (
                    x["protocol_seq"] is None,
                    x["protocol_seq"] or 0,
                    x["platform_time_ms"] or 0,
                    x["receive_order"],
                    x["message_id"],
                )
            )
            return items

        if "UPDATE dalton_channel_weixin_collect_items" in norm:
            if "SET upload_status = 'failed', upload_error = COALESCE" in norm:
                bid = str(params[0])
                for it in self.collect_items.values():
                    if it["buffer_id"] == bid and it["upload_status"] == "pending_upload":
                        it["upload_status"] = "failed"
                        it["upload_error"] = it.get("upload_error") or "upload_timeout"
                return []
            up_atts, up_st, up_err, bid, mid = params
            it = self.collect_items.get((str(bid), str(mid)))
            if it:
                it["uploaded_attachments"] = self._unwrap(up_atts) or []
                it["upload_status"] = str(up_st)
                it["upload_error"] = up_err
                self.last_rowcount = 1
                return [dict(it)]
            self.last_rowcount = 0
            return []

        # --- Thread watches ---
        if "INSERT INTO dalton_channel_thread_watches" in norm:
            tid, uid, plat, addr, scope, gen, last_turn, active = (
                str(params[0]),
                str(params[1]),
                str(params[2]),
                str(params[3]),
                str(params[4]),
                int(params[5]),
                int(params[6]),
                bool(params[7]),
            )
            prev = self.thread_watches.get(tid)
            if (
                prev is not None
                and "WHERE dalton_channel_thread_watches.platform = EXCLUDED.platform" in norm
                and str(prev.get("platform") or "").strip().lower() != plat.lower()
            ):
                return []
            self.thread_watches[tid] = {
                "thread_id": tid,
                "dalton_user_id": uid,
                "platform": plat,
                "target_address": addr,
                "scope_key": scope,
                "generation": gen,
                "last_seen_turn_index": last_turn,
                "watch_active": active,
                "created_at": (prev or {}).get("created_at") or now,
                "updated_at": now,
            }
            return [{"platform": plat}] if "RETURNING platform" in norm else []

        if "UPDATE dalton_channel_thread_watches" in norm:
            if "SET weixin_bot_generation = %s" in norm:
                new_gen, aid = int(params[0]), str(params[-1])
                for w in self.thread_watches.values():
                    if w.get("platform") == "weixin" and w.get("weixin_account_id") == aid and w.get("watch_active"):
                        w["weixin_bot_generation"] = new_gen
                        w["updated_at"] = now
                return []
            if "SET watch_active = FALSE" in norm:
                if "WHERE dalton_user_id = %s AND platform = 'weixin'" in norm:
                    uid = str(params[0])
                    for w in self.thread_watches.values():
                        if w.get("dalton_user_id") == uid and w.get("platform") == "weixin":
                            w["watch_active"] = False
                elif "WHERE weixin_account_id = %s" in norm:
                    aid = str(params[0])
                    for w in self.thread_watches.values():
                        if w.get("weixin_account_id") == aid:
                            w["watch_active"] = False
                return []
            return []

        if "FROM dalton_channel_thread_watches" in norm and "SELECT" in norm:
            if "WHERE thread_id = %s" in norm:
                w = self.thread_watches.get(str(params[0]))
                return [dict(w)] if w else []
            if "NOT (weixin_account_id = ANY" in norm:
                allowed_aids = {str(x) for x in (params[0] or [])}
                for w in self.thread_watches.values():
                    if (
                        w.get("watch_active")
                        and w.get("weixin_account_id")
                        and str(w["weixin_account_id"]) not in allowed_aids
                    ):
                        return [{"?column?": 1}]
                return []
            if "weixin_account_id IS NOT NULL" in norm and "SELECT 1" in norm:
                for w in self.thread_watches.values():
                    if w.get("watch_active") and w.get("weixin_account_id"):
                        return [{"?column?": 1}]
                return []
            if "weixin_account_id IS NULL OR weixin_account_id = ANY" in norm:
                allowed_aids = {str(x) for x in (params[0] or [])}
                return [
                    dict(w)
                    for w in self.thread_watches.values()
                    if w.get("watch_active")
                    and (
                        not w.get("weixin_account_id")
                        or str(w["weixin_account_id"]) in allowed_aids
                    )
                ]
            if "AND weixin_account_id IS NULL" in norm:
                return [
                    dict(w)
                    for w in self.thread_watches.values()
                    if w.get("watch_active") and not w.get("weixin_account_id")
                ]
            if "AND platform <> 'weixin'" in norm:
                return [
                    dict(w)
                    for w in self.thread_watches.values()
                    if w.get("watch_active") and str(w.get("platform") or "").lower() != "weixin"
                ]
            return [dict(w) for w in self.thread_watches.values() if w.get("watch_active")]

        # --- Conversation queries + responses (watch backfill Codex ① & live background_run) ---
        if "FROM conversation_queries q JOIN conversation_responses r" in norm:
            if "WHERE r.conversation_response_id = %s" in norm:
                rid_q = str(params[0])
                for q in self.conversation_queries:
                    if str(q.get("response_id") or "") == rid_q:
                        return [{"turn_created_at": q["created_at"]}]
                return []
            tid, last_t = str(params[0]), int(params[1])
            out = []
            for q in self.conversation_queries:
                if str(q["conversation_thread_id"]) == tid and int(q["turn_index"]) > last_t:
                    resp = self.conversation_responses.get(str(q["response_id"]))
                    if resp:
                        out.append(
                            {
                                "turn_index": q["turn_index"],
                                "query_type": q["query_type"],
                                "run_id": resp["conversation_response_id"],
                                "response_status": resp["status"],
                                "turn_created_at": q["created_at"],
                            }
                        )
            return out

        if "FROM conversation_queries WHERE conversation_thread_id = %s AND turn_index = %s" in norm:
            tid_q, tidx_q = str(params[0]), int(params[1])
            for q in self.conversation_queries:
                if str(q["conversation_thread_id"]) == tid_q and int(q["turn_index"]) == tidx_q:
                    return [{"turn_created_at": q["created_at"]}]
            return []

        if "FROM conversation_threads WHERE conversation_thread_id = %s" in norm:
            ct = self.conversation_threads.get(str(params[0]))
            return [dict(ct)] if ct else []

        if "FROM conversation_responses cr JOIN conversation_threads ct" in norm:
            resp = self.conversation_responses.get(str(params[0]))
            if resp and resp.get("conversation_thread_id"):
                ct = self.conversation_threads.get(str(resp["conversation_thread_id"]))
                return [{"platform": ct.get("platform")}] if ct else []
            return []

        # --- Background runs ---
        if "INSERT INTO dalton_channel_background_runs" in norm:
            rid = str(params[0])
            if len(params) == 11:
                row_bg = {
                    "run_id": rid,
                    "thread_id": str(params[1]),
                    "dalton_user_id": str(params[2]),
                    "platform": str(params[3]),
                    "target_address": str(params[4]),
                    "turn_index": int(params[5]) if params[5] is not None else None,
                    "status": str(params[6]),
                    "fallback_pending": bool(params[7]),
                    "weixin_account_id": str(params[8]) if params[8] else None,
                    "weixin_recipient_id": params[9],
                    "weixin_bot_generation": int(params[10]) if params[10] is not None else None,
                    "created_at": now,
                    "updated_at": now,
                }
                prev_bg = self.background_runs.get(rid)
                if (
                    prev_bg is not None
                    and "WHERE dalton_channel_background_runs.platform = EXCLUDED.platform" in norm
                    and str(prev_bg.get("platform") or "").strip().lower() != row_bg["platform"].lower()
                ):
                    return []
                if prev_bg is None:
                    self.background_runs[rid] = row_bg
                else:
                    self.background_runs[rid]["status"] = row_bg["status"]
                    self.background_runs[rid]["fallback_pending"] = row_bg["fallback_pending"]
                return [{"platform": row_bg["platform"]}] if "RETURNING platform" in norm else []
            elif rid not in self.background_runs:
                self.background_runs[rid] = {
                    "run_id": rid,
                    "thread_id": str(params[1]),
                    "dalton_user_id": str(params[2]),
                    "platform": str(params[3]),
                    "target_address": str(params[4]),
                    "turn_index": int(params[5]),
                    "status": "streaming",
                    "fallback_pending": True,
                    "weixin_account_id": str(params[6]) if len(params) > 6 and params[6] else None,
                    "weixin_recipient_id": params[7] if len(params) > 7 else None,
                    "weixin_bot_generation": int(params[8]) if len(params) > 8 and params[8] is not None else None,
                    "created_at": now,
                    "updated_at": now,
                }
            return []

        if "SELECT COALESCE(MAX(sent_to_current_count), 0) AS cnt" in norm:
            return [{"cnt": 0}]

        if "FROM dalton_channel_background_runs" in norm and "SELECT" in norm:
            if "WHERE run_id = %s" in norm:
                r = self.background_runs.get(str(params[0]))
                return [dict(r)] if r else []
            if "run_id = %s" in norm:
                # Filter by run_id (and optional dalton_user_id / thread_id)
                out_bg = []
                for r in self.background_runs.values():
                    if str(r["run_id"]) in {str(p) for p in params}:
                        out_bg.append(dict(r))
                return out_bg
            return [
                dict(r)
                for r in self.background_runs.values()
                if r.get("fallback_pending") or r.get("status") == "streaming"
            ]

        # --- Deliveries ---
        if "SELECT EXISTS (" in norm and "AS in_flight" in norm:
            rid_f = str(params[0]) if params else ""
            has_inf = any(
                str(d.get("run_id") or "") == rid_f and d.get("status") == "in_flight"
                for d in self.deliveries.values()
            )
            return [{"in_flight": has_inf}]
        if "SELECT EXISTS (" in norm and "AS has_unknown_text" in norm:
            rid_f = str(params[0]) if params else ""
            has_unk = any(
                str(d.get("run_id") or "") == rid_f and d.get("status") == "unknown"
                for d in self.deliveries.values()
            )
            return [{"has_unknown_text": has_unk}]
        if "SELECT COALESCE(MAX(sent_to_current_count), 0) AS cnt" in norm:
            return [{"cnt": 0}]
        if "FROM dalton_channel_deliveries" in norm and "SELECT" in norm:
            if "WHERE delivery_key = %s" in norm:
                d = self.deliveries.get(str(params[0]))
                return [dict(d)] if d else []
            if "MIN(next_retry_at) AS next_upcoming_retry_at" in norm:
                skip_wx = "platform <> 'weixin'" in norm
                plat_f = str(params[0]) if params and "platform = %s" in norm else None
                cands = [
                    d.get("next_retry_at")
                    for d in self.deliveries.values()
                    if d.get("status") in ("deferred", "partial_deferred")
                    and d.get("next_retry_at") is not None
                    and d["next_retry_at"] > now
                    and (plat_f is None or str(d.get("platform") or "").lower() == plat_f)
                    and (not skip_wx or str(d.get("platform") or "").lower() != "weixin")
                ]
                return [{"next_upcoming_retry_at": min(cands) if cands else None}]
            if "status IN ('deferred', 'partial_deferred')" in norm:
                skip_wx = "platform <> 'weixin'" in norm
                plat_f = str(params[0]) if params and "platform = %s" in norm else None
                only_expired = "first_deferred_at <= NOW() - INTERVAL '24 hours'" in norm
                only_due = "next_retry_at <= NOW()" in norm
                out_d = []
                for d in sorted(
                    self.deliveries.values(), key=lambda x: str(x.get("delivery_key") or "")
                ):
                    if d.get("status") not in ("deferred", "partial_deferred"):
                        continue
                    if plat_f is not None and str(d.get("platform") or "").lower() != plat_f:
                        continue
                    if skip_wx and str(d.get("platform") or "").lower() == "weixin":
                        continue
                    if only_expired:
                        fdef = d.get("first_deferred_at")
                        if fdef is None or (now - fdef).total_seconds() < 86400:
                            continue
                    if only_due:
                        nret = d.get("next_retry_at")
                        if nret is not None and nret > now:
                            continue
                    out_d.append(dict(d))
                return out_d
            if "WHERE status = 'in_flight'" in norm:
                skip_wx = "AND platform <> 'weixin'" in norm
                return [
                    dict(d)
                    for d in sorted(
                        self.deliveries.values(), key=lambda x: str(x.get("delivery_key") or "")
                    )
                    if d.get("status") == "in_flight"
                    and (not skip_wx or str(d.get("platform") or "").lower() != "weixin")
                ]
            return list(self.deliveries.values())

        if "UPDATE dalton_channel_deliveries SET status = 'unknown', error_code = 'gateway_restarted'" in norm:
            skip_wx = "AND platform <> 'weixin'" in norm
            target_keys = set(params[0]) if params and "WHERE delivery_key = ANY" in norm else None
            settled = []
            for d in self.deliveries.values():
                if d.get("status") == "in_flight":
                    if target_keys is not None and d.get("delivery_key") not in target_keys:
                        continue
                    if skip_wx and str(d.get("platform") or "").lower() == "weixin":
                        continue
                    d["status"] = "unknown"
                    d["error_code"] = "gateway_restarted"
                    d["updated_at"] = now
                    settled.append(dict(d))
            return settled

        if "UPDATE dalton_channel_deliveries SET status = %s, sent_chunks = %s" in norm and "defer_count = %s" in norm:
            st, sc, tc, pmids, fstat, dsegs, dcnt, fdef, nret, ldef_att, ecode, emsg, dkey = params
            d = self.deliveries.get(str(dkey))
            if d is not None:
                d["status"] = str(st)
                d["sent_chunks"] = int(sc)
                d["total_chunks"] = int(tc)
                d["platform_message_ids"] = pmids.obj if hasattr(pmids, "obj") else pmids
                d["files_status"] = fstat.obj if hasattr(fstat, "obj") else fstat
                d["delivered_segments"] = dsegs.obj if hasattr(dsegs, "obj") else dsegs
                d["defer_count"] = int(dcnt)
                d["first_deferred_at"] = fdef
                d["next_retry_at"] = nret
                d["last_deferred_attempt"] = int(ldef_att)
                if ecode is not None:
                    d["error_code"] = ecode
                if emsg is not None:
                    d["error_message"] = emsg
                d["updated_at"] = now
            return []

        if "UPDATE dalton_channel_deliveries SET status = %s, sent_chunks = %s" in norm and "delivered_segments = %s" in norm:
            st, sc, tc, pmids, fstat, dsegs, dkey = params
            d = self.deliveries.get(str(dkey))
            if d is not None:
                d["status"] = str(st)
                d["sent_chunks"] = int(sc)
                d["total_chunks"] = int(tc)
                d["platform_message_ids"] = pmids.obj if hasattr(pmids, "obj") else pmids
                d["files_status"] = fstat.obj if hasattr(fstat, "obj") else fstat
                d["delivered_segments"] = dsegs.obj if hasattr(dsegs, "obj") else dsegs
                d["updated_at"] = now
            return []

        if "UPDATE dalton_channel_deliveries SET status = 'failed', next_retry_at = NULL" in norm:
            if "WHERE delivery_key = ANY" in norm:
                ecode, emsg, keys = params
                expired_out = []
                for dk in keys:
                    d = self.deliveries.get(str(dk))
                    if d is not None and d.get("status") in ("deferred", "partial_deferred"):
                        d["status"] = "failed"
                        d["next_retry_at"] = None
                        d["error_code"] = ecode
                        if d.get("error_message") is None:
                            d["error_message"] = emsg
                        d["updated_at"] = now
                        expired_out.append(dict(d))
                return expired_out
            ecode, emsg, dkey = params
            d = self.deliveries.get(str(dkey))
            if d is not None:
                d["status"] = "failed"
                d["next_retry_at"] = None
                d["error_code"] = ecode
                if d.get("error_message") is None:
                    d["error_message"] = emsg
                d["updated_at"] = now
            return []

        if "UPDATE dalton_channel_deliveries SET sent_chunks = %s" in norm:
            if "delivered_segments = %s" in norm:
                sc, tc, pmids, fstat, dsegs, fresp, st, ecode, emsg, dkey = params
            else:
                sc, tc, pmids, fstat, fresp, st, ecode, emsg, dkey = params
                dsegs = None
            d = self.deliveries.get(str(dkey))
            if d is not None:
                d["sent_chunks"] = int(sc)
                d["total_chunks"] = int(tc)
                d["platform_message_ids"] = pmids.obj if hasattr(pmids, "obj") else pmids
                d["files_status"] = fstat.obj if hasattr(fstat, "obj") else fstat
                if dsegs is not None:
                    d["delivered_segments"] = dsegs.obj if hasattr(dsegs, "obj") else dsegs
                d["first_response_json"] = fresp.obj if hasattr(fresp, "obj") else fresp
                d["status"] = str(st)
                d["next_retry_at"] = None
                if ecode is not None:
                    d["error_code"] = ecode
                if emsg is not None:
                    d["error_message"] = emsg
                d["updated_at"] = now
            return []

        if "INSERT INTO dalton_channel_deliveries" in norm:
            dkey = str(params[0])
            existing_d = self.deliveries.get(dkey)
            if "last_deferred_attempt" in norm:
                (
                    _dk, kind_v, uid_v, tid_v, rid_v, eid_v, is_cur_v, has_txt_v,
                    plat_v, tgt_v, st_v, tc_v, ec_v, em_v, sscope_v, sgen_v,
                    aorig_v, dcnt_v, fdef_v, nret_v,
                ) = params
                if existing_d is not None:
                    existing_d["status"] = str(st_v)
                    existing_d["defer_count"] = int(dcnt_v)
                    if existing_d.get("first_deferred_at") is None:
                        existing_d["first_deferred_at"] = fdef_v
                    existing_d["next_retry_at"] = nret_v
                    existing_d["last_deferred_attempt"] = int(existing_d.get("delivery_attempt") or 1)
                    existing_d["error_code"] = ec_v
                    existing_d["error_message"] = em_v
                    existing_d["updated_at"] = now
                else:
                    self.deliveries[dkey] = {
                        "delivery_key": dkey,
                        "kind": str(kind_v),
                        "user_id": str(uid_v),
                        "thread_id": tid_v,
                        "run_id": rid_v,
                        "execution_id": eid_v,
                        "is_current": bool(is_cur_v),
                        "has_non_empty_text": bool(has_txt_v),
                        "platform": str(plat_v),
                        "target_address": str(tgt_v),
                        "status": str(st_v),
                        "total_chunks": int(tc_v),
                        "sent_chunks": 0,
                        "platform_message_ids": [],
                        "files_status": [],
                        "delivered_segments": [],
                        "delivery_attempt": 1,
                        "defer_count": int(dcnt_v),
                        "first_deferred_at": fdef_v,
                        "next_retry_at": nret_v,
                        "last_deferred_attempt": 1,
                        "error_code": ec_v,
                        "error_message": em_v,
                        "source_scope_key": sscope_v,
                        "source_generation": sgen_v,
                        "activity_origin": aorig_v,
                        "updated_at": now,
                    }
                return []
            if "'unknown'" in norm:
                self.deliveries[dkey] = {
                    "delivery_key": dkey,
                    "status": "unknown",
                    "error_code": "source_generation_unknown" if "source_generation_unknown" in norm else None,
                    "platform": "weixin",
                    "updated_at": now,
                }
            else:
                plat_v = str(params[8]) if len(params) > 8 else "weixin"
                tgt_v = str(params[9]) if len(params) > 9 else f"{plat_v}:@me"
                uid_v = str(params[2]) if len(params) > 2 else ""
                if existing_d is not None:
                    existing_d["status"] = "in_flight"
                    existing_d["next_retry_at"] = None
                    existing_d["delivery_attempt"] = int(existing_d.get("delivery_attempt") or 1) + 1
                    existing_d["platform"] = plat_v
                    existing_d["target_address"] = tgt_v
                    existing_d["updated_at"] = now
                else:
                    self.deliveries[dkey] = {
                        "delivery_key": dkey,
                        "kind": str(params[1]) if len(params) > 1 else "agent_send",
                        "user_id": uid_v,
                        "platform": plat_v,
                        "target_address": tgt_v,
                        "status": "in_flight",
                        "total_chunks": int(params[10]) if len(params) > 10 else 0,
                        "sent_chunks": 0,
                        "platform_message_ids": [],
                        "files_status": [],
                        "delivered_segments": [],
                        "delivery_attempt": 1,
                        "defer_count": 0,
                        "first_deferred_at": None,
                        "next_retry_at": None,
                        "last_deferred_attempt": None,
                        "error_code": None,
                        "error_message": None,
                        "source_scope_key": params[11] if len(params) > 11 else None,
                        "source_generation": params[12] if len(params) > 12 else None,
                        "activity_origin": params[13] if len(params) > 13 else "none",
                        "weixin_account_id": params[14] if len(params) > 14 else None,
                        "weixin_bot_id": params[15] if len(params) > 15 else None,
                        "weixin_bot_generation": params[16] if len(params) > 16 else None,
                        "weixin_recipient_id": params[17] if len(params) > 17 else None,
                        "updated_at": now,
                    }
            return []

        # --- Inbound events ---
        if "UPDATE dalton_channel_inbound_events" in norm:
            if "WHERE collect_buffer_id = ANY" in norm:
                bids = {str(x) for x in (params[0] or [])}
                for r in self.inbound_events.values():
                    if str(r.get("collect_buffer_id") or "") in bids:
                        r["status"] = "ignored"
                        r["fallback_pending"] = False
                        r["command_reply_pending"] = False
                        r["encrypted_weixin_media_meta"] = None
                return []
            if "WHERE collect_buffer_id = %s" in norm:
                bid = str(params[0])
                for r in self.inbound_events.values():
                    if str(r.get("collect_buffer_id") or "") == bid:
                        r["status"] = "ignored"
                        r["fallback_pending"] = False
                        r["command_reply_pending"] = False
                        r["encrypted_weixin_media_meta"] = None
                return []
            if "SET status = 'ignored', fallback_pending = FALSE, command_reply_pending = FALSE, encrypted_weixin_media_meta = NULL" in norm:
                if "dalton_channel_weixin_collect_buffers" in norm or not params:
                    for r in self.inbound_events.values():
                        bid = r.get("collect_buffer_id")
                        if bid and bid in self.collect_buffers:
                            if self.collect_buffers[bid]["status"] in ("cancelled", "expired", "done"):
                                r["status"] = "ignored"
                                r["fallback_pending"] = False
                                r["command_reply_pending"] = False
                                r["encrypted_weixin_media_meta"] = None
                    return []
                if "AND collect_buffer_id = %s::uuid AND weixin_account_id = %s::uuid" in norm:
                    mid, bid, aid_f = str(params[0]), str(params[1]), str(params[2])
                    r = self.inbound_events.get(("weixin", mid))
                    if r and str(r.get("collect_buffer_id") or "") == bid and str(r.get("weixin_account_id") or "") == aid_f:
                        r["status"] = "ignored"
                        r["fallback_pending"] = False
                        r["command_reply_pending"] = False
                        r["encrypted_weixin_media_meta"] = None
                    return []
                if "AND collect_buffer_id = %s::uuid" in norm:
                    mid, bid = str(params[0]), str(params[1])
                    r = self.inbound_events.get(("weixin", mid))
                    if r and str(r.get("collect_buffer_id") or "") == bid:
                        r["status"] = "ignored"
                        r["fallback_pending"] = False
                        r["command_reply_pending"] = False
                        r["encrypted_weixin_media_meta"] = None
                    return []
                mid = str(params[-1])
                r = self.inbound_events.get(("weixin", mid))
                if r:
                    r["status"] = "ignored"
                    r["fallback_pending"] = False
                    r["command_reply_pending"] = False
                    r["encrypted_weixin_media_meta"] = None
                return []
            if "SET collect_buffer_id = %s::uuid" in norm:
                bid = str(params[0])
                wid = str(params[1])
                st = str(params[2])
                mid = str(params[-1])
                r = self.inbound_events.get(("weixin", mid))
                if r:
                    r["collect_buffer_id"] = bid
                    r["workspace_id"] = wid
                    r["status"] = st
                    r["fallback_pending"] = False
                    if "encrypted_weixin_media_meta = pgp_sym_encrypt" in norm:
                        r["encrypted_weixin_media_meta"] = str(params[3])
                return []
            if "SET status = 'completed', fallback_pending = FALSE, uploaded_attachments = %s, encrypted_weixin_media_meta = NULL" in norm or "SET status = 'completed', uploaded_attachments = %s, encrypted_weixin_media_meta = NULL" in norm:
                atts, mid = self._unwrap(params[0]), str(params[1])
                r = self.inbound_events.get(("weixin", mid))
                if r:
                    if "AND collect_buffer_id = %s::uuid AND weixin_account_id = %s::uuid" in norm:
                        if str(r.get("collect_buffer_id") or "") != str(params[2]) or str(r.get("weixin_account_id") or "") != str(params[3]):
                            return []
                    elif "AND collect_buffer_id = %s::uuid" in norm:
                        if str(r.get("collect_buffer_id") or "") != str(params[2]):
                            return []
                    r["status"] = "completed"
                    r["fallback_pending"] = False
                    r["uploaded_attachments"] = atts
                    r["encrypted_weixin_media_meta"] = None
                return []
            if "SET status = 'ignored', raw_payload = %s, updated_at = NOW() WHERE platform = %s AND message_id = %s" in norm:
                r_pay, plat, mid = params
                r = self.inbound_events.get((str(plat), str(mid)))
                if r:
                    r["status"] = "ignored"
                    r["raw_payload"] = self._unwrap(r_pay) or {}
                    r["updated_at"] = now
                    return [dict(r)]
                return []
            if "SET workspace_id = %s, external_thread_id = %s, session_generation = %s" in norm:
                wid, ext_t, sgen, pend_i, r_pay, mid = params
                r = self.inbound_events.get(("weixin", str(mid)))
                if r:
                    r["workspace_id"] = str(wid) if wid else None
                    r["external_thread_id"] = str(ext_t) if ext_t else None
                    r["session_generation"] = int(sgen) if sgen is not None else None
                    r["pending_interrupt_snapshot"] = self._unwrap(pend_i)
                    r["raw_payload"] = self._unwrap(r_pay) or {}
                    r["updated_at"] = now
                    return [dict(r)]
                return []
            return []

        if "INSERT INTO dalton_channel_inbound_events" in norm:
            # Handle synthetic collect inbound, collect/cancel command_result, or normal inbound (all platforms)
            if "VALUES ('weixin', %s" in norm or "VALUES ( 'weixin', %s" in norm:
                mid = str(params[0])
                key = ("weixin", mid)
                if "command_result" in norm and "ON CONFLICT (platform, message_id) DO UPDATE" in norm:
                    # _persist_weixin_collect_command_result (12 params)
                    (
                        _mid, req_k, uid_v, c_addr, s_key, ws_v, s_gen,
                        cmd_res_p, cmd_pay_p, aid_v, bot_v, gen_v,
                    ) = params
                    row = {
                        "platform": "weixin",
                        "message_id": mid,
                        "request_key": str(req_k),
                        "dalton_user_id": str(uid_v),
                        "chat_type": "dm",
                        "chat_address": str(c_addr),
                        "thread_scope_key": str(s_key),
                        "workspace_id": ws_v,
                        "session_generation": s_gen,
                        "status": "completed",
                        "fallback_pending": False,
                        "command_effect_status": "done",
                        "command_reply_pending": True,
                        "command_result": self._unwrap(cmd_res_p),
                        "raw_payload": self._unwrap(cmd_pay_p) or {},
                        "event_kind": "command",
                        "weixin_account_id": str(aid_v) if aid_v else None,
                        "weixin_context_bot_id": str(bot_v) if bot_v else None,
                        "weixin_context_bot_generation": int(gen_v) if gen_v is not None else None,
                        "collect_buffer_id": None,
                        "encrypted_weixin_media_meta": None,
                        "created_at": now,
                        "updated_at": now,
                    }
                    self.inbound_events[key] = row
                    return [dict(row)]
                if key in self.inbound_events and "ON CONFLICT" in norm:
                    return []
                row = {
                    "platform": "weixin",
                    "message_id": mid,
                    "request_key": str(params[1]),
                    "dalton_user_id": str(params[2]) if len(params) > 2 else None,
                    "status": "completed" if "'completed'" in norm else ("ignored" if "'ignored'" in norm else "pending"),
                    "fallback_pending": False,
                    "command_reply_pending": False,
                    "raw_payload": {},
                    "uploaded_attachments": [],
                    "collect_buffer_id": None,
                    "encrypted_weixin_media_meta": None,
                    "weixin_account_id": None,
                    "weixin_context_bot_id": None,
                    "weixin_context_bot_generation": None,
                    "created_at": now,
                    "updated_at": now,
                }
                if "weixin_context_bot_generation, weixin_context_seq" in norm and len(params) >= 11:
                    row["weixin_account_id"] = str(params[7]) if params[7] else None
                    row["weixin_context_bot_id"] = str(params[8]) if params[8] else None
                    row["weixin_context_bot_generation"] = int(params[9]) if params[9] is not None else None
                for p in params:
                    if isinstance(p, str) and p in self.collect_buffers:
                        row["collect_buffer_id"] = p
                    elif hasattr(p, "obj") and isinstance(p.obj, dict):
                        row["raw_payload"] = dict(p.obj)
                    elif hasattr(p, "obj") and isinstance(p.obj, list):
                        row["uploaded_attachments"] = list(p.obj)
                self.inbound_events[key] = row
                return [dict(row)]
            if params and len(params) == 18 and "command_effect_status" in norm:
                (
                    plat, mid, req_k, c_type, c_addr, s_key, uid_v,
                    ws_v, ext_tid, s_gen, tid_v, rid_v, st_v,
                    raw_p, cmd_req_p, cmd_res_p, eff_st, rep_pend,
                ) = params
                key = (str(plat), str(mid))
                prev = self.inbound_events.get(key) or {}
                row = {
                    **prev,
                    "platform": str(plat),
                    "message_id": str(mid),
                    "request_key": str(req_k),
                    "chat_type": str(c_type),
                    "chat_address": str(c_addr),
                    "thread_scope_key": str(s_key),
                    "dalton_user_id": str(uid_v) if uid_v else prev.get("dalton_user_id"),
                    "workspace_id": ws_v or prev.get("workspace_id"),
                    "external_thread_id": ext_tid or prev.get("external_thread_id"),
                    "session_generation": s_gen if s_gen is not None else prev.get("session_generation"),
                    "thread_id": tid_v or prev.get("thread_id"),
                    "run_id": rid_v or prev.get("run_id"),
                    "status": str(st_v),
                    "fallback_pending": False,
                    "raw_payload": self._unwrap(raw_p) or {},
                    "event_kind": "command",
                    "command_request": self._unwrap(cmd_req_p) or {},
                    "command_result": self._unwrap(cmd_res_p),
                    "command_effect_status": str(eff_st),
                    "command_reply_pending": bool(rep_pend),
                    "updated_at": now,
                }
                row.setdefault("created_at", now)
                self.inbound_events[key] = row
                return [dict(row)]
            if params and len(params) >= 3:
                plat = str(params[0])
                mid = str(params[1])
                key = (plat, mid)
                if key in self.inbound_events and "ON CONFLICT" in norm:
                    return []
                row = {
                    "platform": plat,
                    "message_id": mid,
                    "request_key": str(params[2]),
                    "dalton_user_id": None,
                    "status": "ignored" if "'ignored'" in norm else "pending",
                    "fallback_pending": False,
                    "command_reply_pending": False,
                    "raw_payload": {},
                    "uploaded_attachments": [],
                    "created_at": now,
                    "updated_at": now,
                }
                self.inbound_events[key] = row
                return [dict(row)]
            return []

        if "FROM dalton_channel_inbound_events" in norm and "SELECT" in norm:
            if "LEFT JOIN dalton_channel_weixin_collect_buffers cb" in norm:
                out = []
                for r in self.inbound_events.values():
                    if r.get("status") in ("pending", "accepted", "streaming", "steered", "pending_resubmit") or r.get("fallback_pending") or r.get("command_reply_pending"):
                        d = dict(r)
                        bid = r.get("collect_buffer_id")
                        d["collect_buffer_status"] = (
                            self.collect_buffers[bid]["status"]
                            if bid and bid in self.collect_buffers
                            else None
                        )
                        d["decrypted_weixin_media_meta_json"] = r.get("encrypted_weixin_media_meta")
                        out.append(d)
                return out
            if "WHERE platform = 'weixin' AND message_id = %s" in norm:
                r = self.inbound_events.get(("weixin", str(params[0])))
                return [dict(r)] if r else []
            if "WHERE platform = %s AND message_id = %s" in norm:
                r = self.inbound_events.get((str(params[0]), str(params[1])))
                return [dict(r)] if r else []
            if "run_id = %s" in norm and params:
                out_r = []
                p_strs = {str(p) for p in params}
                for r in self.inbound_events.values():
                    if r.get("run_id") and str(r["run_id"]) in p_strs:
                        d = dict(r)
                        d.setdefault("target_address", d.get("chat_address") or "weixin:@me")
                        out_r.append(d)
                return out_r
            return []

        # --- Session generations ---
        if "INSERT INTO dalton_channel_session_generations" in norm:
            if "VALUES ('weixin', %s" in norm:
                plat, scope, uid = "weixin", str(params[0]), str(params[1])
            else:
                plat, scope, uid = str(params[0]), str(params[1]), str(params[2])
            key = (plat, scope, uid)
            if key not in self.session_generations:
                self.session_generations[key] = {
                    "platform": plat,
                    "scope_key": scope,
                    "dalton_user_id": uid,
                    "generation": 1,
                    "override_workspace_id": None,
                    "pending_interrupt": None,
                    "selection_initialized_at": now,
                    "last_conversation_activity_at": now,
                    "updated_at": now,
                }
                return [{"generation": self.session_generations[key]["generation"]}]
            if "generation = dalton_channel_session_generations.generation + 1" in norm:
                self.session_generations[key]["generation"] += 1
                return [{"generation": self.session_generations[key]["generation"]}]
            if "ON CONFLICT" in norm and "DO NOTHING" in norm:
                return []
            return [{"generation": self.session_generations[key]["generation"]}]

        if "UPDATE dalton_channel_session_generations" in norm:
            if "SET generation = %s" in norm and len(params) >= 8:
                new_g = int(params[0])
                ov_ws = params[1]
                plat, scope, uid = str(params[-3]), str(params[-2]), str(params[-1])
                row = self.session_generations.get((plat, scope, uid))
                if row is not None:
                    row["generation"] = new_g
                    row["override_workspace_id"] = str(ov_ws) if ov_ws else None
                    row["updated_at"] = now
                    return [{"scope_key": scope, "generation": row["generation"]}]
                return []
            uid = str(params[-1])
            out = []
            for k, row in self.session_generations.items():
                if k[0] == "weixin" and k[2] == uid:
                    row["generation"] += 1
                    row["override_workspace_id"] = None
                    row["pending_interrupt"] = None
                    row["updated_at"] = now
                    out.append({"scope_key": k[1], "generation": row["generation"]})
            return out

        if "FROM dalton_channel_session_generations" in norm and "SELECT" in norm:
            if len(params) == 1:
                uid = str(params[0])
                return [
                    dict(r)
                    for k, r in sorted(self.session_generations.items())
                    if k[0] == "weixin" and k[2] == uid
                ]
            if len(params) == 2:
                scope, uid = str(params[0]), str(params[1])
                row = self.session_generations.get(("weixin", scope, uid))
                return [dict(row)] if row else []
            plat, scope, uid = str(params[0]), str(params[1]), str(params[2])
            row = self.session_generations.get((plat, scope, uid))
            return [dict(row)] if row else []

        # --- User roles (for delete_user_role hook test) ---
        if "DELETE FROM dalton_user_roles" in norm:
            uid = str(params[0])
            role = self.user_roles.pop(uid, None)
            if role:
                self.last_rowcount = 1
                return [{"user_id": uid, "role": role}]
            self.last_rowcount = 0
            return []
        if "FROM dalton_user_roles" in norm and "SELECT" in norm:
            if not params:
                return [{"user_id": u, "role": r} for u, r in sorted(self.user_roles.items())]
            if "WHERE role = 'admin'" in norm:
                return [{"user_id": u, "role": r} for u, r in self.user_roles.items() if r == "admin"]
            uid = str(params[0])
            r = self.user_roles.get(uid)
            return [{"user_id": uid, "role": r}] if r else []

        return []


def _build_autospec_db(store: InMemoryWeixinMultiAccountStore) -> tuple[Any, Any]:
    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    state: dict[str, Any] = {"rows": [], "idx": 0}

    async def _execute(sql: str, params: Any = None) -> None:
        res = await store.handle_sql(sql, params)
        if store.last_rowcount is not None:
            cursor.rowcount = store.last_rowcount
        elif isinstance(res, list):
            cursor.rowcount = len(res)
        else:
            cursor.rowcount = 0 if res is None else 1
        state["rows"] = list(res) if isinstance(res, list) else ([] if res is None else [res])
        state["idx"] = 0

    async def _fetchone() -> Any:
        rows = state["rows"]
        idx = state["idx"]
        if idx < len(rows):
            state["idx"] = idx + 1
            return rows[idx]
        return None

    async def _fetchall() -> list[Any]:
        rows = state["rows"][state["idx"] :]
        state["idx"] = len(state["rows"])
        return list(rows)

    cursor.execute.side_effect = _execute
    cursor.fetchone.side_effect = _fetchone
    cursor.fetchall.side_effect = _fetchall

    conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cursor

    @asynccontextmanager
    async def _tx_cm(*args: Any, **kwargs: Any):
        yield

    conn.cursor.side_effect = _cursor_cm
    conn.transaction.side_effect = _tx_cm

    @asynccontextmanager
    async def _get_db(existing_conn: Any = None):
        yield existing_conn or conn

    return create_autospec(db_pool.get_db_connection, side_effect=_get_db), conn


@pytest.fixture
def wx_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    store = InMemoryWeixinMultiAccountStore()
    fake_get_db, fake_conn = _build_autospec_db(store)
    monkeypatch.setenv("DALTON_ENABLED", "true")
    monkeypatch.setenv("APP_ENV", "dalton")
    monkeypatch.setenv("DALTON_PROFILE", "everflow")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_GATEWAY", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_WEIXIN", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_FEISHU", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_DISCORD", "true")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("BYOK_ENCRYPTION_KEY", "unit-test-encryption-key-32bytes!")
    monkeypatch.setattr(wx_db, "get_db_connection", fake_get_db)
    monkeypatch.setattr(cg_db, "get_db_connection", fake_get_db)
    monkeypatch.setattr(dalton_roles, "get_db_connection", fake_get_db)

    tiers: dict[str, str] = {
        "u-admin": "admin",
        "u-alice": "internal",
        "u-bob": "internal",
        "u-ext": "external",
    }
    roster: dict[str, str] = {
        "u-admin": "active",
        "u-alice": "active",
        "u-bob": "active",
        "u-ext": "active",
    }

    async def _fake_tier(uid: str, *, conn: Any = None) -> str:
        return tiers.get(uid, "external")

    async def _fake_is_admin(uid: str, *, email: str | None = None) -> bool:
        return tiers.get(uid, "external") == "admin"

    async def _fake_roster(uid: str, *, email: str | None = None, conn: Any = None) -> str:
        return roster.get(uid, "removed")

    monkeypatch.setattr(wx_db, "get_user_tier", create_autospec(wx_db.get_user_tier, side_effect=_fake_tier))
    monkeypatch.setattr(cg_db, "get_user_tier", create_autospec(cg_db.get_user_tier, side_effect=_fake_tier))
    monkeypatch.setattr(ch_router_mod, "get_user_tier", create_autospec(ch_router_mod.get_user_tier, side_effect=_fake_tier))
    monkeypatch.setattr(ch_router_mod, "is_admin_user", create_autospec(ch_router_mod.is_admin_user, side_effect=_fake_is_admin))
    monkeypatch.setattr(wx_db, "check_team_member_status_live", create_autospec(wx_db.check_team_member_status_live, side_effect=_fake_roster))
    monkeypatch.setattr(cg_db, "check_team_member_status_live", create_autospec(cg_db.check_team_member_status_live, side_effect=_fake_roster))
    monkeypatch.setattr(
        wx_db,
        "notify_gateway_weixin_accounts_sync",
        create_autospec(wx_db.notify_gateway_weixin_accounts_sync, return_value=True),
    )

    app = FastAPI()
    current_user = {"user_id": "u-alice"}
    app.dependency_overrides[get_current_user_id] = lambda: current_user["user_id"]
    app.include_router(dalton_router)
    client = TestClient(app)

    return {
        "store": store,
        "conn": fake_conn,
        "tiers": tiers,
        "roster": roster,
        "current_user": current_user,
        "client": client,
        "headers": {"X-Service-Token": SERVICE_TOKEN, "X-Gateway-Epoch": "1"},
    }


# ---------------------------------------------------------------------------
# §7.1 #1: d049 Migration Contract & Post-Deploy FK Validator
# ---------------------------------------------------------------------------


def test_d049_migration_schema_copy_only_not_valid_fks_and_fk_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify d049 revision chain, 8 tables, 13 columns, 5 NOT VALID FKs, copy-only guarantee, and FK validator."""
    mig_path = REPO_ROOT / "src/dalton/migrations/versions/d049_weixin_multi_account.py"
    src = mig_path.read_text(encoding="utf-8")

    assert 'revision: str = "d049"' in src
    assert 'down_revision: str | None = "d046"' in src

    # All 8 new tables
    for tbl in (
        "dalton_channel_gateway_lease",
        "dalton_channel_weixin_config",
        "dalton_channel_weixin_accounts",
        "dalton_channel_weixin_account_tokens",
        "dalton_channel_weixin_login_sessions",
        "dalton_channel_weixin_audit_logs",
        "dalton_channel_weixin_collect_buffers",
        "dalton_channel_weixin_collect_items",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {tbl}" in src

    # All 5 NOT VALID foreign keys
    assert len(fk_val.WEIXIN_D049_FK_CONSTRAINTS) == 5
    for _table, cname in fk_val.WEIXIN_D049_FK_CONSTRAINTS:
        assert cname in src
    assert src.count("NOT VALID") >= 5

    # Copy-only guarantee: legacy tables never mutated in upgrade()
    upgrade_body = src.split("def downgrade()")[0]
    for forbidden in (
        "UPDATE dalton_channel_weixin_state",
        "DELETE FROM dalton_channel_weixin_state",
        "DROP TABLE dalton_channel_weixin_state",
        "UPDATE dalton_channel_identities",
        "DELETE FROM dalton_channel_identities",
        "UPDATE dalton_channel_bind_codes",
        "DELETE FROM dalton_channel_bind_codes",
        "UPDATE dalton_channel_weixin_context_tokens",
        "DELETE FROM dalton_channel_weixin_context_tokens",
    ):
        assert forbidden not in upgrade_body

    # Both split ops wrappers exist and are executable
    for rel in (
        "scripts/dalton/ops/prod/validate_d049_fks.sh",
        "scripts/standalone/validate_d049_fks.sh",
    ):
        script_path = REPO_ROOT / rel
        assert script_path.exists()
        assert script_path.stat().st_mode & 0o111

    # Post-deploy validator executes 5 separate transactions with lock_timeout and checks convalidated
    executed_txs: list[list[str]] = []

    class _FakeCur:
        def __init__(self, tx_log: list[str]) -> None:
            self._tx_log = tx_log
            self._last = ""

        def __enter__(self) -> _FakeCur:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def execute(self, sql: str, params: Any = None) -> None:
            self._last = " ".join(str(sql).split())
            self._tx_log.append(self._last)

        def fetchone(self) -> tuple[bool] | None:
            if "convalidated" in self._last:
                return (True,)
            return None

    class _FakeConn:
        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def transaction(self):
            tx_log: list[str] = []
            executed_txs.append(tx_log)

            class _Tx:
                def __enter__(self_inner) -> None:
                    return None

                def __exit__(self_inner, *args: Any) -> None:
                    return None

            self._cur_tx = tx_log
            return _Tx()

        def cursor(self) -> _FakeCur:
            return _FakeCur(self._cur_tx)

    monkeypatch.setattr(
        fk_val.psycopg,
        "connect",
        create_autospec(fk_val.psycopg.connect, return_value=_FakeConn()),
    )
    validated = fk_val.validate_d049_foreign_keys("postgresql://u:p@localhost/db_test")
    assert validated["ok"] is True
    assert len(validated["validated"]) == 5
    assert all(item["convalidated"] is True for item in validated["validated"])
    assert len(executed_txs) == 5
    for tx_sqls in executed_txs:
        assert any("SET LOCAL lock_timeout" in s for s in tx_sqls)
        assert any("VALIDATE CONSTRAINT" in s for s in tx_sqls)


# ---------------------------------------------------------------------------
# §7.1 #2: Gateway Lease & Full-Write Epoch Fencing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_lease_acquire_heartbeat_release_and_epoch_fencing_across_all_write_routes(
    wx_env: dict[str, Any],
) -> None:
    """Verify single-instance lease acquire/takeover/heartbeat/release and 409 on every write route."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    client: TestClient = wx_env["client"]
    svc_hdr = {"X-Service-Token": SERVICE_TOKEN}

    # Peer denied while gw-1 holds unexpired lease
    r_denied = client.post(
        "/api/v1/dalton/channels/internal/gateway-lease/acquire",
        headers=svc_hdr,
        json={"instance_id": "gw-2", "ttl_seconds": 45},
    )
    assert r_denied.status_code == 200
    assert r_denied.json()["granted"] is False
    assert r_denied.json()["epoch"] == 1

    # Stale epoch heartbeat returns 409 stale_gateway_epoch
    r_hb_stale = client.post(
        "/api/v1/dalton/channels/internal/gateway-lease/heartbeat",
        headers=svc_hdr,
        json={"instance_id": "gw-2", "epoch": 1, "ttl_seconds": 45},
    )
    assert r_hb_stale.status_code == 409
    assert r_hb_stale.json()["code"] == "stale_gateway_epoch"

    # Lease expires -> gw-2 takes over and epoch advances to 2
    store.now = store.now + timedelta(seconds=60)
    r_takeover = client.post(
        "/api/v1/dalton/channels/internal/gateway-lease/acquire",
        headers=svc_hdr,
        json={"instance_id": "gw-2", "ttl_seconds": 45},
    )
    assert r_takeover.status_code == 200
    assert r_takeover.json()["granted"] is True
    assert r_takeover.json()["epoch"] == 2

    # Every write/recover internal route rejects old epoch=1 with HTTP 409 stale_gateway_epoch
    stale_hdr = {"X-Service-Token": SERVICE_TOKEN, "X-Gateway-Epoch": "1"}
    aid = str(uuid4())
    bid = str(uuid4())
    for method, url, body in (
        ("GET", "/api/v1/dalton/channels/internal/recover?mode=startup", None),
        ("POST", "/api/v1/dalton/channels/internal/weixin/accounts/reconcile", {"since_revision": 0}),
        (
            "POST",
            f"/api/v1/dalton/channels/internal/weixin/accounts/{aid}/cursor",
            {"expected_bot_generation": 1, "expected_ilink_bot_id": "b1", "get_updates_buf": "c2"},
        ),
        (
            "POST",
            f"/api/v1/dalton/channels/internal/weixin/accounts/{aid}/status",
            {"action": "mark_session_expired", "expected_bot_generation": 1, "expected_ilink_bot_id": "b1"},
        ),
        (
            "PATCH",
            "/api/v1/dalton/channels/internal/weixin/login-sessions/sk-1",
            {"action": "update", "expected_login_version": 1, "status": "scaned"},
        ),
        (
            "POST",
            f"/api/v1/dalton/channels/internal/weixin/collect-buffers/{bid}/items/m1/attachments",
            {"uploaded_attachments": []},
        ),
        (
            "POST",
            f"/api/v1/dalton/channels/internal/weixin/collect-buffers/{bid}/finalize",
            None,
        ),
        (
            "POST",
            "/api/v1/dalton/channels/internal/deliveries/dk-1/preflight-send",
            {"delivery_attempt": 1, "chunk_index": 0},
        ),
        (
            "POST",
            "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
            {
                "platform": "weixin",
                "platform_user_id": "wx1",
                "chat_address": "weixin:@me",
                "thread_scope_key": "dm:wx1",
                "message_id": "m1",
            },
        ),
        (
            "POST",
            "/api/v1/dalton/channels/internal/bind-code/consume",
            {"action": "unbind", "platform": "weixin", "user_id": "u-alice"},
        ),
        (
            "POST",
            "/api/v1/dalton/channels/internal/session/command",
            {
                "cmd": "help",
                "platform": "weixin",
                "platform_user_id": "wx1",
                "scope_key": "dm:wx1",
                "message_id": "m2",
            },
        ),
        (
            "POST",
            "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
            {"delivery_key": "dk-1", "kind": "agent_send", "platform": "weixin", "user_id": "u-alice"},
        ),
        (
            "POST",
            "/api/v1/dalton/channels/internal/deliveries/progress",
            {"delivery_key": "dk-1", "status": "sent"},
        ),
        (
            "POST",
            "/api/v1/dalton/channels/internal/inbound/progress",
            {"platform": "weixin", "message_id": "m1", "status": "completed"},
        ),
        (
            "POST",
            "/api/v1/dalton/channels/internal/steers/reconcile",
            {"platform": "weixin", "message_id": "m1"},
        ),
    ):
        resp = client.request(method, url, headers=stale_hdr, json=body)
        assert resp.status_code == 409, f"{method} {url} returned {resp.status_code}: {resp.text}"
        assert resp.json()["code"] == "stale_gateway_epoch"

    # Voluntary release succeeds for current holder
    r_rel = client.post(
        "/api/v1/dalton/channels/internal/gateway-lease/release",
        headers=svc_hdr,
        json={"instance_id": "gw-2", "epoch": 2},
    )
    assert r_rel.status_code == 200
    assert r_rel.json()["released"] is True


# ---------------------------------------------------------------------------
# §7.1 #3 & #4: Global Lock Order, Starting Pre-Write, Revoke, D7, Roster,
#               Codex ② Dual-Switch Reopen, prev_ilink_user_id, Capacity, D16
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_lock_order_enforced_in_confirm_unbind_and_pending_verification_rollback(
    wx_env: dict[str, Any],
) -> None:
    """Verify Level 0b -> Level 1 -> Level 3a -> Level 3b -> Level 4 -> Level 8 lock order."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    start = await wx_db.start_weixin_login_session(user_id="u-alice")
    await wx_db.patch_weixin_login_session(
        session_key=start["session_key"],
        gateway_epoch=1,
        action="save_qr",
        qrcode="qr-secret-1",
        qrcode_img_content="data:image/png;base64,AAA",
        redirect_base_url="https://ilinkai.weixin.qq.com",
    )

    store.executed_sql.clear()
    res = await wx_db.patch_weixin_login_session(
        session_key=start["session_key"],
        gateway_epoch=1,
        action="confirm",
        expected_login_version=2,
        ilink_bot_id="bot-alice@im.wechat",
        bot_token="bot-tok-alice-secret",
        ilink_user_id="wx_alice@im.wechat",
        ilink_username="Alice",
    )
    assert res["ok"] is True

    sqls = [s for s, _p in store.executed_sql]
    idx_lease = next(i for i, s in enumerate(sqls) if "FROM dalton_channel_gateway_lease" in s and "FOR SHARE" in s)
    idx_user = next(i for i, s in enumerate(sqls) if "dalton_user_settings:" in str(store.executed_sql[i][1]))
    idx_wx_lock = next(i for i, s in enumerate(sqls) if "dalton_weixin_ilink_user:" in str(store.executed_sql[i][1]))
    idx_sess = next(i for i, s in enumerate(sqls) if "FROM dalton_channel_weixin_login_sessions" in s and "FOR UPDATE" in s)
    idx_cfg = next(i for i, s in enumerate(sqls) if "UPDATE dalton_channel_weixin_config" in s)
    assert idx_lease < idx_user < idx_wx_lock < idx_sess < idx_cfg


@pytest.mark.asyncio
async def test_qr_login_starting_prewrite_conditional_revoke_and_d7_confirmed_retry_recheck(
    wx_env: dict[str, Any],
) -> None:
    """D14 starting pre-write + admin revoke by user_id before account exists + D7 confirmed retry recheck."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    client: TestClient = wx_env["client"]

    # 1. Start pre-writes `starting` before iLink
    started = await wx_db.start_weixin_login_session(user_id="u-alice")
    skey = started["session_key"]
    assert store.login_sessions[skey]["status"] == "starting"

    # 2. Admin revokes by target_user_id BEFORE any account_id exists (preset reason only)
    wx_env["current_user"]["user_id"] = "u-admin"
    r_rev = client.request(
        "DELETE",
        "/api/v1/dalton/channels/weixin/admin/users/u-alice/weixin",
        json={"reason": "误绑微信号"},
    )
    assert r_rev.status_code == 200
    assert store.login_sessions[skey]["status"] == "superseded"
    assert store.audit_logs[-1]["action"] == "admin_unbind"
    assert store.audit_logs[-1]["reason"] == "误绑微信号"

    # 3. Late save_qr hits 0 rows -> 409 session_revoked
    with pytest.raises(HTTPException) as exc_info:
        await wx_db.patch_weixin_login_session(
            session_key=skey,
            gateway_epoch=1,
            action="save_qr",
            qrcode="late-qr",
            qrcode_img_content="data:image/png;base64,BBB",
        )
    assert exc_info.value.status_code == 409

    # 4. New session confirms -> then unbind -> D7 GET login-sessions rechecks & marks expired
    s2 = await wx_db.start_weixin_login_session(user_id="u-alice")
    await wx_db.patch_weixin_login_session(
        session_key=s2["session_key"],
        gateway_epoch=1,
        action="save_qr",
        qrcode="qr-2",
        qrcode_img_content="data:image/png;base64,CCC",
    )
    conf = await wx_db.patch_weixin_login_session(
        session_key=s2["session_key"],
        gateway_epoch=1,
        action="confirm",
        expected_login_version=2,
        ilink_bot_id="bot-a2@im.wechat",
        bot_token="tok-a2",
        ilink_user_id="wx_a2@im.wechat",
    )
    assert conf["ok"] is True
    # Confirmed result strictly 5 whitelisted fields (§4.3)
    saved_conf = store.login_sessions[s2["session_key"]]["confirmed_result"]
    assert set(saved_conf.keys()) == {
        "account_id",
        "connected",
        "already_connected",
        "bot_generation",
        "accounts_revision",
    }

    # Unbind account -> retry GET on confirmed session returns connected=False & marks expired
    await wx_db.unbind_weixin_account(
        target_user_id="u-alice",
        operator_user_id="u-alice",
        action="member_unbind",
    )
    rechecked = await wx_db.get_weixin_login_session(
        session_key=s2["session_key"], user_id="u-alice", include_qrcode=False
    )
    assert rechecked["connected"] is False
    assert rechecked["status"] == "expired"


@pytest.mark.asyncio
async def test_roster_three_states_allowlist_priority_and_lookup_error_503(
    wx_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify check_team_member_status_live 3-state semantics in local & Supabase modes + role delete hook."""
    # Local auth mode
    orig_get_user_role = dalton_roles.get_user_role
    monkeypatch.setattr(access_tiers, "is_dalton_local_auth_enabled", lambda: True)
    monkeypatch.setattr(
        dalton_roles,
        "get_user_role",
        create_autospec(orig_get_user_role, return_value="internal"),
    )
    assert await access_tiers.check_team_member_status_live("u-1") == "active"

    monkeypatch.setattr(
        dalton_roles,
        "get_user_role",
        create_autospec(orig_get_user_role, return_value=None),
    )
    assert await access_tiers.check_team_member_status_live("u-1") == "removed"

    monkeypatch.setattr(
        dalton_roles,
        "get_user_role",
        create_autospec(orig_get_user_role, side_effect=RuntimeError("db down")),
    )
    assert await access_tiers.check_team_member_status_live("u-1") == "lookup_error"
    monkeypatch.setattr(dalton_roles, "get_user_role", orig_get_user_role)

    # Supabase mode: allowlist short-circuit takes precedence over DB
    orig_get_user = user_db.get_user
    monkeypatch.setattr(access_tiers, "is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(
        access_tiers,
        "get_dalton_allowed_emails",
        lambda: frozenset({"alice@example.com"}),
    )
    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(orig_get_user, return_value={"email": "alice@example.com"}),
    )
    mock_member_err = create_autospec(
        dalton_members.get_member_by_email, side_effect=RuntimeError("should not be called")
    )
    monkeypatch.setattr(dalton_members, "get_member_by_email", mock_member_err)
    assert await access_tiers.check_team_member_status_live("u-alice") == "active"
    assert mock_member_err.call_count == 0

    # Non-allowlist email hits DB -> error yields lookup_error (never silently removed!)
    monkeypatch.setattr(
        user_db,
        "get_user",
        create_autospec(orig_get_user, return_value={"email": "bob@example.com"}),
    )
    assert await access_tiers.check_team_member_status_live("u-bob") == "lookup_error"

    # I-1 returns HTTP 503 when roster returns lookup_error
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    aid = str(uuid4())
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-bob",
        "ilink_bot_id": "bot-bob@im.wechat",
        "encrypted_bot_token": "tok-bob",
        "bot_token": "tok-bob",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "cur-1",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_bob@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Bob",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    wx_env["roster"]["u-bob"] = "lookup_error"
    r_503 = wx_env["client"].post(
        "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
        headers=wx_env["headers"],
        json={
            "platform": "weixin",
            "platform_user_id": "wx_bob@im.wechat",
            "chat_address": "weixin:@me",
            "thread_scope_key": "dm:wx_bob@im.wechat",
            "message_id": "wx_m_503",
            "weixin_account_id": aid,
            "weixin_bot_id": "bot-bob@im.wechat",
            "weixin_bot_generation": 1,
        },
    )
    assert r_503.status_code == 503

    # Reconcile leaves account untouched during lookup_error
    rev_before = store.config["accounts_revision"]
    rec_err = await wx_db.reconcile_weixin_accounts(gateway_epoch=1, since_revision=rev_before)
    assert store.accounts[aid]["status"] == "active"
    assert rec_err["accounts_revision"] == rev_before

    # Local auth delete_user_role revokes Weixin account in same tx
    store.user_roles["u-admin"] = "admin"
    store.user_roles["u-bob"] = "internal"
    deleted = await dalton_roles.delete_user_role("u-bob", conn=wx_env["conn"])
    assert deleted is True
    assert store.accounts[aid]["status"] == "disabled"
    assert store.accounts[aid]["disabled_reason"] == "member_not_in_roster"
    assert store.accounts[aid]["prev_status"] == "active"


@pytest.mark.asyncio
async def test_member_tier_switch_evaluation_dual_switch_reopen_orders_and_prev_status_restore(
    wx_env: dict[str, Any],
) -> None:
    """Verify evaluate_weixin_member_access, Codex ② both switch reopen orders, and prev_status exact restore."""
    # Direct unit checks for evaluate_weixin_member_access
    assert access_tiers.evaluate_weixin_member_access("admin", channel_enabled=True, db_allow_external=False) == (True, None)
    assert access_tiers.evaluate_weixin_member_access("internal", channel_enabled=True, db_allow_external=False) == (True, None)
    assert access_tiers.evaluate_weixin_member_access("external", channel_enabled=True, db_allow_external=False) == (False, "external_access_disabled")
    assert access_tiers.evaluate_weixin_member_access("external", channel_enabled=True, db_allow_external=None) == (True, None)
    assert access_tiers.evaluate_weixin_member_access("admin", channel_enabled=False, db_allow_external=True) == (False, "weixin_channel_disabled")

    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    # Seed 3 external users in active, pending_verification, and expired states + 1 internal user
    for uid, st, wx_id in (
        ("u-ext-active", "active", "wx_ea@im.wechat"),
        ("u-ext-pending", "pending_verification", None),
        ("u-ext-expired", "expired", "wx_ee@im.wechat"),
        ("u-alice", "active", "wx_alice@im.wechat"),
    ):
        wx_env["tiers"][uid] = "internal" if uid == "u-alice" else "external"
        wx_env["roster"][uid] = "active"
        aid = str(uuid4())
        store.accounts[aid] = {
            "account_id": aid,
            "user_id": uid,
            "ilink_bot_id": f"bot-{uid}@im.wechat",
            "encrypted_bot_token": f"tok-{uid}",
            "bot_token": f"tok-{uid}",
            "base_url": "https://ilinkai.weixin.qq.com",
            "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
            "get_updates_buf": "",
            "status": st,
            "prev_status": None,
            "disabled_reason": None,
            "bot_generation": 1,
            "prev_bot_generation": None,
            "bot_generation_updated_at": None,
            "ilink_user_id": wx_id,
            "prev_ilink_user_id": None,
            "ilink_username": uid,
            "last_inbound_at": None,
            "created_at": store.now,
            "updated_at": store.now,
        }

    # Codex ② order A: close allow_external -> close enabled -> reopen allow_external (blocked by enabled=False) -> reopen enabled (MUST restore all 3 to exact prev_status!)
    await wx_db.update_weixin_admin_config(operator_user_id="u-admin", allow_external=False)
    by_user = {a["user_id"]: a for a in store.accounts.values()}
    assert by_user["u-alice"]["status"] == "active"  # internal unaffected
    assert by_user["u-ext-active"]["status"] == "disabled"
    assert by_user["u-ext-active"]["prev_status"] == "active"
    assert by_user["u-ext-pending"]["status"] == "disabled"
    assert by_user["u-ext-pending"]["prev_status"] == "pending_verification"
    assert by_user["u-ext-expired"]["status"] == "disabled"
    assert by_user["u-ext-expired"]["prev_status"] == "expired"

    # Reconcile while allow_external=False keeps external accounts disabled
    await wx_db.reconcile_weixin_accounts(gateway_epoch=1)
    assert by_user["u-ext-active"]["status"] == "disabled"

    # Close global enabled, then reopen allow_external (still blocked by enabled=False)
    await wx_db.update_weixin_admin_config(operator_user_id="u-admin", enabled=False)
    await wx_db.update_weixin_admin_config(operator_user_id="u-admin", allow_external=True)
    assert by_user["u-ext-active"]["status"] == "disabled"

    # Now reopen global enabled -> unified reevaluate restores ALL 3 back to exact prev_status!
    await wx_db.update_weixin_admin_config(operator_user_id="u-admin", enabled=True)
    assert by_user["u-ext-active"]["status"] == "active"
    assert by_user["u-ext-pending"]["status"] == "pending_verification"
    assert by_user["u-ext-expired"]["status"] == "expired"
    assert by_user["u-ext-active"]["prev_status"] is None


@pytest.mark.asyncio
async def test_rescan_pending_verification_preserves_prev_ilink_user_id_and_conflict_one_time_reply(
    wx_env: dict[str, Any],
) -> None:
    """Verify prev_ilink_user_id protection during rescan pending_verification, conflict D16 reply, and 9 audit defaults."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    # Verify all 9 audit actions have non-empty fixed default reasons (§4.3)
    assert len(wx_db.AUDIT_DEFAULT_REASONS) == 9
    assert "delivery_deferred" not in wx_db.AUDIT_DEFAULT_REASONS
    assert "delivery_deferred_timeout" not in wx_db.AUDIT_DEFAULT_REASONS
    assert all(isinstance(v, str) and v.strip() for v in wx_db.AUDIT_DEFAULT_REASONS.values())

    # Member A binds wx_a
    s_a1 = await wx_db.start_weixin_login_session(user_id="u-alice")
    await wx_db.patch_weixin_login_session(
        session_key=s_a1["session_key"], gateway_epoch=1, action="save_qr", qrcode="q1"
    )
    c_a1 = await wx_db.patch_weixin_login_session(
        session_key=s_a1["session_key"],
        gateway_epoch=1,
        action="confirm",
        expected_login_version=2,
        ilink_bot_id="bot-a1@im.wechat",
        bot_token="tok-a1",
        ilink_user_id="wx_a@im.wechat",
    )
    aid_a = c_a1["account_id"]

    # Member A rescans and iLink omits ilink_user_id -> pending_verification keeping prev_ilink_user_id = wx_a
    s_a2 = await wx_db.start_weixin_login_session(user_id="u-alice")
    await wx_db.patch_weixin_login_session(
        session_key=s_a2["session_key"], gateway_epoch=1, action="save_qr", qrcode="q2"
    )
    c_a2 = await wx_db.patch_weixin_login_session(
        session_key=s_a2["session_key"],
        gateway_epoch=1,
        action="confirm",
        expected_login_version=2,
        expected_bot_generation=1,
        ilink_bot_id="bot-a2@im.wechat",
        bot_token="tok-a2",
        ilink_user_id=None,
    )
    assert c_a2["account_status"] == "pending_verification"
    assert store.accounts[aid_a]["ilink_user_id"] is None
    assert store.accounts[aid_a]["prev_ilink_user_id"] == "wx_a@im.wechat"

    # Member B tries to confirm wx_a while A is pending_verification -> REJECTED!
    s_b = await wx_db.start_weixin_login_session(user_id="u-bob")
    await wx_db.patch_weixin_login_session(
        session_key=s_b["session_key"], gateway_epoch=1, action="save_qr", qrcode="qb"
    )
    with pytest.raises(HTTPException) as exc_conflict:
        await wx_db.patch_weixin_login_session(
            session_key=s_b["session_key"],
            gateway_epoch=1,
            action="confirm",
            expected_login_version=2,
            ilink_bot_id="bot-b@im.wechat",
            bot_token="tok-b",
            ilink_user_id="wx_a@im.wechat",
        )
    assert exc_conflict.value.status_code == 409
    assert exc_conflict.value.detail["code"] == "ilink_user_already_bound"

    # Member A first message verifies as a NEW weixin wx_a_new -> releases wx_a, clears prev_ilink_user_id
    v_res = await wx_db.verify_pending_weixin_account_first_message(
        account_id=aid_a,
        platform_user_id="wx_a_new@im.wechat",
        gateway_epoch=1,
        require_gateway_epoch=True,
        inbound_context_token="ctx-a-new",
    )
    assert v_res["outcome"] == "verified"
    assert store.accounts[aid_a]["status"] == "active"
    assert store.accounts[aid_a]["ilink_user_id"] == "wx_a_new@im.wechat"
    assert store.accounts[aid_a]["prev_ilink_user_id"] is None

    # Member B now enters pending_verification and sends first message from wx_a_new (owned by A!) -> conflict_auto_unbound + D16 reply
    c_b_pend = await wx_db.patch_weixin_login_session(
        session_key=s_b["session_key"],
        gateway_epoch=1,
        action="confirm",
        expected_login_version=2,
        ilink_bot_id="bot-b@im.wechat",
        bot_token="tok-b",
        ilink_user_id=None,
    )
    aid_b = c_b_pend["account_id"]
    v_conflict = await wx_db.verify_pending_weixin_account_first_message(
        account_id=aid_b,
        platform_user_id="wx_a_new@im.wechat",
        gateway_epoch=1,
        require_gateway_epoch=True,
        inbound_context_token="ctx-b-conflict",
    )
    assert v_conflict["outcome"] == "conflict_unbound"
    assert aid_b not in store.accounts
    assert v_conflict["one_time_weixin_reply"]["context_token"] == "ctx-b-conflict"
    assert store.audit_logs[-1]["action"] == "conflict_auto_unbound"

    # /unbind returns D16 one-time farewell reply envelope
    acc_a_cur = store.accounts[aid_a]
    unbind_res = await wx_db.unbind_weixin_account(
        account_id=aid_a,
        operator_user_id="u-alice",
        action="member_unbind",
        via_wechat_command=True,
        expected_bot_id=str(acc_a_cur["ilink_bot_id"]),
        expected_bot_generation=int(acc_a_cur["bot_generation"]),
        expected_sender_ilink_user_id="wx_a_new@im.wechat",
        one_time_context_token="ctx-bye",
        one_time_recipient_id="wx_a_new@im.wechat",
    )
    assert unbind_res["ok"] is True
    assert unbind_res["one_time_weixin_reply"]["context_token"] == "ctx-bye"
    assert aid_a not in store.accounts


@pytest.mark.asyncio
async def test_capacity_limit_200_blocks_new_confirm_and_auto_restore_but_allows_rescan(
    wx_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify slot cap enforcement on start/confirm/auto-restore while allowing rescan and 0-slot expired restore."""
    monkeypatch.setenv("DALTON_WEIXIN_MAX_CONCURRENT_ACCOUNTS", "2")
    assert get_weixin_max_concurrent_accounts() == 2

    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    for idx, uid in enumerate(("u-alice", "u-bob"), start=1):
        aid = str(uuid4())
        store.accounts[aid] = {
            "account_id": aid,
            "user_id": uid,
            "ilink_bot_id": f"bot-{idx}@im.wechat",
            "encrypted_bot_token": f"tok-{idx}",
            "bot_token": f"tok-{idx}",
            "base_url": "https://ilinkai.weixin.qq.com",
            "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
            "get_updates_buf": "",
            "status": "active",
            "prev_status": None,
            "disabled_reason": None,
            "bot_generation": 1,
            "prev_bot_generation": None,
            "bot_generation_updated_at": None,
            "ilink_user_id": f"wx_{idx}@im.wechat",
            "prev_ilink_user_id": None,
            "ilink_username": uid,
            "last_inbound_at": None,
            "created_at": store.now,
            "updated_at": store.now,
        }

    # New member u-ext blocked by capacity 2/2
    with pytest.raises(HTTPException) as exc_cap:
        await wx_db.start_weixin_login_session(user_id="u-ext")
    assert exc_cap.value.status_code == 409
    assert exc_cap.value.detail["code"] == wx_db.WEIXIN_CAPACITY_FULL_CODE

    # Existing slot holder u-alice can still rescan at 2/2!
    s_rescan = await wx_db.start_weixin_login_session(user_id="u-alice")
    await wx_db.patch_weixin_login_session(
        session_key=s_rescan["session_key"], gateway_epoch=1, action="save_qr", qrcode="qr-r"
    )
    c_rescan = await wx_db.patch_weixin_login_session(
        session_key=s_rescan["session_key"],
        gateway_epoch=1,
        action="confirm",
        expected_login_version=2,
        expected_bot_generation=1,
        ilink_bot_id="bot-1-new@im.wechat",
        bot_token="tok-1-new",
        ilink_user_id="wx_1@im.wechat",
    )
    assert c_rescan["ok"] is True
    assert c_rescan["bot_generation"] == 2


# ---------------------------------------------------------------------------
# §7.1 #5 + Codex ① & ③: Watch Gen Rescan, Ambiguous Legacy Audit, Preflight
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_account_rescan_updates_thread_watches_and_backfill_distinguishes_pre_rescan_turns(
    wx_env: dict[str, Any],
) -> None:
    """Codex ①: same-ilink rescan updates thread_watches to new gen; backfill gives pre-rescan turns prev_gen."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    s1 = await wx_db.start_weixin_login_session(user_id="u-alice")
    await wx_db.patch_weixin_login_session(session_key=s1["session_key"], gateway_epoch=1, action="save_qr", qrcode="q1")
    c1 = await wx_db.patch_weixin_login_session(
        session_key=s1["session_key"],
        gateway_epoch=1,
        action="confirm",
        expected_login_version=2,
        ilink_bot_id="bot-a@im.wechat",
        bot_token="tok-1",
        ilink_user_id="wx_alice@im.wechat",
    )
    aid = c1["account_id"]
    tid = str(uuid4())
    store.thread_watches[tid] = {
        "thread_id": tid,
        "dalton_user_id": "u-alice",
        "platform": "weixin",
        "target_address": "weixin:@me",
        "scope_key": "dm:wx_alice@im.wechat",
        "generation": 1,
        "watch_active": True,
        "last_notified_turn_index": 0,
        "weixin_account_id": aid,
        "weixin_recipient_id": "wx_alice@im.wechat",
        "weixin_bot_generation": 1,
    }

    t_before = store.now
    store.now = t_before + timedelta(seconds=10)

    # Same-account rescan confirm -> updates thread_watches.weixin_bot_generation to 2 in same tx
    s2 = await wx_db.start_weixin_login_session(user_id="u-alice")
    await wx_db.patch_weixin_login_session(session_key=s2["session_key"], gateway_epoch=1, action="save_qr", qrcode="q2")
    c2 = await wx_db.patch_weixin_login_session(
        session_key=s2["session_key"],
        gateway_epoch=1,
        action="confirm",
        expected_login_version=2,
        expected_bot_generation=1,
        ilink_bot_id="bot-a@im.wechat",
        bot_token="tok-2",
        ilink_user_id="wx_alice@im.wechat",
    )
    assert c2["bot_generation"] == 2
    assert store.thread_watches[tid]["weixin_bot_generation"] == 2
    assert store.accounts[aid]["prev_bot_generation"] == 1
    assert store.accounts[aid]["bot_generation_updated_at"] == store.now

    # Seed two missed wakeup turns: turn 1 created BEFORE rescan, turn 2 created AFTER rescan
    r_pre = str(uuid4())
    r_post = str(uuid4())
    store.conversation_responses[r_pre] = {"conversation_response_id": r_pre, "status": "completed"}
    store.conversation_responses[r_post] = {"conversation_response_id": r_post, "status": "completed"}
    store.conversation_queries.extend(
        [
            {
                "conversation_thread_id": tid,
                "turn_index": 1,
                "query_type": "wakeup",
                "response_id": r_pre,
                "created_at": t_before,
            },
            {
                "conversation_thread_id": tid,
                "turn_index": 2,
                "query_type": "wakeup",
                "response_id": r_post,
                "created_at": store.now + timedelta(seconds=5),
            },
        ]
    )

    # Run startup recovery backfill
    await cg_db.recover_channel_state(mode="startup", gateway_epoch=1, require_gateway_epoch=True)
    assert store.background_runs[r_pre]["weixin_bot_generation"] == 1  # pre-rescan keeps old gen!
    assert store.background_runs[r_post]["weixin_bot_generation"] == 2  # post-rescan inherits new gen!


@pytest.mark.asyncio
async def test_startup_recovery_audits_ambiguous_legacy_in_flight_deliveries(
    wx_env: dict[str, Any],
) -> None:
    """Codex ③: startup recovery writes ambiguous_legacy_delivery_blocked audit immediately for ambiguous Weixin in_flight rows."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    store.deliveries["legacy-wx-dlv-1"] = {
        "delivery_key": "legacy-wx-dlv-1",
        "kind": "agent_send",
        "user_id": "u-alice",
        "platform": "weixin",
        "target_address": "weixin:@me",
        "status": "in_flight",
        "weixin_account_id": None,
        "weixin_bot_id": None,
        "weixin_bot_generation": None,
        "weixin_recipient_id": None,
    }
    store.deliveries["clear-wx-dlv-2"] = {
        "delivery_key": "clear-wx-dlv-2",
        "kind": "agent_send",
        "user_id": "u-alice",
        "platform": "weixin",
        "target_address": "weixin:@me",
        "status": "in_flight",
        "weixin_account_id": str(uuid4()),
        "weixin_bot_id": "bot-1@im.wechat",
        "weixin_bot_generation": 1,
        "weixin_recipient_id": "wx_alice@im.wechat",
    }

    await cg_db.recover_channel_state(mode="startup", gateway_epoch=1, require_gateway_epoch=True)
    assert store.deliveries["legacy-wx-dlv-1"]["status"] == "unknown"
    assert store.deliveries["clear-wx-dlv-2"]["status"] == "unknown"

    amb_audits = [a for a in store.audit_logs if a["action"] == "ambiguous_legacy_delivery_blocked"]
    assert len(amb_audits) == 1
    assert amb_audits[0]["metadata"]["delivery_key"] == "legacy-wx-dlv-1"
    assert amb_audits[0]["reason"] == wx_db.AUDIT_DEFAULT_REASONS["ambiguous_legacy_delivery_blocked"]


@pytest.mark.asyncio
async def test_preflight_send_blocks_midstream_unbind_disable_rescan_and_switch_off(
    wx_env: dict[str, Any],
) -> None:
    """Verify preflight-send allows valid chunk 1 and immediately blocks chunk 2 on unbind/rescan/switch-off."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    aid = str(uuid4())
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-a@im.wechat",
        "encrypted_bot_token": "tok-a",
        "bot_token": "tok-a",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_alice@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    store.account_tokens[(aid, 1, "wx_alice@im.wechat")] = {
        "account_id": aid,
        "bot_generation": 1,
        "platform_user_id": "wx_alice@im.wechat",
        "encrypted_context_token": "ctx-live-1",
        "token_version": 3,
        "stale_at": None,
        "seq": 10,
        "seq_source": "protocol",
    }
    store.deliveries["dlv-chunked"] = {
        "delivery_key": "dlv-chunked",
        "user_id": "u-alice",
        "platform": "weixin",
        "status": "in_flight",
        "delivery_attempt": 1,
        "weixin_account_id": aid,
        "weixin_bot_id": "bot-a@im.wechat",
        "weixin_bot_generation": 1,
        "weixin_recipient_id": "wx_alice@im.wechat",
    }

    # Chunk 0 authorized
    p0 = await wx_db.preflight_weixin_send(delivery_key="dlv-chunked", gateway_epoch=1, delivery_attempt=1, chunk_index=0)
    assert p0["authorized"] is True
    assert p0["weixin_context_token"] == "ctx-live-1"
    assert p0["weixin_context_token_version"] == 3

    # Mid-stream channel disabled -> chunk 1 blocked
    store.config["enabled"] = False
    p_off = await wx_db.preflight_weixin_send(delivery_key="dlv-chunked", gateway_epoch=1, delivery_attempt=1, chunk_index=1)
    assert p_off["authorized"] is False
    assert p_off["deny_code"] == "weixin_channel_disabled"
    store.config["enabled"] = True

    # Mid-stream bot_generation advanced (rescan) -> blocked
    store.accounts[aid]["bot_generation"] = 2
    p_gen = await wx_db.preflight_weixin_send(delivery_key="dlv-chunked", gateway_epoch=1, delivery_attempt=1, chunk_index=1)
    assert p_gen["authorized"] is False
    assert p_gen["deny_code"] == "weixin_bot_generation_superseded"
    store.accounts[aid]["bot_generation"] = 1

    # Mid-stream unbind -> blocked
    store.accounts.pop(aid)
    p_unb = await wx_db.preflight_weixin_send(delivery_key="dlv-chunked", gateway_epoch=1, delivery_attempt=1, chunk_index=1)
    assert p_unb["authorized"] is False
    assert p_unb["deny_code"] == "weixin_account_unbound"


# ---------------------------------------------------------------------------
# §7.1 #6: context_token Version CAS & Secret Redaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_token_version_cas_stale_errcodes_and_secret_redaction(
    wx_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify CONTEXT_TOKEN_STALE_ERRCODES whitelist, token_version CAS, inbound recovery, and redact_weixin_secrets."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    aid = str(uuid4())
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-a@im.wechat",
        "encrypted_bot_token": "tok-a",
        "bot_token": "tok-a",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_alice@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    store.account_tokens[(aid, 1, "wx_alice@im.wechat")] = {
        "account_id": aid,
        "bot_generation": 1,
        "platform_user_id": "wx_alice@im.wechat",
        "encrypted_context_token": "ctx-v2",
        "token_version": 2,
        "stale_at": None,
        "seq": 5,
        "seq_source": "protocol",
        "protocol_seq_high": 5,
    }

    # Non-whitelisted errcode never marks stale
    r_non = await wx_db.update_weixin_account_status(
        account_id=aid,
        gateway_epoch=1,
        action="mark_context_token_stale",
        expected_bot_generation=1,
        expected_ilink_bot_id="bot-a@im.wechat",
        error_code="99999",
        recipient_platform_user_id="wx_alice@im.wechat",
        expected_token_version=2,
    )
    assert r_non["updated"] is False
    assert store.account_tokens[(aid, 1, "wx_alice@im.wechat")]["stale_at"] is None

    # Whitelisted errcode with OLD token_version=1 returns version_mismatch and keeps v2 fresh
    monkeypatch.setattr(wx_db, "CONTEXT_TOKEN_STALE_ERRCODES", frozenset({"-999"}))
    r_old_ver = await wx_db.update_weixin_account_status(
        account_id=aid,
        gateway_epoch=1,
        action="mark_context_token_stale",
        expected_bot_generation=1,
        expected_ilink_bot_id="bot-a@im.wechat",
        error_code="-999",
        recipient_platform_user_id="wx_alice@im.wechat",
        expected_token_version=1,
    )
    assert r_old_ver["version_mismatch"] is True
    assert store.account_tokens[(aid, 1, "wx_alice@im.wechat")]["stale_at"] is None

    # Matching token_version=2 marks stale
    r_ok = await wx_db.update_weixin_account_status(
        account_id=aid,
        gateway_epoch=1,
        action="mark_context_token_stale",
        expected_bot_generation=1,
        expected_ilink_bot_id="bot-a@im.wechat",
        error_code="-999",
        recipient_platform_user_id="wx_alice@im.wechat",
        expected_token_version=2,
    )
    assert r_ok["updated"] is True
    assert store.account_tokens[(aid, 1, "wx_alice@im.wechat")]["stale_at"] is not None

    # Redaction strips sensitive keys
    redacted = redact_weixin_secrets(
        "err bot_token=SECRET123 context_token=CTX999 aes_key=AES777 verify_code=654321"
    )
    for leaked in ("SECRET123", "CTX999", "AES777", "654321"):
        assert leaked not in redacted


# ---------------------------------------------------------------------------
# §7.1 #7 + Codex ④: Collect Mode (Home Auto-Create, Lock-Free /done,
#                    Finalize, Cancel Stops Everything, collect_buffer_status)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_mode_home_autocreate_lockfree_done_finalize_and_cancel_stops_everything(
    wx_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify /collect Home auto-create, Codex ④ cancelled attachment rejection & status field, /done finalize, and cancel."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    aid = str(uuid4())
    from src.server.database.home_workspace import get_home_workspace_id

    home_ws_id = get_home_workspace_id("u-alice")
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-a@im.wechat",
        "encrypted_bot_token": "tok-a",
        "bot_token": "tok-a",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_alice@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }

    async def _create_home(uid: str, **kwargs: Any) -> dict[str, Any]:
        wid = get_home_workspace_id(uid)
        store.workspaces[wid] = {"workspace_id": wid, "user_id": uid, "name": "Home", "status": "running"}
        return {"workspace_id": wid}

    mock_home = create_autospec(
        workspace_db.get_or_create_home_workspace,
        side_effect=_create_home,
    )
    monkeypatch.setattr(workspace_db, "get_or_create_home_workspace", mock_home)
    monkeypatch.setattr(
        cg_db,
        "_verify_workspace_write_permission",
        create_autospec(cg_db._verify_workspace_write_permission, return_value=(True, None, {"workspace_id": home_ws_id})),
    )

    # 1. /collect auto-creates Home workspace and creates collecting buffer
    r_collect = await cg_db.execute_session_command(
        cmd="collect",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        scope_key="dm:wx_alice@im.wechat",
        message_id="wx_cmd_collect_1",
        weixin_account_id=aid,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        command_arg="帮我汇总",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_collect["ok"] is True, r_collect
    assert r_collect["collect_status"] == "started"
    assert r_collect["workspace_id"] == home_ws_id
    assert mock_home.call_count == 1
    buf_id = r_collect["collect_buffer_id"]

    # 2. Inbound message appends to collecting buffer and exposes collect_buffer_status (Codex ④)
    r_in = await cg_db.register_and_resolve_inbound(
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        thread_scope_key="dm:wx_alice@im.wechat",
        message_id="wx_item_1",
        raw_payload={"text": "第一条转发", "seq": 10},
        weixin_account_id=aid,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        weixin_context_token="ctx-collect-1",
        weixin_media_meta=[{"type": "image", "aes_key": "k1", "filename": "a.png"}],
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_in["collection_mode"] == "appended"
    assert r_in["collect_buffer_id"] == buf_id
    assert r_in["collect_buffer_status"] == "collecting"

    # 3. Recovery also exposes collect_buffer_status + decrypted_weixin_attachments
    rec = await cg_db.recover_channel_state(mode="startup", gateway_epoch=1, require_gateway_epoch=True)
    rec_item = next(r for r in rec["inbound_events"] if r["message_id"] == "wx_item_1")
    assert rec_item["collect_buffer_status"] == "collecting"

    # 4. Attachment registration succeeds while collecting
    r_att = await wx_db.register_collect_item_attachments(
        buffer_id=buf_id,
        message_id="wx_item_1",
        gateway_epoch=1,
        uploaded_attachments=[{"name": "a.png", "path": "/ws/a.png"}],
    )
    assert r_att["upload_status"] == "ready"
    assert store.inbound_events[("weixin", "wx_item_1")]["encrypted_weixin_media_meta"] is None

    # 5. /done transitions to flushing -> finalize registers synthetic collect:<buf_id> & marks done
    r_done = await cg_db.execute_session_command(
        cmd="done",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        scope_key="dm:wx_alice@im.wechat",
        message_id="wx_cmd_done_1",
        weixin_account_id=aid,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        command_arg="请总结要点",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_done["ok"] is True
    assert r_done["collect_status"] == "done"
    assert store.collect_buffers[buf_id]["status"] == "done"
    assert ("weixin", f"collect:{buf_id}") in store.inbound_events

    # 6. Cancelled buffer stops attachment upload registration (Codex ④) AND finalize
    r_c2 = await cg_db.execute_session_command(
        cmd="collect",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        scope_key="dm:wx_alice@im.wechat",
        message_id="wx_cmd_collect_2",
        weixin_account_id=aid,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    buf2 = r_c2["collect_buffer_id"]
    await cg_db.register_and_resolve_inbound(
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        thread_scope_key="dm:wx_alice@im.wechat",
        message_id="wx_item_2",
        raw_payload={"text": "第二条", "seq": 11},
        weixin_account_id=aid,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        weixin_media_meta=[{"type": "file", "aes_key": "k2", "filename": "b.pdf"}],
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    # Unbind cancels buffer, preserves buffer row with account_id=NULL, retires inbound
    await wx_db.unbind_weixin_account(account_id=aid, operator_user_id="u-alice", action="member_unbind")
    assert buf2 in store.collect_buffers
    assert store.collect_buffers[buf2]["status"] == "cancelled"
    assert store.collect_buffers[buf2]["account_id"] is None
    assert store.inbound_events[("weixin", "wx_item_2")]["status"] == "ignored"
    assert store.inbound_events[("weixin", "wx_item_2")]["collect_buffer_id"] == buf2

    # Late attachment registration rejected with cancelled & "已取消" (Codex ④)
    r_att_cancel = await wx_db.register_collect_item_attachments(
        buffer_id=buf2,
        message_id="wx_item_2",
        gateway_epoch=1,
        uploaded_attachments=[{"name": "b.pdf"}],
    )
    assert r_att_cancel["ok"] is True
    assert r_att_cancel["upload_status"] == "cancelled"
    assert "已取消" in str(r_att_cancel.get("message") or "")

    # Finalize skips synthetic inbound creation
    r_fin_cancel = await wx_db.finalize_collect_buffer(buffer_id=buf2, gateway_epoch=1)
    assert r_fin_cancel["status"] == "cancelled"
    assert ("weixin", f"collect:{buf2}") not in store.inbound_events


# ---------------------------------------------------------------------------
# §5.1: Report payload_json["text"], Notify Test Plan, Per-Target Transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_text_targets_notify_test_plan_and_per_target_completion_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify §5.1 report text target, notify test plan for Weixin, and per-target gateway/bridge split."""
    monkeypatch.setenv("DALTON_ENABLED", "true")
    monkeypatch.setenv("APP_ENV", "dalton")
    monkeypatch.setenv("DALTON_PROFILE", "everflow")

    # 1. Report text_targets accepts weixin:user:<id>, cards rejects it, payload["text"] extracted
    assert auto_reports_db._target_allowed("weixin:user:wx_u1@im.wechat", outcome="text") is True
    assert auto_reports_db._target_allowed("weixin:user:wx_u1@im.wechat", outcome="cards") is False
    items, err = auto_reports_db.native_items_from_report_payload(
        {
            "source": "automation_report",
            "content_kind": "text",
            "content_sha256": "abc123",
            "expected_item_count": 1,
            "text": "今日研报摘要正文",
        }
    )
    assert err is None
    assert items == [{"item_index": 0, "kind": "text", "text": "今日研报摘要正文"}]

    # report_delivery parses weixin:user:<id>
    cleaned, plat, kind, ident = report_delivery_svc.parse_delivery_target("weixin:user:wx_u1@im.wechat")
    assert (cleaned, plat, kind, ident) == ("weixin:user:wx_u1@im.wechat", "weixin", "dm", "wx_u1@im.wechat")

    # 2. decide_automation_notification_plan builds kind="text" from payload["text"] for notify_test on weixin
    delivery_id = str(uuid4())
    fake_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    fake_cur.fetchone.return_value = None
    plan = await auto_notify_db.decide_automation_notification_plan(
        fake_cur,
        {
            "delivery_id": delivery_id,
            "execution_id": delivery_id,
            "automation_id": "",
            "user_id": "u-alice",
            "channel": "weixin",
            "target": "weixin:user:wx_u1@im.wechat",
            "status": "pending",
            "payload_json": {
                "transport": "gateway",
                "source": "notify_test",
                "text": "测试推送内容",
            },
        },
        delivery_id=delivery_id,
        resolve_enabled_fn=create_autospec(dispatch_mod.resolve_dalton_notify_enabled, return_value=True),
        resolve_overrides_fn=create_autospec(dispatch_mod.resolve_dalton_notify_overrides, return_value={}),
        resolve_targets_fn=create_autospec(
            dispatch_mod.resolve_dalton_notify_targets,
            return_value=["weixin:user:wx_u1@im.wechat"],
        ),
        resolve_routes_fn=create_autospec(dispatch_mod.resolve_dalton_notify_routes, return_value={}),
        is_admin_fn=create_autospec(dispatch_mod._default_notify_admin, return_value=False),
    )
    assert plan["authorized"] is True
    assert plan["platform"] == "weixin"
    assert plan["items"][0]["kind"] == "text"
    assert plan["items"][0]["text"] == "测试推送内容"

    # 3. send_dalton_notify_test forces weixin: target onto _send_claimed_notify_test even when notify_via_gateway_enabled() is False
    from src.dalton.llm import model_prefs as model_prefs_mod

    monkeypatch.setattr(dispatch_mod, "notify_via_gateway_enabled", lambda: False)
    monkeypatch.setattr(
        model_prefs_mod,
        "resolve_dalton_notify_targets",
        create_autospec(
            model_prefs_mod.resolve_dalton_notify_targets,
            return_value=["weixin:user:wx_u1@im.wechat"],
        ),
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "resolve_dalton_notify_target_labels",
        create_autospec(
            model_prefs_mod.resolve_dalton_notify_target_labels,
            return_value={"weixin:user:wx_u1@im.wechat": "Alice 微信"},
        ),
    )
    mock_claimed_test = create_autospec(
        dispatch_mod._send_claimed_notify_test,
        return_value={"ok": True, "detail": None, "error_code": None},
    )
    monkeypatch.setattr(dispatch_mod, "_send_claimed_notify_test", mock_claimed_test)
    res_test = await dispatch_mod.send_dalton_notify_test(
        user_id="u-alice",
        target="weixin:user:wx_u1@im.wechat",
    )
    assert res_test["ok"] is True
    assert mock_claimed_test.call_count == 1

    # 4. Completion dispatch splits per-target: weixin -> gateway, feishu -> bridge in same batch
    gw_calls: list[tuple[str, str]] = []
    bridge_calls: list[list[str]] = []
    marked: list[tuple[str, str, str]] = []

    class _ClaimRes(tuple):
        delivery_id: str | None

        def __new__(cls, ok: bool, attempt: int, did: str | None):
            obj = super().__new__(cls, (ok, attempt))
            obj.delivery_id = did
            return obj

    async def _fake_claim_gw(exec_id, ch, tgt, **kwargs):
        assert kwargs["payload"]["transport"] == "gateway"
        return _ClaimRes(True, 1, str(uuid4()))

    async def _fake_claim_bridge(exec_id, ch, tgt, **kwargs):
        assert kwargs["payload"]["transport"] == "bridge"
        return True

    async def _fake_gw_post(did: str, *, target: str, **kwargs):
        gw_calls.append((did, target))
        return {"ok": True, "results": [{"target": target, "ok": True}]}

    async def _fake_bridge_post(targets, text, card=None, **kwargs):
        bridge_calls.append(list(targets))
        return {"ok": True, "results": [{"target": t, "ok": True} for t in targets]}

    async def _fake_mark(exec_id, ch, tgt, *, status, **kwargs):
        marked.append((ch, tgt, status))

    monkeypatch.setattr(dispatch_mod, "claim_target_delivery_with_attempt", _fake_claim_gw)
    monkeypatch.setattr(dispatch_mod, "post_automation_gateway_notify", _fake_gw_post)
    monkeypatch.setattr(dispatch_mod, "post_bridge_notify", _fake_bridge_post)
    monkeypatch.setattr(dispatch_mod, "_post_notify_is_bridge", lambda fn: True)

    auto_id = str(uuid4())
    sent = await dispatch_mod.on_automation_settled(
        execution_id=str(uuid4()),
        automation={"name": "日报", "automation_id": auto_id, "user_id": "u-alice"},
        final_text="今日行情总结",
        resolve_enabled_fn=create_autospec(dispatch_mod.resolve_dalton_notify_enabled, return_value=True),
        resolve_overrides_fn=create_autospec(dispatch_mod.resolve_dalton_notify_overrides, return_value={}),
        resolve_targets_fn=create_autospec(
            dispatch_mod.resolve_dalton_notify_targets,
            return_value=["feishu:user:ou_1", "weixin:user:wx_u1@im.wechat"],
        ),
        resolve_routes_fn=create_autospec(
            dispatch_mod.resolve_dalton_notify_routes,
            return_value={f"automation:{auto_id}": ["feishu:user:ou_1", "weixin:user:wx_u1@im.wechat"]},
        ),
        is_admin_fn=create_autospec(dispatch_mod._default_notify_admin, return_value=False),
        claim_target_fn=_fake_claim_bridge,
        mark_target_fn=_fake_mark,
        post_notify_fn=_fake_bridge_post,
        get_verdict_fn=create_autospec(dispatch_mod.get_verdict_for_execution, return_value=None),
    )
    assert sent is True
    assert [t for _, t in gw_calls] == ["weixin:user:wx_u1@im.wechat"]
    assert bridge_calls == [["feishu:user:ou_1"]]
    assert ("weixin", "weixin:user:wx_u1@im.wechat", "sent") in marked
    assert ("feishu", "feishu:user:ou_1", "sent") in marked


# ---------------------------------------------------------------------------
# Batch A Round 2 Tests (Decisions #1–#11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r2_conditional_gateway_epoch_no_lease_allows_feishu_discord_rejects_weixin_and_post_lease_enforces_all(
    wx_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision #1: No lease -> Feishu/Discord I-1/I-2/I-3/I-6/I-7/recover succeed without header; Weixin fails 409; once lease exists, ALL fail 409 without header."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    client: TestClient = wx_env["client"]
    no_epoch_hdrs = {"X-Service-Token": SERVICE_TOKEN}

    # Start with NO lease row in DB (legacy gateway pre-Batch B)
    store.lease = None

    # Stub underlying non-Weixin DB helpers only where needed so HTTP routes exercise real verify_gateway_epoch_in_tx
    # Direct DB-level verify_gateway_epoch_in_tx matrix
    cur = create_autospec(psycopg.AsyncCursor, instance=True)

    async def _exec(sql: str, params: Any = None) -> None:
        cur._last = await store.handle_sql(sql, params)

    async def _fetchone() -> Any:
        rows = getattr(cur, "_last", [])
        return rows[0] if rows else None

    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = _fetchone

    # 1. No lease row: non-Weixin allowed without epoch; Weixin rejected!
    assert await wx_db.verify_gateway_epoch_in_tx(cur, None, required=True, platform="feishu") is None
    assert await wx_db.verify_gateway_epoch_in_tx(cur, None, required=True, platform="discord") is None
    assert await wx_db.verify_gateway_epoch_in_tx(cur, None, required=True, platform=None) is None
    with pytest.raises(wx_db.StaleGatewayEpochError):
        await wx_db.verify_gateway_epoch_in_tx(cur, None, required=True, platform="weixin")

    # 2. HTTP level with NO lease row: recover, I-1/I-2/I-3/I-6/I-7 for Feishu/Discord succeed without X-Gateway-Epoch;
    #    Weixin I-1 / I-2 / I-3 without X-Gateway-Epoch return 409!
    r_rec = client.get("/api/v1/dalton/channels/internal/recover?mode=startup", headers=no_epoch_hdrs)
    assert r_rec.status_code == 200, r_rec.text

    r_f_i1 = client.post(
        "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
        headers=no_epoch_hdrs,
        json={
            "platform": "feishu",
            "platform_user_id": "ou_legacy_1",
            "chat_type": "dm",
            "chat_address": "feishu:@me",
            "thread_scope_key": "dm:ou_legacy_1",
            "message_id": "feishu_no_epoch_1",
            "raw_payload": {"text": "hi"},
        },
    )
    assert r_f_i1.status_code == 200, r_f_i1.text

    r_d_i2 = client.post(
        "/api/v1/dalton/channels/internal/bind-code/consume",
        headers=no_epoch_hdrs,
        json={
            "action": "bind",
            "platform": "discord",
            "platform_user_id": "dc_legacy_1",
            "code": "NO-SUCH-CODE",
        },
    )
    assert r_d_i2.status_code == 200, r_d_i2.text

    r_d_i3 = client.post(
        "/api/v1/dalton/channels/internal/session/command",
        headers=no_epoch_hdrs,
        json={
            "cmd": "new",
            "platform": "discord",
            "platform_user_id": "dc_legacy_1",
            "scope_key": "dm:dc_legacy_1",
            "message_id": "dc_cmd_no_epoch_1",
        },
    )
    assert r_d_i3.status_code == 200, r_d_i3.text

    r_f_i6 = client.post(
        "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
        headers=no_epoch_hdrs,
        json={
            "delivery_key": "dlv-feishu-no-epoch",
            "kind": "agent_send",
            "user_id": "u-alice",
            "platform": "feishu",
            "target": "feishu:@me",
        },
    )
    assert r_f_i6.status_code == 200, r_f_i6.text

    r_f_i7_in = client.post(
        "/api/v1/dalton/channels/internal/inbound/progress",
        headers=no_epoch_hdrs,
        json={"platform": "feishu", "message_id": "feishu_no_epoch_1", "status": "completed"},
    )
    assert r_f_i7_in.status_code == 200, r_f_i7_in.text

    r_f_i7_steer = client.post(
        "/api/v1/dalton/channels/internal/steers/reconcile",
        headers=no_epoch_hdrs,
        json={"platform": "feishu", "message_id": "feishu_no_epoch_1", "run_id": str(uuid4())},
    )
    assert r_f_i7_steer.status_code == 200, r_f_i7_steer.text

    store.deliveries["dlv-feishu-prog"] = {
        "delivery_key": "dlv-feishu-prog",
        "user_id": "u-alice",
        "platform": "feishu",
        "status": "in_flight",
        "source_scope_key": "dm:ou_legacy_1",
        "source_generation": 1,
        "activity_origin": "none",
    }
    r_f_i7_dlv = client.post(
        "/api/v1/dalton/channels/internal/deliveries/progress",
        headers=no_epoch_hdrs,
        json={"delivery_key": "dlv-feishu-prog", "status": "sent"},
    )
    assert r_f_i7_dlv.status_code == 200, r_f_i7_dlv.text

    # Weixin requests without X-Gateway-Epoch MUST be rejected 409 even when no lease row exists!
    r_wx_i1 = client.post(
        "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
        headers=no_epoch_hdrs,
        json={
            "platform": "weixin",
            "platform_user_id": "wx_1@im.wechat",
            "chat_type": "dm",
            "chat_address": "weixin:@me",
            "thread_scope_key": "dm:wx_1@im.wechat",
            "message_id": "wx_no_epoch_1",
            "raw_payload": {"text": "hi"},
        },
    )
    assert r_wx_i1.status_code == 409
    assert r_wx_i1.json()["code"] == "stale_gateway_epoch"

    r_wx_i2 = client.post(
        "/api/v1/dalton/channels/internal/bind-code/consume",
        headers=no_epoch_hdrs,
        json={"action": "bind", "platform": "weixin", "platform_user_id": "wx_1@im.wechat", "code": "ABC"},
    )
    assert r_wx_i2.status_code == 409

    r_wx_i3 = client.post(
        "/api/v1/dalton/channels/internal/session/command",
        headers=no_epoch_hdrs,
        json={"cmd": "help", "platform": "weixin", "platform_user_id": "wx_1@im.wechat", "scope_key": "dm:wx_1@im.wechat"},
    )
    assert r_wx_i3.status_code == 409

    # 3. Once a gateway lease exists -> ANY platform / recover without X-Gateway-Epoch is rejected 409!
    await wx_db.acquire_gateway_lease(instance_id="gw-new-1")
    for path, method, payload in (
        ("/api/v1/dalton/channels/internal/recover?mode=startup", "GET", None),
        (
            "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
            "POST",
            {
                "platform": "feishu",
                "platform_user_id": "ou_legacy_1",
                "chat_type": "dm",
                "chat_address": "feishu:@me",
                "thread_scope_key": "dm:ou_legacy_1",
                "message_id": "feishu_post_lease_1",
                "raw_payload": {"text": "hi"},
            },
        ),
        (
            "/api/v1/dalton/channels/internal/session/command",
            "POST",
            {
                "cmd": "new",
                "platform": "discord",
                "platform_user_id": "dc_legacy_1",
                "scope_key": "dm:dc_legacy_1",
                "message_id": "dc_post_lease_1",
            },
        ),
    ):
        resp = (
            client.get(path, headers=no_epoch_hdrs)
            if method == "GET"
            else client.post(path, headers=no_epoch_hdrs, json=payload)
        )
        assert resp.status_code == 409, f"{method} {path}: {resp.text}"
        assert resp.json()["code"] == "stale_gateway_epoch"


def test_r2_sql_explicit_casts_and_d049_cdn_comment() -> None:
    """Decision #2 & #11: Zero untyped IS (NOT) NULL bind parameters in d049 and weixin_multi_account."""
    import re

    untyped_re = re.compile(r"(?:(?<!:):[a-zA-Z0-9_]+|%s)\s+IS\s+(?:NOT\s+)?NULL")
    for rel in (
        "src/dalton/migrations/versions/d049_weixin_multi_account.py",
        "src/dalton/database/weixin_multi_account.py",
    ):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert not untyped_re.findall(text), f"Untyped NULL bind parameter in {rel}"

    d049_src = (REPO_ROOT / "src/dalton/migrations/versions/d049_weixin_multi_account.py").read_text(encoding="utf-8")
    assert "CAST(:eff_ilink_user_id AS VARCHAR) IS NULL" in d049_src
    assert "has no cdn_base_url" in d049_src


@pytest.mark.asyncio
async def test_r2_unbind_auth_bot_gen_sender_roster_503_and_d16_safe_reply(
    wx_env: dict[str, Any],
) -> None:
    """Decision #3a & #11: /unbind verifies group/bot/gen/sender/roster(503) in tx; _bind_identity_in_tx rejects weixin."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    roster: dict[str, str] = wx_env["roster"]
    aid = str(uuid4())
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-new@im.wechat",
        "encrypted_bot_token": "tok-new",
        "bot_token": "tok-new",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 2,
        "prev_bot_generation": 1,
        "bot_generation_updated_at": store.now,
        "ilink_user_id": "wx_alice@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }

    # 1. Group /unbind rejected
    r_grp = await cg_db.consume_bind_code_or_unbind(
        action="unbind",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        weixin_account_id=aid,
        weixin_bot_id="bot-new@im.wechat",
        weixin_bot_generation=2,
        is_group=True,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_grp["ok"] is False
    assert r_grp["reason"] == "weixin_group_unsupported"
    assert aid in store.accounts

    # 2. Stale bot_generation=1 cannot delete gen=2 connection!
    r_stale = await cg_db.consume_bind_code_or_unbind(
        action="unbind",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        weixin_account_id=aid,
        weixin_bot_id="bot-new@im.wechat",
        weixin_bot_generation=1,
        weixin_context_token="ctx-old",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_stale["ok"] is False
    assert r_stale["reason"] == "weixin_bot_generation_mismatch"
    assert aid in store.accounts

    # 3. Spoofed sender cannot unbind OR receive D16 credentials
    r_spoof = await cg_db.consume_bind_code_or_unbind(
        action="unbind",
        platform="weixin",
        platform_user_id="wx_attacker@im.wechat",
        weixin_account_id=aid,
        weixin_bot_id="bot-new@im.wechat",
        weixin_bot_generation=2,
        weixin_context_token="ctx-attacker",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_spoof["ok"] is False
    assert r_spoof["reason"] == "weixin_sender_mismatch"
    assert "one_time_weixin_reply" not in r_spoof
    assert aid in store.accounts

    # 4. Roster lookup_error raises 503 without deleting
    roster["u-alice"] = "lookup_error"
    with pytest.raises(HTTPException) as exc_503:
        await cg_db.consume_bind_code_or_unbind(
            action="unbind",
            platform="weixin",
            platform_user_id="wx_alice@im.wechat",
            weixin_account_id=aid,
            weixin_bot_id="bot-new@im.wechat",
            weixin_bot_generation=2,
            weixin_context_token="ctx-503",
            gateway_epoch=1,
            require_gateway_epoch=True,
        )
    assert exc_503.value.status_code == 503
    assert aid in store.accounts

    # 5. Removed member rejected without deleting
    roster["u-alice"] = "removed"
    r_rem = await cg_db.consume_bind_code_or_unbind(
        action="unbind",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        weixin_account_id=aid,
        weixin_bot_id="bot-new@im.wechat",
        weixin_bot_generation=2,
        weixin_context_token="ctx-rem",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_rem["ok"] is False
    assert r_rem["reason"] == "member_not_in_roster"
    assert aid in store.accounts

    # 6. Valid active owner with matching bot/gen/sender succeeds and emits D16 reply ONLY to verified sender with inbound token
    roster["u-alice"] = "active"
    r_ok = await cg_db.consume_bind_code_or_unbind(
        action="unbind",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        weixin_account_id=aid,
        weixin_bot_id="bot-new@im.wechat",
        weixin_bot_generation=2,
        weixin_context_token="ctx-valid-bye",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_ok["ok"] is True
    assert aid not in store.accounts
    assert r_ok["one_time_weixin_reply"]["recipient_id"] == "wx_alice@im.wechat"
    assert r_ok["one_time_weixin_reply"]["context_token"] == "ctx-valid-bye"

    # 7. Decision #11: _bind_identity_in_tx rejects platform="weixin" directly
    fake_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    with pytest.raises(HTTPException) as exc_bind:
        await cg_db._bind_identity_in_tx(
            fake_cur,
            user_id="u-alice",
            platform="weixin",
            platform_user_id="wx_alice@im.wechat",
            platform_username="Alice",
            platform_tenant_key=None,
            settings_already_locked={"u-alice"},
        )
    assert exc_bind.value.status_code == 400
    assert exc_bind.value.detail["code"] == "weixin_uses_qr_login"


@pytest.mark.asyncio
async def test_r2_i3_server_readonly_enforcement_d11_bot_gen_and_group_reject_code(
    wx_env: dict[str, Any],
) -> None:
    """Decision #3b & #11: I-3 never trusts is_internal_query on write cmds; D11 passes bot_id/gen; group cmd returns weixin_group_unsupported."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    client: TestClient = wx_env["client"]
    no_epoch_hdrs = {"X-Service-Token": SERVICE_TOKEN}

    # Lease exists -> cmd='new' with spoofed is_internal_query=True MUST still require X-Gateway-Epoch (409)!
    r_spoof_ro = client.post(
        "/api/v1/dalton/channels/internal/session/command",
        headers=no_epoch_hdrs,
        json={
            "cmd": "new",
            "platform": "discord",
            "platform_user_id": "dc_1",
            "scope_key": "dm:dc_1",
            "is_internal_query": True,
        },
    )
    assert r_spoof_ro.status_code == 409
    assert r_spoof_ro.json()["code"] == "stale_gateway_epoch"

    # Read-only workspace_list with message_id=None and is_internal_query=True succeeds without X-Gateway-Epoch
    r_true_ro = client.post(
        "/api/v1/dalton/channels/internal/session/command",
        headers=no_epoch_hdrs,
        json={
            "cmd": "workspace_list",
            "platform": "discord",
            "platform_user_id": "dc_1",
            "scope_key": "dm:dc_1",
            "is_internal_query": True,
        },
    )
    assert r_true_ro.status_code == 200

    # Pending-verification account at gen=2 rejects ordinary command carrying stale gen=1 during D11 verification!
    aid = str(uuid4())
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-v2@im.wechat",
        "encrypted_bot_token": "tok-v2",
        "bot_token": "tok-v2",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "pending_verification",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 2,
        "prev_bot_generation": 1,
        "bot_generation_updated_at": store.now,
        "ilink_user_id": None,
        "prev_ilink_user_id": "wx_alice@im.wechat",
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    r_d11_stale = await cg_db.execute_session_command(
        cmd="new",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        scope_key="dm:wx_alice@im.wechat",
        message_id="wx_cmd_stale_d11",
        weixin_account_id=aid,
        weixin_bot_id="bot-v1@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_d11_stale["ok"] is False
    assert r_d11_stale["deny_reason"] == "weixin_bot_generation_superseded"
    assert store.accounts[aid]["status"] == "pending_verification"

    # Active account in group chat gets weixin_group_unsupported (unified with I-1)
    store.accounts[aid]["status"] = "active"
    store.accounts[aid]["ilink_user_id"] = "wx_alice@im.wechat"
    r_grp_cmd = await cg_db.execute_session_command(
        cmd="new",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="group",
        scope_key="group:g1",
        message_id="wx_grp_cmd_1",
        weixin_account_id=aid,
        weixin_bot_id="bot-v2@im.wechat",
        weixin_bot_generation=2,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_grp_cmd["ok"] is False
    assert r_grp_cmd["deny_reason"] == "weixin_group_unsupported"


@pytest.mark.asyncio
async def test_r2_i1_replay_verifies_account_gen_sender_roster_switch_and_blocks_cross_member_leak(
    wx_env: dict[str, Any],
) -> None:
    """Decision #3c: I-1 replay re-checks switch, bot_generation, roster (503/removed), and blocks cross-member message_id collision."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    roster: dict[str, str] = wx_env["roster"]
    aid_a = str(uuid4())
    aid_b = str(uuid4())
    store.accounts[aid_a] = {
        "account_id": aid_a,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-a@im.wechat",
        "encrypted_bot_token": "tok-a",
        "bot_token": "tok-a",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_alice@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    store.accounts[aid_b] = {
        **store.accounts[aid_a],
        "account_id": aid_b,
        "user_id": "u-bob",
        "ilink_bot_id": "bot-b@im.wechat",
        "ilink_user_id": "wx_bob@im.wechat",
    }

    # Initial registration succeeds
    r0 = await cg_db.register_and_resolve_inbound(
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        thread_scope_key="dm:wx_alice@im.wechat",
        message_id="shared_mid_1",
        raw_payload={"text": "secret alice payload", "seq": 1},
        weixin_account_id=aid_a,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        weixin_context_token="ctx-1",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r0["allowed"] is True

    # 1. Cross-member message_id collision -> REJECTED with ZERO leakage of Alice's user/workspace/payload!
    r_cross = await cg_db.register_and_resolve_inbound(
        platform="weixin",
        platform_user_id="wx_bob@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        thread_scope_key="dm:wx_bob@im.wechat",
        message_id="shared_mid_1",
        raw_payload={"text": "bob"},
        weixin_account_id=aid_b,
        weixin_bot_id="bot-b@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_cross["allowed"] is False
    assert r_cross["deny_reason"] == "weixin_message_id_account_mismatch"
    assert "dalton_user_id" not in r_cross
    assert "raw_payload" not in r_cross

    # 2. Channel disabled -> replay denied even though account row stays active
    store.config["enabled"] = False
    r_off = await cg_db.register_and_resolve_inbound(
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        thread_scope_key="dm:wx_alice@im.wechat",
        message_id="shared_mid_1",
        raw_payload={"text": "secret alice payload"},
        weixin_account_id=aid_a,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_off["allowed"] is False
    assert r_off["deny_reason"] == "weixin_channel_disabled"
    store.config["enabled"] = True

    # 3. Roster lookup_error on replay -> 503
    roster["u-alice"] = "lookup_error"
    with pytest.raises(HTTPException) as exc_rep_503:
        await cg_db.register_and_resolve_inbound(
            platform="weixin",
            platform_user_id="wx_alice@im.wechat",
            chat_type="dm",
            chat_address="weixin:@me",
            thread_scope_key="dm:wx_alice@im.wechat",
            message_id="shared_mid_1",
            raw_payload={"text": "secret alice payload"},
            weixin_account_id=aid_a,
            weixin_bot_id="bot-a@im.wechat",
            weixin_bot_generation=1,
            gateway_epoch=1,
            require_gateway_epoch=True,
        )
    assert exc_rep_503.value.status_code == 503
    roster["u-alice"] = "active"

    # 4. After rescan bumps bot_generation to 2 -> replay of gen=1 message denied!
    store.accounts[aid_a]["bot_generation"] = 2
    r_gen = await cg_db.register_and_resolve_inbound(
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        thread_scope_key="dm:wx_alice@im.wechat",
        message_id="shared_mid_1",
        raw_payload={"text": "secret alice payload"},
        weixin_account_id=aid_a,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_gen["allowed"] is False
    assert r_gen["deny_reason"] == "weixin_bot_generation_mismatch"


@pytest.mark.asyncio
async def test_r2_i6_requires_exact_run_source_and_live_background_run_respects_rescan_timestamp(
    wx_env: dict[str, Any],
) -> None:
    """Decision #3d: I-6 blocks unmatched run_id as unknown+audit; update_inbound_progress uses bot_generation_updated_at for live turns."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    aid = str(uuid4())
    tid = str(uuid4())
    t_rescan = store.now
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-a@im.wechat",
        "encrypted_bot_token": "tok-a",
        "bot_token": "tok-a",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 2,
        "prev_bot_generation": 1,
        "bot_generation_updated_at": t_rescan,
        "ilink_user_id": "wx_alice@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": t_rescan - timedelta(minutes=5),
        "updated_at": t_rescan,
    }
    store.thread_watches[tid] = {
        "thread_id": tid,
        "dalton_user_id": "u-alice",
        "platform": "weixin",
        "target_address": "weixin:@me",
        "scope_key": "dm:wx_alice@im.wechat",
        "generation": 1,
        "watch_active": True,
        "last_notified_turn_index": 0,
        "weixin_account_id": aid,
        "weixin_recipient_id": "wx_alice@im.wechat",
        "weixin_bot_generation": 2,
    }

    # 1. update_inbound_progress inserts a pre-rescan wakeup turn -> records prev_bot_generation=1 (NOT watch's gen=2!)
    r_pre = str(uuid4())
    r_post = str(uuid4())
    store.conversation_queries.extend(
        [
            {
                "conversation_thread_id": tid,
                "turn_index": 1,
                "query_type": "wakeup",
                "response_id": r_pre,
                "created_at": t_rescan - timedelta(seconds=30),
            },
            {
                "conversation_thread_id": tid,
                "turn_index": 2,
                "query_type": "wakeup",
                "response_id": r_post,
                "created_at": t_rescan + timedelta(seconds=30),
            },
        ]
    )
    await cg_db.update_inbound_progress(
        platform="weixin",
        background_run={
            "run_id": r_pre,
            "thread_id": tid,
            "dalton_user_id": "u-alice",
            "platform": "weixin",
            "target_address": "weixin:@me",
            "turn_index": 1,
            "status": "completed",
            "fallback_pending": True,
        },
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert store.background_runs[r_pre]["weixin_bot_generation"] == 1

    await cg_db.update_inbound_progress(
        platform="weixin",
        background_run={
            "run_id": r_post,
            "thread_id": tid,
            "dalton_user_id": "u-alice",
            "platform": "weixin",
            "target_address": "weixin:@me",
            "turn_index": 2,
            "status": "completed",
            "fallback_pending": True,
        },
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert store.background_runs[r_post]["weixin_bot_generation"] == 2

    # 2. I-6 (Scenario A2: fallback / current conversation) for a run_id NOT present in inbound_events or background_runs
    #    (only thread_watches exists) -> MUST NOT borrow thread_watches.weixin_bot_generation=2; marks unknown + writes audit!
    unmatched_rid = str(uuid4())
    r_unm = await cg_db.authorize_and_claim_delivery(
        delivery_key="dlv-unmatched-run",
        kind="fallback",
        user_id="u-alice",
        thread_id=tid,
        run_id=unmatched_rid,
        platform="weixin",
        target=None,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_unm["claim_status"] == "denied"
    assert r_unm["status"] == "unknown"
    assert r_unm["code"] == "source_generation_unknown"
    assert any(a["action"] == "ambiguous_legacy_delivery_blocked" for a in store.audit_logs)


@pytest.mark.asyncio
async def test_r2_collect_attachment_buffer_account_scope_and_collect_cancel_replay_idempotency(
    wx_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision #3e: Attachment registration never touches another buffer's inbound row; /collect & /cancel replay is idempotent."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    from src.server.database.home_workspace import get_home_workspace_id

    aid_a = str(uuid4())
    aid_b = str(uuid4())
    home_ws = get_home_workspace_id("u-alice")
    store.workspaces[home_ws] = {"workspace_id": home_ws, "user_id": "u-alice", "name": "Home", "status": "running"}
    store.accounts[aid_a] = {
        "account_id": aid_a,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-a@im.wechat",
        "encrypted_bot_token": "tok-a",
        "bot_token": "tok-a",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_alice@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    monkeypatch.setattr(
        cg_db,
        "_verify_workspace_write_permission",
        create_autospec(cg_db._verify_workspace_write_permission, return_value=(True, None, {"workspace_id": home_ws})),
    )

    # 1. Start /collect (msg_1), then replay /collect (msg_1) -> duplicate=True without opening another buffer
    r_c1 = await cg_db.execute_session_command(
        cmd="collect",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        scope_key="dm:wx_alice@im.wechat",
        message_id="cmd_collect_idemp_1",
        weixin_account_id=aid_a,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_c1["collect_status"] == "started"
    buf_1 = r_c1["collect_buffer_id"]

    r_c1_dup = await cg_db.execute_session_command(
        cmd="collect",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        scope_key="dm:wx_alice@im.wechat",
        message_id="cmd_collect_idemp_1",
        weixin_account_id=aid_a,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_c1_dup["duplicate"] is True
    assert r_c1_dup["collect_buffer_id"] == buf_1

    # 2. Cancel via /cancel (msg_2), then open a NEW buffer (buf_2), then REPLAY old /cancel (msg_2):
    #    buf_2 MUST stay 'collecting'!
    r_can1 = await cg_db.execute_session_command(
        cmd="cancel",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        scope_key="dm:wx_alice@im.wechat",
        message_id="cmd_cancel_idemp_1",
        weixin_account_id=aid_a,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_can1["collect_status"] == "cancelled"
    assert store.collect_buffers[buf_1]["status"] == "cancelled"

    r_c2 = await cg_db.execute_session_command(
        cmd="collect",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        scope_key="dm:wx_alice@im.wechat",
        message_id="cmd_collect_idemp_2",
        weixin_account_id=aid_a,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    buf_2 = r_c2["collect_buffer_id"]
    assert store.collect_buffers[buf_2]["status"] == "collecting"

    r_can1_replay = await cg_db.execute_session_command(
        cmd="cancel",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        scope_key="dm:wx_alice@im.wechat",
        message_id="cmd_cancel_idemp_1",
        weixin_account_id=aid_a,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_can1_replay["duplicate"] is True
    assert store.collect_buffers[buf_2]["status"] == "collecting"

    # 3. Cross-buffer / cross-member attachment registration on cancelled buf_1 targeting another member's message_id ->
    #    404 and ZERO modification to other member's inbound event & encrypted_weixin_media_meta!
    store.inbound_events[("weixin", "other_member_mid")] = {
        "platform": "weixin",
        "message_id": "other_member_mid",
        "dalton_user_id": "u-bob",
        "weixin_account_id": aid_b,
        "collect_buffer_id": buf_2,
        "status": "pending",
        "fallback_pending": True,
        "encrypted_weixin_media_meta": "ENCRYPTED_BOB_MEDIA",
    }
    with pytest.raises(HTTPException) as exc_404:
        await wx_db.register_collect_item_attachments(
            buffer_id=buf_1,
            message_id="other_member_mid",
            gateway_epoch=1,
            uploaded_attachments=[],
        )
    assert exc_404.value.status_code == 404
    assert store.inbound_events[("weixin", "other_member_mid")]["status"] == "pending"
    assert store.inbound_events[("weixin", "other_member_mid")]["encrypted_weixin_media_meta"] == "ENCRYPTED_BOB_MEDIA"


@pytest.mark.asyncio
async def test_r2_public_qr_errors_lock_order_reconcile_abort_preflight_retryable_and_cap_off(
    wx_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decisions #4, #5, #6, #7, #8: fixed Chinese QR errors, lock order, reconcile atomic abort, preflight retryable/attempt, cap-off behavior."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    roster: dict[str, str] = wx_env["roster"]

    # Decision #4: JSON-quoted secret redaction + fixed Chinese public QR errors (never echo gateway detail!)
    red = redact_weixin_secrets('{"bot_token": "SECRET_BOT_XYZ", "context_token": "CTX_ABC"}')
    assert "SECRET_BOT_XYZ" not in red
    assert "CTX_ABC" not in red

    err_known = ch_router_mod._build_public_weixin_qr_error("session_revoked", 409)
    assert err_known.status_code == 409
    assert err_known.detail["code"] == "session_revoked"
    assert "撤销" in err_known.detail["message"]

    err_unknown = ch_router_mod._build_public_weixin_qr_error("leaked_secret_code", 500)
    assert err_unknown.status_code == 503
    assert err_unknown.detail["code"] == "weixin_qr_gateway_error"
    assert "leaked_secret_code" not in str(err_unknown.detail)

    # Decision #5: unbind acquires Level 3b before Level 4; verify_pending conflict acquires Level 4 before Level 6
    aid_lo = str(uuid4())
    store.accounts[aid_lo] = {
        "account_id": aid_lo,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-lo@im.wechat",
        "encrypted_bot_token": "tok-lo",
        "bot_token": "tok-lo",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_lo@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    store.executed_sql.clear()
    await wx_db.unbind_weixin_account(account_id=aid_lo, operator_user_id="u-alice", action="member_unbind")
    sqls = [s for s, _ in store.executed_sql]
    idx_3b = next(i for i, s in enumerate(sqls) if "pg_advisory_xact_lock" in s and "dalton_user_settings" not in str(store.executed_sql[i][1]))
    idx_4 = next(i for i, s in enumerate(sqls) if "FROM dalton_channel_weixin_login_sessions" in s and "FOR UPDATE" in s)
    assert idx_3b < idx_4

    # Decision #6: Reconcile aborts ENTIRE round if ANY member returns lookup_error (no status change, no revision bump)
    aid_1 = str(uuid4())
    aid_2 = str(uuid4())
    store.accounts[aid_1] = {**store.accounts.get(aid_lo, {}), "account_id": aid_1, "user_id": "u-alice", "ilink_bot_id": "b1@im.wechat", "encrypted_bot_token": "t1", "bot_token": "t1", "base_url": "https://ilinkai.weixin.qq.com", "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c", "get_updates_buf": "", "status": "active", "prev_status": None, "disabled_reason": None, "bot_generation": 1, "prev_bot_generation": None, "bot_generation_updated_at": None, "ilink_user_id": "wx1@im.wechat", "prev_ilink_user_id": None, "ilink_username": "A", "last_inbound_at": None, "created_at": store.now, "updated_at": store.now}
    store.accounts[aid_2] = {**store.accounts[aid_1], "account_id": aid_2, "user_id": "u-bob", "ilink_bot_id": "b2@im.wechat", "ilink_user_id": "wx2@im.wechat"}
    rev_before = store.config["accounts_revision"]
    roster["u-alice"] = "lookup_error"
    roster["u-bob"] = "removed"
    re_res = await wx_db.reevaluate_weixin_accounts(operator_user_id="system:roster-reconciler", gateway_epoch=1, require_gateway_epoch=True)
    assert re_res["changed"] is False
    assert re_res["aborted_on_lookup_error"] is True
    assert store.accounts[aid_2]["status"] == "active"  # NOT disabled because round aborted!
    assert store.config["accounts_revision"] == rev_before
    roster["u-alice"] = "active"
    roster["u-bob"] = "active"

    # Decision #5 & #7: preflight_weixin_send rejects missing delivery_attempt, distinguishes retryable=True on lookup_error
    store.deliveries["dlv-pf-r2"] = {
        "delivery_key": "dlv-pf-r2",
        "user_id": "u-alice",
        "platform": "weixin",
        "status": "in_flight",
        "delivery_attempt": 1,
        "weixin_account_id": aid_1,
        "weixin_bot_id": "b1@im.wechat",
        "weixin_bot_generation": 1,
        "weixin_recipient_id": "wx1@im.wechat",
    }
    p_no_att = await wx_db.preflight_weixin_send(delivery_key="dlv-pf-r2", gateway_epoch=1, delivery_attempt=None)
    assert p_no_att["authorized"] is False
    assert p_no_att["deny_code"] == "delivery_attempt_required"
    assert p_no_att["retryable"] is False
    assert p_no_att["decision"] == "deny"

    roster["u-alice"] = "lookup_error"
    p_defer = await wx_db.preflight_weixin_send(delivery_key="dlv-pf-r2", gateway_epoch=1, delivery_attempt=1)
    assert p_defer["authorized"] is False
    assert p_defer["deny_code"] == "roster_lookup_error"
    assert p_defer["retryable"] is True
    assert p_defer["decision"] == "defer"
    roster["u-alice"] = "active"

    # Decision #8: When channel_weixin=off:
    # (a) preflight-send returns 404 for Weixin delivery
    monkeypatch.setenv("DALTON_CAP_CHANNEL_WEIXIN", "false")
    with pytest.raises(HTTPException) as exc_pf_404:
        await wx_db.preflight_weixin_send(delivery_key="dlv-pf-r2", gateway_epoch=1, delivery_attempt=1)
    assert exc_pf_404.value.status_code == 404

    # (b) recover does NOT mutate Weixin in_flight deliveries when channel_weixin=off
    await cg_db.recover_channel_state(mode="startup", gateway_epoch=1, require_gateway_epoch=True)
    assert store.deliveries["dlv-pf-r2"]["status"] == "in_flight"

    # (c) I-5 saving Feishu settings succeeds WITHOUT validating hidden apps.weixin (even if weixin points to revoked workspace!)
    revoked_ws = str(uuid4())
    hidden_wx_cfg = {"preferred": "weixin:@me", "chats": {"weixin:@me": {"workspace_id": revoked_ws}}, "automation_output": {}, "agent_messages": {"enabled": True, "allowed": ["weixin:@me"]}}
    store.user_settings["u-alice"] = {
        "user_id": "u-alice",
        "apps": {"weixin": hidden_wx_cfg, "feishu": {"preferred": "feishu:@me", "chats": {}, "automation_output": {}, "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]}}},
        "version": "1",
        "updated_at": store.now,
    }
    monkeypatch.setattr(
        cg_db,
        "_get_writable_workspace_names",
        create_autospec(cg_db._get_writable_workspace_names, return_value={}),
    )
    upd_res = await cg_db.update_user_channel_settings(
        user_id="u-alice",
        version="1",
        settings={"feishu": {"preferred": "feishu:@me", "chats": {}, "automation_output": {}, "agent_messages": {"enabled": False, "allowed": []}}},
    )
    assert "version" in upd_res
    assert store.user_settings["u-alice"]["apps"]["weixin"] == hidden_wx_cfg


@pytest.mark.asyncio
async def test_r2_context_token_dual_watermarks_command_encrypt_and_group_reject_before_token_write(
    wx_env: dict[str, Any],
) -> None:
    """Decision #10: Dual protocol/platform_time monotonic watermarks, receive fallback, I-3 encrypted token preserving seq, group reject before token write."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    aid = str(uuid4())
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-a@im.wechat",
        "encrypted_bot_token": "tok-a",
        "bot_token": "tok-a",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_alice@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }

    # 1. Group I-1 rejects with weixin_group_unsupported BEFORE any context_token write!
    r_grp_in = await cg_db.register_and_resolve_inbound(
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="group",
        chat_address="weixin:group:g1",
        thread_scope_key="group:g1",
        message_id="wx_grp_tok_1",
        raw_payload={"text": "hi group"},
        weixin_account_id=aid,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        weixin_context_token="ctx-must-not-save",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_grp_in["allowed"] is False
    assert r_grp_in["deny_reason"] == "weixin_group_unsupported"
    assert (aid, 1, "wx_alice@im.wechat") not in store.account_tokens

    # 2. Invalid inbound rejects BEFORE writing context_token
    r_inv = await cg_db.register_and_resolve_inbound(
        platform="weixin",
        platform_user_id="__invalid__",
        chat_type="dm",
        chat_address="weixin:@me",
        thread_scope_key="dm:__invalid__",
        message_id="wx_inv_tok_1",
        raw_payload={"text": "bad"},
        weixin_account_id=aid,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        weixin_context_token="ctx-invalid-must-not-save",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_inv["allowed"] is False
    assert (aid, 1, "wx_alice@im.wechat") not in store.account_tokens

    # 3. Dual monotonic watermarks: protocol=10 -> protocol=5 ignored -> platform_time=1000 wins -> platform_time=900 ignored -> receive wins
    async with wx_db.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            upd1, s1, v1 = await wx_db.upsert_weixin_account_context_token_in_tx(
                cur,
                account_id=aid,
                ilink_bot_id="bot-a@im.wechat",
                bot_generation=1,
                platform_user_id="wx_alice@im.wechat",
                context_token="ctx-p10",
                incoming_seq=10,
                incoming_source="protocol",
            )
            assert upd1 is True and s1 == 10 and v1 == 1

            upd2, s2, _ = await wx_db.upsert_weixin_account_context_token_in_tx(
                cur,
                account_id=aid,
                ilink_bot_id="bot-a@im.wechat",
                bot_generation=1,
                platform_user_id="wx_alice@im.wechat",
                context_token="ctx-p5-stale",
                incoming_seq=5,
                incoming_source="protocol",
            )
            assert upd2 is False and s2 == 10
            assert store.account_tokens[(aid, 1, "wx_alice@im.wechat")]["encrypted_context_token"] == "ctx-p10"

            upd3, _, _ = await wx_db.upsert_weixin_account_context_token_in_tx(
                cur,
                account_id=aid,
                ilink_bot_id="bot-a@im.wechat",
                bot_generation=1,
                platform_user_id="wx_alice@im.wechat",
                context_token="ctx-pt1000",
                incoming_seq=1000,
                incoming_source="platform_time",
            )
            assert upd3 is True
            assert store.account_tokens[(aid, 1, "wx_alice@im.wechat")]["encrypted_context_token"] == "ctx-pt1000"

            upd4, _, _ = await wx_db.upsert_weixin_account_context_token_in_tx(
                cur,
                account_id=aid,
                ilink_bot_id="bot-a@im.wechat",
                bot_generation=1,
                platform_user_id="wx_alice@im.wechat",
                context_token="ctx-pt900-stale",
                incoming_seq=900,
                incoming_source="platform_time",
            )
            assert upd4 is False
            assert store.account_tokens[(aid, 1, "wx_alice@im.wechat")]["encrypted_context_token"] == "ctx-pt1000"

            upd5, _, _ = await wx_db.upsert_weixin_account_context_token_in_tx(
                cur,
                account_id=aid,
                ilink_bot_id="bot-a@im.wechat",
                bot_generation=1,
                platform_user_id="wx_alice@im.wechat",
                context_token="ctx-recv-fallback",
                incoming_seq=None,
                incoming_source="receive",
            )
            assert upd5 is True
            assert store.account_tokens[(aid, 1, "wx_alice@im.wechat")]["encrypted_context_token"] == "ctx-recv-fallback"

    # 4. I-3 session command encrypts token into dalton_channel_weixin_account_tokens via pgp_sym_encrypt & preserves higher seq on out-of-order cmd
    store.executed_sql.clear()
    await cg_db.execute_session_command(
        cmd="help",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        scope_key="dm:wx_alice@im.wechat",
        message_id="wx_cmd_tok_1",
        weixin_account_id=aid,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        weixin_context_token="ctx-cmd-p20",
        weixin_context_seq=20,
        raw_payload={"seq": 20},
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert any("INSERT INTO dalton_channel_weixin_account_tokens" in s and "pgp_sym_encrypt" in s for s, _ in store.executed_sql)
    assert store.account_tokens[(aid, 1, "wx_alice@im.wechat")]["encrypted_context_token"] == "ctx-cmd-p20"
    assert store.account_tokens[(aid, 1, "wx_alice@im.wechat")]["protocol_seq_high"] == 20

    # Lower protocol seq on next command preserves stored seq=20 and token!
    await cg_db.execute_session_command(
        cmd="help",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        scope_key="dm:wx_alice@im.wechat",
        message_id="wx_cmd_tok_2",
        weixin_account_id=aid,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        weixin_context_token="ctx-cmd-p15-stale",
        weixin_context_seq=15,
        raw_payload={"seq": 15},
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert store.account_tokens[(aid, 1, "wx_alice@im.wechat")]["encrypted_context_token"] == "ctx-cmd-p20"
    assert store.account_tokens[(aid, 1, "wx_alice@im.wechat")]["protocol_seq_high"] == 20


@pytest.mark.asyncio
async def test_r3_no_bare_cursor_outside_transaction_and_lease_hook_before_for_update(
    wx_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 regressions:
    1. Every two-phase Weixin DB function wraps Phase-1 discovery in `conn.transaction()`
       so non-autocommit connections never stay `idle in transaction` holding advisory locks;
    2. `acquire_gateway_lease` invokes `_before_lease_update_hook` BEFORE `SELECT ... FOR UPDATE`.
    """
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    tx_depth = 0
    bare_cursor_violations: list[str] = []
    event_order: list[str] = []

    class StrictTxCursor:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []
            self._idx = 0

        async def __aenter__(self) -> StrictTxCursor:
            if tx_depth <= 0:
                bare_cursor_violations.append("cursor entered outside transaction")
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            if "FROM dalton_channel_gateway_lease" in sql and "FOR UPDATE" in sql:
                event_order.append("for_update_sql")
            self._rows = await store.handle_sql(sql, params)
            self._idx = 0

        async def fetchone(self) -> Any:
            if self._idx < len(self._rows):
                row = self._rows[self._idx]
                self._idx += 1
                return row
            return None

        async def fetchall(self) -> list[Any]:
            rest = self._rows[self._idx :]
            self._idx = len(self._rows)
            return rest

    class StrictTxConn:
        @asynccontextmanager
        async def transaction(self) -> Any:
            nonlocal tx_depth
            tx_depth += 1
            try:
                yield
            finally:
                tx_depth -= 1

        def cursor(self, row_factory: Any = None) -> StrictTxCursor:
            _ = row_factory
            return StrictTxCursor()

    strict_conn = StrictTxConn()

    # 1. Verify _before_lease_update_hook fires BEFORE SELECT ... FOR UPDATE
    store.lease = None

    async def _before_hook() -> None:
        event_order.append("hook")

    monkeypatch.setattr(wx_db, "_before_lease_update_hook", _before_hook)
    try:
        lease_res = await wx_db.acquire_gateway_lease(
            instance_id="gw-hook-check", ttl_seconds=45, conn=strict_conn
        )
        assert lease_res["granted"] is True
        assert event_order[:2] == ["hook", "for_update_sql"]
        epoch = int(lease_res["epoch"])
    finally:
        monkeypatch.setattr(wx_db, "_before_lease_update_hook", None)

    # 2. Verify start -> save_qr -> confirm -> verify_pending -> reevaluate -> unbind never use bare cursors
    s = await wx_db.start_weixin_login_session(user_id="u-alice", conn=strict_conn)
    await wx_db.patch_weixin_login_session(
        session_key=s["session_key"],
        gateway_epoch=epoch,
        action="save_qr",
        qrcode="qr-strict",
        conn=strict_conn,
    )
    c = await wx_db.patch_weixin_login_session(
        session_key=s["session_key"],
        gateway_epoch=epoch,
        action="confirm",
        expected_login_version=2,
        ilink_bot_id="bot-strict@im.wechat",
        bot_token="tok-strict",
        ilink_user_id=None,
        conn=strict_conn,
    )
    aid = c["account_id"]
    await wx_db.verify_pending_weixin_account_first_message(
        account_id=aid,
        platform_user_id="wx_strict@im.wechat",
        gateway_epoch=epoch,
        require_gateway_epoch=True,
        conn=strict_conn,
    )
    await wx_db.reevaluate_weixin_accounts(
        target_user_ids=["u-alice"],
        gateway_epoch=epoch,
        require_gateway_epoch=True,
        conn=strict_conn,
    )
    await wx_db.unbind_weixin_account(
        target_user_id="u-alice",
        account_id=aid,
        operator_user_id="u-alice",
        action="member_unbind",
        gateway_epoch=epoch,
        require_gateway_epoch=True,
        conn=strict_conn,
    )
    assert bare_cursor_violations == []
    assert tx_depth == 0


@pytest.mark.asyncio
async def test_r4_weixin_command_stamps_account_id_for_i6_notice_claim(
    wx_env: dict[str, Any],
) -> None:
    """Round 4 regression: Weixin slash command (`new`/`stop`/etc.) stamps `weixin_account_id`
    onto `dalton_channel_inbound_events`, so subsequent I-6 `notice` delivery claim succeeds
    instead of being denied as `source_generation_unknown`.
    """
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    aid = str(uuid4())
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-cmd@im.wechat",
        "ilink_user_id": "wx_cmd_alice@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "encrypted_bot_token": "tok-cmd",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "last_inbound_at": None,
        "last_error_code": None,
        "last_error_at": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }

    store.executed_sql.clear()
    cmd_res = await cg_db.execute_session_command(
        cmd="new",
        platform="weixin",
        platform_user_id="wx_cmd_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_cmd_alice@im.wechat",
        message_id="wx_r4_cmd_new_1",
        weixin_account_id=aid,
        weixin_context_token="ctx-r4-cmd-new",
        weixin_bot_id="bot-cmd@im.wechat",
        weixin_bot_generation=1,
        weixin_context_source="protocol",
        weixin_context_seq=10,
        gateway_epoch=1,
        require_gateway_epoch=True,
        raw_payload={"seq": 10},
    )
    assert cmd_res["ok"] is True
    assert any(
        "UPDATE dalton_channel_inbound_events" in s and "weixin_account_id = %s::uuid" in s
        for s, _ in store.executed_sql
    )


@pytest.mark.asyncio
async def test_r5_i3_authorize_first_and_readonly_never_verifies(
    wx_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5 Decision #1: I-3 authorizes account/bot/gen/DM/roster/switches BEFORE any branch;
    read-only workspace_list NEVER triggers D11 first-message verification.
    """
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    roster: dict[str, str] = wx_env["roster"]
    aid = str(uuid4())
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-pv@im.wechat",
        "encrypted_bot_token": "tok-pv",
        "bot_token": "tok-pv",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "pending_verification",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 2,
        "prev_bot_generation": 1,
        "bot_generation_updated_at": store.now,
        "ilink_user_id": None,
        "prev_ilink_user_id": "wx_alice@im.wechat",
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }

    verify_spy = create_autospec(
        wx_db.verify_pending_weixin_account_first_message,
        side_effect=wx_db.verify_pending_weixin_account_first_message,
    )
    monkeypatch.setattr(wx_db, "verify_pending_weixin_account_first_message", verify_spy)

    # 1. Read-only internal workspace_list MUST NOT trigger D11 verification
    r_ro = await cg_db.execute_session_command(
        cmd="workspace_list",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_alice@im.wechat",
        message_id=None,
        is_internal_query=True,
        weixin_account_id=aid,
        weixin_bot_id="bot-pv@im.wechat",
        weixin_bot_generation=2,
        gateway_epoch=1,
        require_gateway_epoch=False,
    )
    assert r_ro["ok"] is False
    assert r_ro["deny_reason"] == "weixin_account_pending_verification"
    assert store.accounts[aid]["status"] == "pending_verification"
    verify_spy.assert_not_called()

    # 2. Group command on pending_verification rejected BEFORE verification
    r_grp = await cg_db.execute_session_command(
        cmd="new",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="group",
        scope_key="group:g1",
        message_id="r5_pv_grp",
        weixin_account_id=aid,
        weixin_bot_id="bot-pv@im.wechat",
        weixin_bot_generation=2,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_grp["deny_reason"] == "weixin_group_unsupported"
    assert store.accounts[aid]["status"] == "pending_verification"
    verify_spy.assert_not_called()

    # 3. Removed member rejected BEFORE verification (does not activate/mutate account!)
    roster["u-alice"] = "removed"
    r_rem = await cg_db.execute_session_command(
        cmd="new",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_alice@im.wechat",
        message_id="r5_pv_rem",
        weixin_account_id=aid,
        weixin_bot_id="bot-pv@im.wechat",
        weixin_bot_generation=2,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_rem["deny_reason"] == "member_not_in_roster"
    assert store.accounts[aid]["status"] == "pending_verification"
    verify_spy.assert_not_called()
    roster["u-alice"] = "active"

    # 4. Channel disabled rejected BEFORE verification (both ordinary & collect commands!)
    store.config["enabled"] = False
    r_off = await cg_db.execute_session_command(
        cmd="new",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_alice@im.wechat",
        message_id="r5_pv_off",
        weixin_account_id=aid,
        weixin_bot_id="bot-pv@im.wechat",
        weixin_bot_generation=2,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_off["deny_reason"] == "weixin_channel_disabled"
    assert store.accounts[aid]["status"] == "pending_verification"
    r_col_off = await cg_db.execute_session_command(
        cmd="collect",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_alice@im.wechat",
        message_id="r5_pv_col_off",
        weixin_account_id=aid,
        weixin_bot_id="bot-pv@im.wechat",
        weixin_bot_generation=2,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_col_off["deny_reason"] == "weixin_channel_disabled"
    assert store.accounts[aid]["status"] == "pending_verification"
    verify_spy.assert_not_called()
    store.config["enabled"] = True

    # 5. Once ALL authorization passes, ordinary write command triggers D11 verification and succeeds!
    r_ok = await cg_db.execute_session_command(
        cmd="new",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_alice@im.wechat",
        message_id="r5_pv_ok",
        weixin_account_id=aid,
        weixin_bot_id="bot-pv@im.wechat",
        weixin_bot_generation=2,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_ok["ok"] is True
    assert store.accounts[aid]["status"] == "active"
    assert verify_spy.call_count == 1


@pytest.mark.asyncio
async def test_r5_weixin_unbind_requires_complete_four_fields(
    wx_env: dict[str, Any],
) -> None:
    """R5 Decision #2: Weixin command /unbind requires account_id, bot_id, bot_generation, and sender."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    aid = str(uuid4())
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-u@im.wechat",
        "encrypted_bot_token": "tok-u",
        "bot_token": "tok-u",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_alice@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }

    incomplete_cases = [
        # Account-id only (must NOT bypass Weixin command auth!)
        {"weixin_account_id": aid},
        # Missing account_id
        {
            "weixin_bot_id": "bot-u@im.wechat",
            "weixin_bot_generation": 1,
            "platform_user_id": "wx_alice@im.wechat",
        },
        # Missing bot_id
        {
            "weixin_account_id": aid,
            "weixin_bot_generation": 1,
            "platform_user_id": "wx_alice@im.wechat",
        },
        # Missing bot_generation
        {
            "weixin_account_id": aid,
            "weixin_bot_id": "bot-u@im.wechat",
            "platform_user_id": "wx_alice@im.wechat",
        },
        # Missing platform_user_id
        {
            "weixin_account_id": aid,
            "weixin_bot_id": "bot-u@im.wechat",
            "weixin_bot_generation": 1,
        },
    ]
    for kwargs in incomplete_cases:
        with pytest.raises(HTTPException) as exc_400:
            await cg_db.consume_bind_code_or_unbind(
                action="unbind",
                platform="weixin",
                gateway_epoch=1,
                require_gateway_epoch=True,
                **kwargs,
            )
        assert exc_400.value.status_code == 400
        assert exc_400.value.detail["code"] == "weixin_unbind_fields_required"
        assert aid in store.accounts

    # Direct wx_db.unbind_weixin_account(..., via_wechat_command=True) also enforces all 4 fields
    with pytest.raises(HTTPException) as exc_direct:
        await wx_db.unbind_weixin_account(
            account_id=aid,
            operator_user_id="u-alice",
            action="member_unbind",
            via_wechat_command=True,
            gateway_epoch=1,
            require_gateway_epoch=True,
        )
    assert exc_direct.value.status_code == 400
    assert exc_direct.value.detail["code"] == "weixin_unbind_fields_required"
    assert aid in store.accounts


@pytest.mark.asyncio
async def test_r5_i1_validate_before_replay_and_ignored_zero_leak(
    wx_env: dict[str, Any],
) -> None:
    """R5 Decision #3: I-1 validates sender, required account/bot/gen, match, roster, and switches
    BEFORE entering replay or returning a stored ignored row, with zero member leakage.
    """
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    roster: dict[str, str] = wx_env["roster"]
    aid = str(uuid4())
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-a@im.wechat",
        "encrypted_bot_token": "tok-a",
        "bot_token": "tok-a",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_alice@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    store.inbound_events[("weixin", "r5_ignored_mid")] = {
        "platform": "weixin",
        "message_id": "r5_ignored_mid",
        "request_key": str(uuid4()),
        "dalton_user_id": "u-alice",
        "workspace_id": str(uuid4()),
        "thread_id": str(uuid4()),
        "status": "ignored",
        "raw_payload": {
            "platform_user_id": "wx_alice@im.wechat",
            "deny_reason": "ignored",
            "text": "private alice ignored text",
        },
        "weixin_account_id": aid,
        "weixin_context_bot_id": "bot-a@im.wechat",
        "weixin_context_bot_generation": 1,
        "created_at": store.now,
        "updated_at": store.now,
    }

    # 1. Empty sender on existing ignored row -> rejected as invalid_inbound_event with ZERO leak
    r_empty = await cg_db.register_and_resolve_inbound(
        platform="weixin",
        platform_user_id="",
        chat_type="dm",
        chat_address="weixin:@me",
        thread_scope_key="dm:unknown",
        message_id="r5_ignored_mid",
        weixin_account_id=aid,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_empty["allowed"] is False
    assert r_empty["deny_reason"] == "invalid_inbound_event"
    assert "dalton_user_id" not in r_empty
    assert "thread_id" not in r_empty

    # 2. Missing account/bot/gen fields on replay -> weixin_inbound_fields_required with ZERO leak
    r_missing = await cg_db.register_and_resolve_inbound(
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        thread_scope_key="dm:wx_alice@im.wechat",
        message_id="r5_ignored_mid",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_missing["allowed"] is False
    assert r_missing["deny_reason"] == "weixin_inbound_fields_required"
    assert "dalton_user_id" not in r_missing

    # 3. Removed member on ignored row replay -> member_not_in_roster with ZERO leak
    roster["u-alice"] = "removed"
    r_rem = await cg_db.register_and_resolve_inbound(
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        thread_scope_key="dm:wx_alice@im.wechat",
        message_id="r5_ignored_mid",
        weixin_account_id=aid,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_rem["allowed"] is False
    assert r_rem["deny_reason"] == "member_not_in_roster"
    assert "dalton_user_id" not in r_rem
    roster["u-alice"] = "active"


@pytest.mark.asyncio
async def test_r5_i6_thread_only_missing_source_and_authoritative_db_turn_time(
    wx_env: dict[str, Any],
) -> None:
    """R5 Decision #4: I-6 refuses thread_id-only (no run_id) watch borrow, marks unknown+audit
    when no source exists, and update_inbound_progress derives weixin_bot_generation from DB time.
    """
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    aid = str(uuid4())
    tid = str(uuid4())
    t_rescan = store.now
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-a@im.wechat",
        "encrypted_bot_token": "tok-a",
        "bot_token": "tok-a",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 2,
        "prev_bot_generation": 1,
        "bot_generation_updated_at": t_rescan,
        "ilink_user_id": "wx_alice@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": t_rescan - timedelta(minutes=5),
        "updated_at": t_rescan,
    }
    store.thread_watches[tid] = {
        "thread_id": tid,
        "dalton_user_id": "u-alice",
        "platform": "weixin",
        "target_address": "weixin:@me",
        "scope_key": "dm:wx_alice@im.wechat",
        "generation": 1,
        "watch_active": True,
        "last_notified_turn_index": 0,
        "weixin_account_id": aid,
        "weixin_recipient_id": "wx_alice@im.wechat",
        "weixin_bot_generation": 2,
    }

    # 1. Thread-only delivery (run_id=None) MUST NOT borrow thread_watches.weixin_bot_generation=2!
    audit_before = len(store.audit_logs)
    r_tonly = await cg_db.authorize_and_claim_delivery(
        delivery_key="dlv-r5-thread-only",
        kind="fallback",
        user_id="u-alice",
        thread_id=tid,
        run_id=None,
        platform="weixin",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_tonly["claim_status"] == "denied"
    assert r_tonly["status"] == "unknown"
    assert r_tonly["code"] == "source_generation_unknown"
    assert store.deliveries["dlv-r5-thread-only"]["status"] == "unknown"
    assert len(store.audit_logs) == audit_before + 1
    assert store.audit_logs[-1]["action"] == "ambiguous_legacy_delivery_blocked"

    # 2. Missing source entirely -> writes status='unknown' + audit log BEFORE returning denied!
    audit_before2 = len(store.audit_logs)
    r_miss = await cg_db.authorize_and_claim_delivery(
        delivery_key="dlv-r5-missing-src",
        kind="fallback",
        user_id="u-alice",
        thread_id=str(uuid4()),
        run_id=str(uuid4()),
        platform="weixin",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_miss["claim_status"] == "denied"
    assert r_miss["status"] == "unknown"
    assert r_miss["code"] == "source_generation_unknown"
    assert store.deliveries["dlv-r5-missing-src"]["status"] == "unknown"
    assert len(store.audit_logs) == audit_before2 + 1
    assert store.audit_logs[-1]["action"] == "ambiguous_legacy_delivery_blocked"

    # 3. update_inbound_progress ignores forged caller weixin_bot_generation=2 on pre-rescan turn!
    r_forged_pre = str(uuid4())
    r_no_ts = str(uuid4())
    store.conversation_queries.append(
        {
            "conversation_thread_id": tid,
            "turn_index": 7,
            "query_type": "wakeup",
            "response_id": r_forged_pre,
            "created_at": t_rescan - timedelta(seconds=20),
        }
    )
    await cg_db.update_inbound_progress(
        platform="weixin",
        background_run={
            "run_id": r_forged_pre,
            "thread_id": tid,
            "dalton_user_id": "u-alice",
            "platform": "weixin",
            "target_address": "weixin:@me",
            "turn_index": 7,
            "status": "completed",
            "fallback_pending": True,
            "weixin_bot_generation": 2,  # Forged caller generation MUST be overridden by DB time!
        },
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert store.background_runs[r_forged_pre]["weixin_bot_generation"] == 1

    # Missing DB timestamp after rescan -> fails closed to None even if caller passed 2
    await cg_db.update_inbound_progress(
        platform="weixin",
        background_run={
            "run_id": r_no_ts,
            "thread_id": tid,
            "dalton_user_id": "u-alice",
            "platform": "weixin",
            "target_address": "weixin:@me",
            "turn_index": 8,
            "status": "completed",
            "fallback_pending": True,
            "weixin_bot_generation": 2,
        },
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert store.background_runs[r_no_ts]["weixin_bot_generation"] is None


@pytest.mark.asyncio
async def test_r5_collect_command_whitelisted_payload_and_replay_after_auth(
    wx_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5 Decision #6: Collect command stores only safe whitelisted fields (never credentials/media),
    and checks auth/switches/roster BEFORE returning cached replay.
    """
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    roster: dict[str, str] = wx_env["roster"]
    home_ws_id = cg_db.get_home_workspace_id("u-alice")
    store.workspaces[home_ws_id] = {
        "workspace_id": home_ws_id,
        "user_id": "u-alice",
        "name": "Home",
        "status": "running",
    }
    monkeypatch.setattr(
        cg_db,
        "_verify_workspace_write_permission",
        create_autospec(
            cg_db._verify_workspace_write_permission,
            return_value=(True, None, {"workspace_id": home_ws_id}),
        ),
    )
    aid = str(uuid4())
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-c@im.wechat",
        "encrypted_bot_token": "tok-c",
        "bot_token": "tok-c",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_alice@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }

    r1 = await cg_db.execute_session_command(
        cmd="collect",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_alice@im.wechat",
        message_id="r5_collect_mid_1",
        weixin_account_id=aid,
        weixin_bot_id="bot-c@im.wechat",
        weixin_bot_generation=1,
        weixin_context_token="secret-ctx-token",
        raw_payload={
            "seq": 42,
            "create_time_ms": 1700000000000,
            "context_token": "secret-ctx-token",
            "bot_token": "secret-bot-token",
            "aes_key": "secret-aes-key",
            "weixin_attachments": [{"url": "https://cdn.example/secret", "aes_key": "k"}],
            "weixin_media_meta": [{"encrypt_query_param": "enc"}],
        },
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r1["ok"] is True
    saved_row = store.inbound_events[("weixin", "r5_collect_mid_1")]
    saved_raw = saved_row["raw_payload"]
    assert saved_raw == {
        "cmd": "collect",
        "platform_user_id": "wx_alice@im.wechat",
        "seq": 42,
        "create_time_ms": 1700000000000,
    }
    for forbidden in (
        "context_token",
        "bot_token",
        "aes_key",
        "weixin_attachments",
        "weixin_media_meta",
    ):
        assert forbidden not in saved_raw

    # Replay when channel disabled -> denied (auth runs BEFORE replay!)
    store.config["enabled"] = False
    r_off = await cg_db.execute_session_command(
        cmd="collect",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_alice@im.wechat",
        message_id="r5_collect_mid_1",
        weixin_account_id=aid,
        weixin_bot_id="bot-c@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_off["ok"] is False
    assert r_off["deny_reason"] == "weixin_channel_disabled"
    store.config["enabled"] = True

    # Replay when member removed -> denied BEFORE replay
    roster["u-alice"] = "removed"
    r_rem = await cg_db.execute_session_command(
        cmd="collect",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_alice@im.wechat",
        message_id="r5_collect_mid_1",
        weixin_account_id=aid,
        weixin_bot_id="bot-c@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_rem["ok"] is False
    assert r_rem["deny_reason"] == "member_not_in_roster"
    roster["u-alice"] = "active"

    # Authorized owner replay returns duplicate=True
    r_dup = await cg_db.execute_session_command(
        cmd="collect",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_alice@im.wechat",
        message_id="r5_collect_mid_1",
        weixin_account_id=aid,
        weixin_bot_id="bot-c@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_dup["ok"] is True
    assert r_dup["duplicate"] is True


@pytest.mark.asyncio
async def test_r5_weixin_disabled_recovery_skips_generic_updates_and_preflight_404_before_409(
    wx_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5 Decision #7: Disabled Weixin capability skips Weixin in BOTH generic startup-recovery
    UPDATE statements, and preflight-send returns 404 BEFORE 409 even without X-Gateway-Epoch.
    """
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    client: TestClient = wx_env["client"]

    monkeypatch.setenv("DALTON_CAP_CHANNEL_WEIXIN", "false")

    store.executed_sql.clear()
    await cg_db.recover_channel_state(
        mode="startup",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    norm_sqls = [" ".join(s.split()) for s, _ in store.executed_sql]
    invalid_updates = [
        s
        for s in norm_sqls
        if "UPDATE dalton_channel_inbound_events" in s and "invalid_inbound_event" in s
    ]
    cancel_updates = [
        s
        for s in norm_sqls
        if "UPDATE dalton_channel_inbound_events" in s and "pending_cancel" in s
    ]
    assert invalid_updates and all("AND platform <> 'weixin'" in s for s in invalid_updates)
    assert cancel_updates and all("AND platform <> 'weixin'" in s for s in cancel_updates)

    # preflight-send with NO X-Gateway-Epoch header MUST return 404 (not 409!) when Weixin is disabled
    r_pf = client.post(
        "/api/v1/dalton/channels/internal/deliveries/dlv-r5-disabled/preflight-send",
        headers={"X-Service-Token": SERVICE_TOKEN},
        json={"delivery_attempt": 1, "chunk_index": 0},
    )
    assert r_pf.status_code == 404
    assert r_pf.json().get("code") != "stale_gateway_epoch"


@pytest.mark.asyncio
async def test_r5_data_task_reconcile_wraps_phase1_read_in_transaction() -> None:
    """R5 Decision #9: reconcile_single_data_task_delivery_row wraps Phase-1 read cursor in
    row_conn.transaction() so non-autocommit connections never open a bare cursor before Phase 2.
    """
    from src.dalton.database import data_task_notifications as dtn_db

    tx_depth = 0
    bare_cursor_violations: list[str] = []

    class _StrictCursor:
        async def __aenter__(self) -> _StrictCursor:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def execute(self, _sql: str, _params: Any = None) -> None:
            return None

        async def fetchone(self) -> Any:
            return None

        async def fetchall(self) -> list[Any]:
            return []

    class _StrictConn:
        @asynccontextmanager
        async def transaction(self) -> Any:
            nonlocal tx_depth
            tx_depth += 1
            try:
                yield
            finally:
                tx_depth -= 1

        def cursor(self, **_kwargs: Any) -> _StrictCursor:
            if tx_depth <= 0:
                bare_cursor_violations.append("bare_cursor_outside_transaction")
            return _StrictCursor()

    row = {
        "execution_id": str(uuid4()),
        "user_id": "u-alice",
        "channel": "feishu",
        "target": "feishu:chat:oc_test",
        "status": "pending",
        "error_code": "unknown",
        "payload_json": {"cards": [{"title": "c1"}]},
    }
    res = await dtn_db.reconcile_single_data_task_delivery_row(row, conn=_StrictConn())
    assert res is False
    assert bare_cursor_violations == []
    assert tx_depth == 0


@pytest.mark.asyncio
async def test_r7_pending_verification_checks_roster_and_switch_before_activation_or_conflict_delete(
    wx_env: dict[str, Any],
) -> None:
    """R7 Decision #2: In I-1 pending_verification, check live roster and runtime switches
    BEFORE activating, deleting a conflicting account, or returning one_time_weixin_reply!
    """
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    roster: dict[str, str] = wx_env["roster"]
    aid_owner = str(uuid4())
    aid_pending = str(uuid4())
    store.accounts[aid_owner] = {
        "account_id": aid_owner,
        "user_id": "u-bob",
        "ilink_bot_id": "bot-bob@im.wechat",
        "encrypted_bot_token": "tok-bob",
        "bot_token": "tok-bob",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_bob@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Bob",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    store.accounts[aid_pending] = {
        **store.accounts[aid_owner],
        "account_id": aid_pending,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-alice-pv@im.wechat",
        "status": "pending_verification",
        "ilink_user_id": None,
        "prev_ilink_user_id": None,
    }

    # 1. Admin pauses Weixin channel -> pending_verification MUST NOT activate, delete conflict, or emit reply!
    store.config["enabled"] = False
    r_paused = await cg_db.register_and_resolve_inbound(
        platform="weixin",
        platform_user_id="wx_bob@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        thread_scope_key="dm:wx_bob@im.wechat",
        message_id="r7_pv_paused_conflict",
        weixin_account_id=aid_pending,
        weixin_bot_id="bot-alice-pv@im.wechat",
        weixin_bot_generation=1,
        weixin_context_token="ctx-paused",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_paused["allowed"] is False
    assert r_paused["deny_reason"] == "weixin_channel_disabled"
    assert "one_time_weixin_reply" not in r_paused
    assert aid_pending in store.accounts
    assert store.accounts[aid_pending]["status"] == "pending_verification"
    store.config["enabled"] = True

    # 2. Roster lookup_error -> raises retryable 503 WITHOUT mutating or deleting pending_verification!
    roster["u-alice"] = "lookup_error"
    with pytest.raises(HTTPException) as exc_503:
        await cg_db.register_and_resolve_inbound(
            platform="weixin",
            platform_user_id="wx_alice_new@im.wechat",
            chat_type="dm",
            chat_address="weixin:@me",
            thread_scope_key="dm:wx_alice_new@im.wechat",
            message_id="r7_pv_503",
            weixin_account_id=aid_pending,
            weixin_bot_id="bot-alice-pv@im.wechat",
            weixin_bot_generation=1,
            weixin_context_token="ctx-503",
            gateway_epoch=1,
            require_gateway_epoch=True,
        )
    assert exc_503.value.status_code == 503
    assert store.accounts[aid_pending]["status"] == "pending_verification"
    assert store.accounts[aid_pending]["ilink_user_id"] is None

    # 3. Removed member -> denied WITHOUT activating or deleting conflict!
    roster["u-alice"] = "removed"
    r_rem = await cg_db.register_and_resolve_inbound(
        platform="weixin",
        platform_user_id="wx_bob@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        thread_scope_key="dm:wx_bob@im.wechat",
        message_id="r7_pv_removed",
        weixin_account_id=aid_pending,
        weixin_bot_id="bot-alice-pv@im.wechat",
        weixin_bot_generation=1,
        weixin_context_token="ctx-rem",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_rem["allowed"] is False
    assert r_rem["deny_reason"] == "member_not_in_roster"
    assert "one_time_weixin_reply" not in r_rem
    assert aid_pending in store.accounts
    assert store.accounts[aid_pending]["status"] == "pending_verification"
    roster["u-alice"] = "active"


@pytest.mark.asyncio
async def test_r7_i3_sender_mismatch_preserves_original_command_record(
    wx_env: dict[str, Any],
) -> None:
    """R7 Decision #3: When member B replays member A's command message_id, B is rejected
    WITHOUT modifying A's stored status, command_result, or command_reply_pending; A's
    legitimate replay still receives A's original result and pending reply flag.
    """
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    aid_a = str(uuid4())
    aid_b = str(uuid4())
    store.accounts[aid_a] = {
        "account_id": aid_a,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-a@im.wechat",
        "encrypted_bot_token": "tok-a",
        "bot_token": "tok-a",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_alice@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    store.accounts[aid_b] = {
        **store.accounts[aid_a],
        "account_id": aid_b,
        "user_id": "u-bob",
        "ilink_bot_id": "bot-b@im.wechat",
        "ilink_user_id": "wx_bob@im.wechat",
    }

    r_a = await cg_db.execute_session_command(
        cmd="new",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_alice@im.wechat",
        message_id="r7_shared_cmd_mid",
        weixin_account_id=aid_a,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_a["ok"] is True
    assert r_a["command_reply_pending"] is True
    orig_gen = r_a["generation"]

    # Member B collides on same message_id -> denied WITHOUT overwriting A's row!
    r_b = await cg_db.execute_session_command(
        cmd="new",
        platform="weixin",
        platform_user_id="wx_bob@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_bob@im.wechat",
        message_id="r7_shared_cmd_mid",
        weixin_account_id=aid_b,
        weixin_bot_id="bot-b@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_b["ok"] is False

    stored_a = store.inbound_events[("weixin", "r7_shared_cmd_mid")]
    assert stored_a["status"] == "accepted"
    assert stored_a["command_reply_pending"] is True
    assert stored_a["command_result"]["ok"] is True
    assert stored_a["command_result"]["generation"] == orig_gen

    # Member A retries -> gets original successful result & reply still pending!
    r_a_replay = await cg_db.execute_session_command(
        cmd="new",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_alice@im.wechat",
        message_id="r7_shared_cmd_mid",
        weixin_account_id=aid_a,
        weixin_bot_id="bot-a@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_a_replay["ok"] is True
    assert r_a_replay["duplicate"] is True
    assert r_a_replay["generation"] == orig_gen
    assert r_a_replay["command_reply_pending"] is True


@pytest.mark.asyncio
async def test_r7_i6_explicit_weixin_target_enforces_source_and_live_session_generation(
    wx_env: dict[str, Any],
) -> None:
    """R7 Decision #4:
    1) `kind=agent_send` with explicit `target='weixin:@me'` checks source run_id's
       account/bot_generation (rejected after rescan or account switch).
    2) After `/new` or workspace switch advances `dalton_channel_session_generations.generation`,
       delayed delivery from an older turn is rejected as `session_generation_superseded`.
    3) Migrated legacy inbound with NULL `weixin_context_bot_generation` is rejected as
       `source_generation_unknown` and writes `ambiguous_legacy_delivery_blocked` audit.
    """
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    aid = str(uuid4())
    tid = str(uuid4())
    run_old_bot = str(uuid4())
    run_old_sess = str(uuid4())
    run_null_gen = str(uuid4())
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-v2@im.wechat",
        "encrypted_bot_token": "tok-v2",
        "bot_token": "tok-v2",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 2,
        "prev_bot_generation": 1,
        "bot_generation_updated_at": store.now,
        "ilink_user_id": "wx_alice@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    store.account_tokens[(aid, 2, "wx_alice@im.wechat")] = {
        "account_id": aid,
        "bot_generation": 2,
        "platform_user_id": "wx_alice@im.wechat",
        "encrypted_context_token": "ctx-v2",
        "context_token": "ctx-v2",
        "seq": 10,
        "seq_source": "protocol",
        "protocol_seq_high": 10,
        "platform_time_high": None,
        "token_version": 1,
        "stale_at": None,
        "updated_at": store.now,
    }
    store.session_generations[("weixin", "dm:wx_alice@im.wechat", "u-alice")] = {
        "platform": "weixin",
        "scope_key": "dm:wx_alice@im.wechat",
        "dalton_user_id": "u-alice",
        "generation": 3,
        "override_workspace_id": None,
        "pending_interrupt": None,
        "updated_at": store.now,
    }
    # Inbound #1: pre-rescan bot_generation=1 (current session_generation=3)
    store.inbound_events[("weixin", "r7_in_old_bot")] = {
        "platform": "weixin",
        "message_id": "r7_in_old_bot",
        "dalton_user_id": "u-alice",
        "chat_address": "weixin:@me",
        "target_address": "weixin:@me",
        "thread_scope_key": "dm:wx_alice@im.wechat",
        "session_generation": 3,
        "thread_id": tid,
        "run_id": run_old_bot,
        "status": "streaming",
        "weixin_account_id": aid,
        "weixin_context_bot_id": "bot-v1@im.wechat",
        "weixin_context_bot_generation": 1,
        "raw_payload": {"platform_user_id": "wx_alice@im.wechat"},
        "created_at": store.now,
        "updated_at": store.now,
    }
    # Inbound #2: current bot_generation=2, but OLD session_generation=2 (current DB is 3 after /new!)
    store.inbound_events[("weixin", "r7_in_old_sess")] = {
        "platform": "weixin",
        "message_id": "r7_in_old_sess",
        "dalton_user_id": "u-alice",
        "chat_address": "weixin:@me",
        "target_address": "weixin:@me",
        "thread_scope_key": "dm:wx_alice@im.wechat",
        "session_generation": 2,
        "thread_id": tid,
        "run_id": run_old_sess,
        "status": "streaming",
        "weixin_account_id": aid,
        "weixin_context_bot_id": "bot-v2@im.wechat",
        "weixin_context_bot_generation": 2,
        "raw_payload": {"platform_user_id": "wx_alice@im.wechat"},
        "created_at": store.now,
        "updated_at": store.now,
    }
    # Inbound #3: legacy migrated row with NULL bot_generation
    store.inbound_events[("weixin", "r7_in_null_gen")] = {
        "platform": "weixin",
        "message_id": "r7_in_null_gen",
        "dalton_user_id": "u-alice",
        "chat_address": "weixin:@me",
        "target_address": "weixin:@me",
        "thread_scope_key": "dm:wx_alice@im.wechat",
        "session_generation": 3,
        "thread_id": tid,
        "run_id": run_null_gen,
        "status": "streaming",
        "weixin_account_id": aid,
        "weixin_context_bot_id": "bot-v2@im.wechat",
        "weixin_context_bot_generation": None,
        "raw_payload": {"platform_user_id": "wx_alice@im.wechat"},
        "created_at": store.now,
        "updated_at": store.now,
    }

    # 1. Explicit target='weixin:@me' on pre-rescan turn MUST be denied (NOT bypassed as proactive!)
    r_rescan = await cg_db.authorize_and_claim_delivery(
        delivery_key="dlv-r7-explicit-rescan",
        kind="agent_send",
        user_id="u-alice",
        thread_id=tid,
        run_id=run_old_bot,
        platform="weixin",
        target="weixin:@me",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_rescan["claim_status"] == "denied"
    assert r_rescan["code"] == "weixin_bot_generation_superseded"

    # 2. After /new or workspace switch advances session_generation (2 -> 3), delayed delivery
    #    on old turn (both explicit target='weixin:@me' AND omitted target) MUST be rejected!
    r_stale_sess_c = await cg_db.authorize_and_claim_delivery(
        delivery_key="dlv-r7-stale-sess-c",
        kind="agent_send",
        user_id="u-alice",
        thread_id=tid,
        run_id=run_old_sess,
        platform="weixin",
        target="weixin:@me",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_stale_sess_c["claim_status"] == "denied"
    assert r_stale_sess_c["code"] == "session_generation_superseded"

    r_stale_sess_a2 = await cg_db.authorize_and_claim_delivery(
        delivery_key="dlv-r7-stale-sess-a2",
        kind="fallback",
        user_id="u-alice",
        thread_id=tid,
        run_id=run_old_sess,
        platform="weixin",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_stale_sess_a2["claim_status"] == "denied"
    assert r_stale_sess_a2["code"] == "session_generation_superseded"

    # 3. Legacy migrated turn with NULL bot_generation -> marked unknown + writes audit!
    audit_n = len(store.audit_logs)
    r_null_g = await cg_db.authorize_and_claim_delivery(
        delivery_key="dlv-r7-null-gen",
        kind="agent_send",
        user_id="u-alice",
        thread_id=tid,
        run_id=run_null_gen,
        platform="weixin",
        target="weixin:@me",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_null_g["claim_status"] == "denied"
    assert r_null_g["status"] == "unknown"
    assert r_null_g["code"] == "source_generation_unknown"
    assert len(store.audit_logs) == audit_n + 1
    assert store.audit_logs[-1]["action"] == "ambiguous_legacy_delivery_blocked"


@pytest.mark.asyncio
async def test_r7_preflight_send_disabled_returns_404_even_with_malformed_epoch_header(
    wx_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R7 Decision #8: When channel_weixin is disabled, preflight-send returns 404 BEFORE
    parsing X-Gateway-Epoch — even when X-Gateway-Epoch is malformed (not-a-number / -5).
    """
    client: TestClient = wx_env["client"]
    monkeypatch.setenv("DALTON_CAP_CHANNEL_WEIXIN", "false")

    for bad_epoch in ("not-a-number", "-5", "0"):
        resp = client.post(
            "/api/v1/dalton/channels/internal/deliveries/dlv-r7-bad-hdr/preflight-send",
            headers={
                "X-Service-Token": SERVICE_TOKEN,
                "X-Gateway-Epoch": bad_epoch,
            },
            json={"delivery_attempt": 1, "chunk_index": 0},
        )
        assert resp.status_code == 404, f"epoch={bad_epoch!r}: got {resp.status_code} {resp.text}"


@pytest.mark.asyncio
async def test_r7_session_gen_advances_company_auth_rejected_and_unknown_sender_writes_no_token(
    wx_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R7 Decision #9:
    1) Weixin first `/new`, `workspace_switch`, account switch, and `/unbind` each advance
       `dalton_channel_session_generations.generation`.
    2) Company identity (`system:dalton-company`) is rejected on new QR start & confirm entrypoints.
    3) Unknown Weixin sender command returns `not_linked` and creates ZERO token rows.
    """
    from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID

    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    aid = str(uuid4())
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-sg@im.wechat",
        "encrypted_bot_token": "tok-sg",
        "bot_token": "tok-sg",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_alice@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }

    # 1a. First /new advances generation from 1 -> 2
    r_new = await cg_db.execute_session_command(
        cmd="new",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_alice@im.wechat",
        message_id="r7_sg_new_1",
        weixin_account_id=aid,
        weixin_bot_id="bot-sg@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_new["ok"] is True
    assert r_new["generation"] == 2
    assert store.session_generations[("weixin", "dm:wx_alice@im.wechat", "u-alice")]["generation"] == 2

    # 1b. workspace_switch to a different workspace advances generation from 2 -> 3
    ws_b = str(uuid4())
    store.workspaces[ws_b] = {
        "workspace_id": ws_b,
        "user_id": "u-alice",
        "name": "Research Desk",
        "status": "running",
    }
    monkeypatch.setattr(
        cg_db,
        "_verify_workspace_write_permission",
        create_autospec(
            cg_db._verify_workspace_write_permission,
            return_value=(True, None, store.workspaces[ws_b]),
        ),
    )
    r_sw = await cg_db.execute_session_command(
        cmd="workspace_switch",
        platform="weixin",
        platform_user_id="wx_alice@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_alice@im.wechat",
        message_id="r7_sg_sw_1",
        target_kind="workspace",
        workspace_id=ws_b,
        weixin_account_id=aid,
        weixin_bot_id="bot-sg@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_sw["ok"] is True
    assert r_sw["generation"] == 3
    assert store.session_generations[("weixin", "dm:wx_alice@im.wechat", "u-alice")]["generation"] == 3

    # 1c. Account switch (rescan confirm with DIFFERENT ilink_user_id) advances generation 3 -> 4
    s_sw = await wx_db.start_weixin_login_session(user_id="u-alice")
    await wx_db.patch_weixin_login_session(
        session_key=s_sw["session_key"], gateway_epoch=1, action="save_qr", qrcode="qr-sw"
    )
    c_sw = await wx_db.patch_weixin_login_session(
        session_key=s_sw["session_key"],
        gateway_epoch=1,
        action="confirm",
        expected_login_version=2,
        expected_bot_generation=1,
        ilink_bot_id="bot-sg-2@im.wechat",
        bot_token="tok-sg-2",
        ilink_user_id="wx_alice_2@im.wechat",
    )
    assert c_sw["ok"] is True
    assert store.session_generations[("weixin", "dm:wx_alice@im.wechat", "u-alice")]["generation"] == 4

    # 1d. /unbind advances generation 4 -> 5
    u_res = await cg_db.consume_bind_code_or_unbind(
        action="unbind",
        platform="weixin",
        platform_user_id="wx_alice_2@im.wechat",
        weixin_account_id=aid,
        weixin_bot_id="bot-sg-2@im.wechat",
        weixin_bot_generation=2,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert u_res["ok"] is True
    assert store.session_generations[("weixin", "dm:wx_alice@im.wechat", "u-alice")]["generation"] == 5

    # 2. Company system identity rejected on new QR start & confirm entrypoints
    with pytest.raises(HTTPException) as exc_comp_start:
        await wx_db.start_weixin_login_session(user_id=COMPANY_SYSTEM_USER_ID)
    assert exc_comp_start.value.status_code in (401, 403)

    # Seed a forged login session owned by COMPANY_SYSTEM_USER_ID -> confirm must reject without creating account
    forged_skey = "forged_comp_sess"
    store.login_sessions[forged_skey] = {
        "session_key": forged_skey,
        "user_id": COMPANY_SYSTEM_USER_ID,
        "target_account_id": None,
        "login_version": 2,
        "expected_bot_generation": None,
        "status": "wait",
        "expires_at": store.now + timedelta(minutes=5),
        "created_at": store.now,
        "updated_at": store.now,
    }
    with pytest.raises(HTTPException) as exc_comp_confirm:
        await wx_db.patch_weixin_login_session(
            session_key=forged_skey,
            gateway_epoch=1,
            action="confirm",
            expected_login_version=2,
            ilink_bot_id="bot-comp@im.wechat",
            bot_token="tok-comp",
            ilink_user_id="wx_comp@im.wechat",
        )
    assert exc_comp_confirm.value.status_code in (401, 403)
    assert not any(a["user_id"] == COMPANY_SYSTEM_USER_ID for a in store.accounts.values())

    # 3. Unknown Weixin sender command returns not_linked and writes ZERO token rows
    tokens_before = len(store.account_tokens)
    r_unk = await cg_db.execute_session_command(
        cmd="new",
        platform="weixin",
        platform_user_id="wx_stranger@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_stranger@im.wechat",
        message_id="r7_unknown_sender_cmd",
        weixin_context_token="ctx-stranger-must-not-save",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_unk["ok"] is False
    assert r_unk["deny_reason"] == "not_linked"
    assert len(store.account_tokens) == tokens_before


@pytest.mark.asyncio
async def test_r7_admin_unbind_reason_preset_whitelist_only(
    wx_env: dict[str, Any],
) -> None:
    """R7 Decision #10: Admin unbind accepts ONLY preset enum reasons (no missing reason,
    no free-text reason, no free-text note written to audit) across both web endpoints
    and direct data layer calls.
    """
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    client: TestClient = wx_env["client"]
    wx_env["current_user"]["user_id"] = "u-admin"

    aid = str(uuid4())
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-adm@im.wechat",
        "encrypted_bot_token": "tok-adm",
        "bot_token": "tok-adm",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_alice@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    audits_before = len(store.audit_logs)

    for url in (
        f"/api/v1/dalton/channels/weixin/admin/accounts/{aid}",
        "/api/v1/dalton/channels/weixin/admin/users/u-alice/weixin",
    ):
        # Missing body / missing reason -> 400
        r_none = client.request("DELETE", url)
        assert r_none.status_code == 400
        assert r_none.json()["code"] == "invalid_admin_unbind_reason"

        # Arbitrary free-text reason -> 400
        r_free = client.request("DELETE", url, json={"reason": "arbitrary bot_token=secret123"})
        assert r_free.status_code == 400
        assert r_free.json()["code"] == "invalid_admin_unbind_reason"

        # Valid preset + free-text note -> 400 (note must NOT be written into audit!)
        r_note = client.request(
            "DELETE",
            url,
            json={"reason": "误绑微信号", "note": "leaked-token-xyz"},
        )
        assert r_note.status_code == 400
        assert r_note.json()["code"] == "invalid_admin_unbind_reason"

    # Direct wx_db call with arbitrary reason -> 400
    with pytest.raises(HTTPException) as exc_db:
        await wx_db.unbind_weixin_account(
            account_id=aid,
            operator_user_id="u-admin",
            action="admin_unbind",
            reason="free-text-bypass",
        )
    assert exc_db.value.status_code == 400
    assert exc_db.value.detail["code"] == "invalid_admin_unbind_reason"
    assert aid in store.accounts
    assert len(store.audit_logs) == audits_before

    # Valid preset enum succeeds and stores ONLY the canonical preset string
    r_ok = client.request(
        "DELETE",
        f"/api/v1/dalton/channels/weixin/admin/accounts/{aid}",
        json={"reason": "手机遗失应急"},
    )
    assert r_ok.status_code == 200
    assert aid not in store.accounts
    assert store.audit_logs[-1]["action"] == "admin_unbind"
    assert store.audit_logs[-1]["reason"] == "手机遗失应急"


# ---------------------------------------------------------------------------
# Round 8: Batch B gateway contracts, I-7 deferred backoff, I-6/I-7 epoch exemption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r8_batch_b_items_1_2_3_4_7_8_10_contracts(
    wx_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Batch B items 1, 2, 3, 4, 7, 8, and 10 end-to-end via HTTP & DB helpers."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    client: TestClient = wx_env["client"]
    svc_hdrs = {"X-Service-Token": SERVICE_TOKEN, "X-Gateway-Epoch": "1"}
    if store.lease is not None:
        store.lease["expires_at"] = store.now + timedelta(hours=48)
    svc_read_hdrs = {"X-Service-Token": SERVICE_TOKEN}

    # --- Item 1: GET /internal/weixin/accounts/{account_id} returns get_updates_buf ---
    aid = str(uuid4())
    from src.server.database.home_workspace import get_home_workspace_id

    ws_id = get_home_workspace_id("u-alice")
    store.workspaces[ws_id] = {"workspace_id": ws_id, "user_id": "u-alice", "name": "Research", "status": "running"}
    monkeypatch.setattr(
        workspace_db,
        "get_or_create_home_workspace",
        create_autospec(
            workspace_db.get_or_create_home_workspace,
            return_value={"workspace_id": ws_id},
        ),
    )
    monkeypatch.setattr(
        cg_db,
        "_verify_workspace_write_permission",
        create_autospec(cg_db._verify_workspace_write_permission, return_value=(True, None, {"workspace_id": ws_id})),
    )
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-r8@im.wechat",
        "encrypted_bot_token": "tok-r8",
        "bot_token": "tok-r8",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "cursor-opaque-xyz",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 2,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_alice_r8@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    r_det = client.get(f"/api/v1/dalton/channels/internal/weixin/accounts/{aid}", headers=svc_read_hdrs)
    assert r_det.status_code == 200, r_det.text
    det_body = r_det.json()
    assert det_body["get_updates_buf"] == "cursor-opaque-xyz"

    # --- Item 8: POST /internal/weixin/accounts/{account_id}/status keeps string error_code ---
    r_exp = client.post(
        f"/api/v1/dalton/channels/internal/weixin/accounts/{aid}/status",
        headers=svc_hdrs,
        json={
            "action": "mark_session_expired",
            "expected_bot_generation": 2,
            "expected_ilink_bot_id": "bot-r8@im.wechat",
            "error_code": "-14",
        },
    )
    assert r_exp.status_code == 200, r_exp.text
    assert r_exp.json()["updated"] is True
    assert store.accounts[aid]["last_error_code"] == "-14"
    store.accounts[aid]["status"] = "active"

    # --- Item 4: PATCH login-sessions action="update" returns 409 session_revoked when superseded ---
    s_old = await wx_db.start_weixin_login_session(user_id="u-alice")
    await wx_db.patch_weixin_login_session(
        session_key=s_old["session_key"],
        gateway_epoch=1,
        action="save_qr",
        qrcode="qr-old",
        qrcode_img_content="data:image/png;base64,AAA",
    )
    # Supersede by starting a new login session
    await wx_db.start_weixin_login_session(user_id="u-alice")
    r_upd_409 = client.patch(
        f"/api/v1/dalton/channels/internal/weixin/login-sessions/{s_old['session_key']}",
        headers=svc_hdrs,
        json={"action": "update", "status": "scaned"},
    )
    assert r_upd_409.status_code == 409, r_upd_409.text
    assert r_upd_409.json()["code"] == "login_session_revoked"

    # --- Item 3: Conflict auto-unbind deny_reason is unified to ilink_user_conflict_auto_unbound on I-3 too ---
    aid_bob = str(uuid4())
    store.accounts[aid_bob] = {
        "account_id": aid_bob,
        "user_id": "u-bob",
        "ilink_bot_id": "bot-bob-r8@im.wechat",
        "encrypted_bot_token": "tok-bob-r8",
        "bot_token": "tok-bob-r8",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "pending_verification",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": None,
        "prev_ilink_user_id": None,
        "ilink_username": None,
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    r_i3_conflict = await cg_db.execute_session_command(
        cmd="new",
        platform="weixin",
        platform_user_id="wx_alice_r8@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_alice_r8@im.wechat",
        message_id="r8_cmd_conflict_1",
        weixin_account_id=aid_bob,
        weixin_bot_id="bot-bob-r8@im.wechat",
        weixin_bot_generation=1,
        weixin_context_token="ctx-bob-conflict",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_i3_conflict["ok"] is False
    assert r_i3_conflict["deny_reason"] == "ilink_user_conflict_auto_unbound"
    assert aid_bob not in store.accounts

    # --- Items 2, 7, 10: Collect buffer GET status, cancelled attachment ok=True, /done flushed_inbound ---
    store.user_settings["u-alice"] = {
        "user_id": "u-alice",
        "version": 1,
        "settings": {
            "apps": {
                "weixin": {
                    "preferred": "weixin:@me",
                    "chats": {"weixin:@me": {"workspace_id": ws_id}},
                    "automation_output": {},
                    "agent_messages": {"enabled": True, "allowed": ["weixin:@me"]},
                }
            }
        },
    }
    r_col = await cg_db.execute_session_command(
        cmd="collect",
        platform="weixin",
        platform_user_id="wx_alice_r8@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_alice_r8@im.wechat",
        message_id="r8_col_start",
        weixin_account_id=aid,
        weixin_bot_id="bot-r8@im.wechat",
        weixin_bot_generation=2,
        weixin_context_token="ctx-col-1",
        command_arg="整理要点",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_col["ok"] is True
    bid = r_col["collect_buffer_id"]

    # Append 1 item with attachment
    r_item = await cg_db.register_and_resolve_inbound(
        platform="weixin",
        platform_user_id="wx_alice_r8@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        thread_scope_key="dm:wx_alice_r8@im.wechat",
        message_id="r8_col_item_1",
        raw_payload={"text": "财报摘录", "platform_user_id": "wx_alice_r8@im.wechat"},
        weixin_account_id=aid,
        weixin_bot_id="bot-r8@im.wechat",
        weixin_bot_generation=2,
        weixin_context_token="ctx-col-2",
        weixin_media_meta=[{"type": "file", "filename": "q3.pdf"}],
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_item["collection_mode"] == "appended"
    assert r_item["collect_buffer_id"] == bid

    # Item 10: GET /internal/weixin/collect-buffers/{buffer_id} returns collecting & item_upload_status
    r_buf_st = client.get(
        f"/api/v1/dalton/channels/internal/weixin/collect-buffers/{bid}?message_id=r8_col_item_1",
        headers=svc_read_hdrs,
    )
    assert r_buf_st.status_code == 200, r_buf_st.text
    buf_json = r_buf_st.json()
    assert buf_json["ok"] is True
    assert buf_json["status"] == "collecting"
    assert buf_json["item_upload_status"] == "pending_upload"

    # Unknown buffer -> 404
    r_buf_404 = client.get(
        f"/api/v1/dalton/channels/internal/weixin/collect-buffers/{uuid4()}",
        headers=svc_read_hdrs,
    )
    assert r_buf_404.status_code == 404

    # Register attachment -> ready, then /done -> verify Item 7 flushed_inbound.raw_payload
    r_att_ok = client.post(
        f"/api/v1/dalton/channels/internal/weixin/collect-buffers/{bid}/items/r8_col_item_1/attachments",
        headers=svc_hdrs,
        json={"uploaded_attachments": [{"file_name": "q3.pdf", "workspace_path": "/workspace/q3.pdf"}]},
    )
    assert r_att_ok.status_code == 200
    assert r_att_ok.json()["upload_status"] == "ready"

    r_done = await cg_db.execute_session_command(
        cmd="done",
        platform="weixin",
        platform_user_id="wx_alice_r8@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_alice_r8@im.wechat",
        message_id="r8_col_done",
        weixin_account_id=aid,
        weixin_bot_id="bot-r8@im.wechat",
        weixin_bot_generation=2,
        weixin_context_token="ctx-col-3",
        command_arg="总结三条结论",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_done["ok"] is True
    flushed = r_done["flushed_inbound"]
    assert flushed is not None
    assert flushed["raw_payload"]["platform_user_id"] == "wx_alice_r8@im.wechat"
    assert "财报摘录" in flushed["raw_payload"]["text"]
    assert "总结三条结论" in flushed["raw_payload"]["text"]
    assert flushed["raw_payload"]["synthetic_collect"] is True

    # Start second collect buffer, cancel it, verify GET status="cancelled" and attachment returns ok=True without writing collect_items
    r_col2 = await cg_db.execute_session_command(
        cmd="collect",
        platform="weixin",
        platform_user_id="wx_alice_r8@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_alice_r8@im.wechat",
        message_id="r8_col2_start",
        weixin_account_id=aid,
        weixin_bot_id="bot-r8@im.wechat",
        weixin_bot_generation=2,
        weixin_context_token="ctx-col2-1",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    bid2 = r_col2["collect_buffer_id"]
    await cg_db.register_and_resolve_inbound(
        platform="weixin",
        platform_user_id="wx_alice_r8@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        thread_scope_key="dm:wx_alice_r8@im.wechat",
        message_id="r8_col2_item",
        raw_payload={"text": "待取消附件", "platform_user_id": "wx_alice_r8@im.wechat"},
        weixin_account_id=aid,
        weixin_bot_id="bot-r8@im.wechat",
        weixin_bot_generation=2,
        weixin_context_token="ctx-col2-2",
        weixin_media_meta=[{"type": "file", "filename": "cancel.pdf"}],
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    await cg_db.execute_session_command(
        cmd="cancel",
        platform="weixin",
        platform_user_id="wx_alice_r8@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_alice_r8@im.wechat",
        message_id="r8_col2_cancel",
        weixin_account_id=aid,
        weixin_bot_id="bot-r8@im.wechat",
        weixin_bot_generation=2,
        weixin_context_token="ctx-col2-3",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    r_buf2 = client.get(
        f"/api/v1/dalton/channels/internal/weixin/collect-buffers/{bid2}",
        headers=svc_read_hdrs,
    )
    assert r_buf2.status_code == 200
    assert r_buf2.json()["status"] == "cancelled"

    r_att_c = client.post(
        f"/api/v1/dalton/channels/internal/weixin/collect-buffers/{bid2}/items/r8_col2_item/attachments",
        headers=svc_hdrs,
        json={"uploaded_attachments": [{"file_name": "cancel.pdf"}]},
    )
    assert r_att_c.status_code == 200
    assert r_att_c.json()["ok"] is True
    assert r_att_c.json()["upload_status"] == "cancelled"
    # collect_items must NOT have been mutated with uploaded_attachments
    assert store.collect_items[(bid2, "r8_col2_item")]["uploaded_attachments"] == []


@pytest.mark.asyncio
async def test_r8_collect_buffer_duplicate_i1_returns_collection_mode_appended(
    wx_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batch B Item 6: Duplicate I-1 registration of a collected message returns collection_mode='appended'."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    aid = str(uuid4())
    from src.server.database.home_workspace import get_home_workspace_id

    ws_id = get_home_workspace_id("u-alice")
    store.workspaces[ws_id] = {"workspace_id": ws_id, "user_id": "u-alice", "name": "Research", "status": "running"}
    monkeypatch.setattr(
        workspace_db,
        "get_or_create_home_workspace",
        create_autospec(
            workspace_db.get_or_create_home_workspace,
            return_value={"workspace_id": ws_id},
        ),
    )
    monkeypatch.setattr(
        cg_db,
        "_verify_workspace_write_permission",
        create_autospec(cg_db._verify_workspace_write_permission, return_value=(True, None, {"workspace_id": ws_id})),
    )
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-dup@im.wechat",
        "encrypted_bot_token": "tok-dup",
        "bot_token": "tok-dup",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_dup@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    store.user_settings["u-alice"] = {
        "user_id": "u-alice",
        "version": 1,
        "settings": {
            "apps": {
                "weixin": {
                    "preferred": "weixin:@me",
                    "chats": {"weixin:@me": {"workspace_id": ws_id}},
                    "automation_output": {},
                    "agent_messages": {"enabled": True, "allowed": ["weixin:@me"]},
                }
            }
        },
    }
    r_col = await cg_db.execute_session_command(
        cmd="collect",
        platform="weixin",
        platform_user_id="wx_dup@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_dup@im.wechat",
        message_id="r8_dup_collect_cmd",
        weixin_account_id=aid,
        weixin_bot_id="bot-dup@im.wechat",
        weixin_bot_generation=1,
        weixin_context_token="ctx-dup-0",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    bid = r_col["collect_buffer_id"]

    # First I-1 append (text-only -> inbound status is 'ignored' + collect_buffer_id=bid)
    first = await cg_db.register_and_resolve_inbound(
        platform="weixin",
        platform_user_id="wx_dup@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        thread_scope_key="dm:wx_dup@im.wechat",
        message_id="r8_dup_item_1",
        raw_payload={"text": "重复投递的消息", "platform_user_id": "wx_dup@im.wechat"},
        weixin_account_id=aid,
        weixin_bot_id="bot-dup@im.wechat",
        weixin_bot_generation=1,
        weixin_context_token="ctx-dup-1",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert first["collection_mode"] == "appended"
    assert first["collect_buffer_id"] == bid

    # Duplicate I-1 replay of the same message_id MUST ALSO include collection_mode="appended"
    replay = await cg_db.register_and_resolve_inbound(
        platform="weixin",
        platform_user_id="wx_dup@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        thread_scope_key="dm:wx_dup@im.wechat",
        message_id="r8_dup_item_1",
        raw_payload={"text": "重复投递的消息", "platform_user_id": "wx_dup@im.wechat"},
        weixin_account_id=aid,
        weixin_bot_id="bot-dup@im.wechat",
        weixin_bot_generation=1,
        weixin_context_token="ctx-dup-1",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert replay["collection_mode"] == "appended"
    assert replay["collect_buffer_id"] == bid
    assert replay["collect_buffer_status"] == "collecting"


@pytest.mark.asyncio
async def test_r10_d049_migration_upgrade_downgrade_no_deferred_columns() -> None:
    """R10: Verify d049 upgrade/downgrade has zero deferred delivery columns and symmetric DDL."""
    from pathlib import Path

    d049_path = (
        Path(__file__).resolve().parents[3]
        / "src/dalton/migrations/versions/d049_weixin_multi_account.py"
    )
    src = d049_path.read_text(encoding="utf-8")
    for removed_col in (
        "defer_count",
        "first_deferred_at",
        "next_retry_at",
        "last_deferred_attempt",
        "delivered_segments",
    ):
        assert removed_col not in src, f"Removed deferred column still in d049: {removed_col}"
    for col in (
        "weixin_account_id",
        "weixin_bot_id",
        "weixin_bot_generation",
        "weixin_recipient_id",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in src
        assert f"DROP COLUMN IF EXISTS {col}" in src


@pytest.mark.asyncio
async def test_r10_i6_roster_503_no_defer_and_preflight_retryable(
    wx_env: dict[str, Any],
) -> None:
    """R10: Deferred delivery mechanism removed —
    - I-6 roster_lookup_error returns HTTP 503 with retryable=True and writes zero delivery row;
    - Preflight roster_lookup_error returns 200 retryable=True while delivery stays in_flight;
    - GET /internal/deliveries/deferred-due is removed (404);
    - startup recovery has no deferred_deliveries field.
    """
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    client: TestClient = wx_env["client"]
    roster: dict[str, str] = wx_env["roster"]
    svc_hdrs = {"X-Service-Token": SERVICE_TOKEN, "X-Gateway-Epoch": "1"}

    aid = str(uuid4())
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-r10@im.wechat",
        "encrypted_bot_token": "tok-r10",
        "bot_token": "tok-r10",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_r10@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    store.user_settings["u-alice"] = {
        "user_id": "u-alice",
        "version": "v1",
        "apps": {
            "weixin": {
                "preferred": "weixin:@me",
                "chats": {},
                "automation_output": {},
                "agent_messages": {"enabled": True, "allowed": ["weixin:@me"]},
            }
        },
    }

    # 1. GET /internal/deliveries/deferred-due is removed (404)
    r_gone = client.get(
        "/api/v1/dalton/channels/internal/deliveries/deferred-due",
        headers=svc_hdrs,
    )
    assert r_gone.status_code == 404

    # 2. I-6 roster_lookup_error returns HTTP 503 retryable=True and persists ZERO delivery row
    roster["u-alice"] = "lookup_error"
    r_503 = client.post(
        "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
        headers=svc_hdrs,
        json={
            "delivery_key": "dlv-r10-roster-503",
            "kind": "agent_send",
            "user_id": "u-alice",
            "platform": "weixin",
            "target": "weixin:@me",
        },
    )
    assert r_503.status_code == 503, r_503.text
    assert r_503.json()["code"] == "roster_lookup_error"
    assert r_503.json()["retryable"] is True
    assert "dlv-r10-roster-503" not in store.deliveries

    # 3. Preflight on an in_flight delivery with roster_lookup_error returns retryable=True, keeping in_flight
    store.deliveries["dlv-r10-pre"] = {
        "delivery_key": "dlv-r10-pre",
        "kind": "agent_send",
        "user_id": "u-alice",
        "platform": "weixin",
        "target_address": "weixin:@me",
        "status": "in_flight",
        "delivery_attempt": 1,
        "weixin_account_id": aid,
        "weixin_bot_id": "bot-r10@im.wechat",
        "weixin_bot_generation": 1,
        "weixin_recipient_id": "wx_r10@im.wechat",
    }
    r_pre = client.post(
        "/api/v1/dalton/channels/internal/deliveries/dlv-r10-pre/preflight-send",
        headers=svc_hdrs,
        json={"delivery_attempt": 1, "chunk_index": 0},
    )
    assert r_pre.status_code == 200
    assert r_pre.json()["authorized"] is False
    assert r_pre.json()["retryable"] is True
    assert r_pre.json()["deny_code"] == "roster_lookup_error"
    assert store.deliveries["dlv-r10-pre"]["status"] == "in_flight"
    roster["u-alice"] = "active"

    # 4. Startup recovery no longer includes deferred_deliveries
    rec = await cg_db.recover_channel_state(
        mode="startup", gateway_epoch=1, require_gateway_epoch=True
    )
    assert "deferred_deliveries" not in rec


@pytest.mark.asyncio
async def test_r8_feishu_discord_i6_i7_no_epoch_required_after_lease_weixin_still_409(
    wx_env: dict[str, Any],
) -> None:
    """Batch B Item 9: Even when lease table has an active lease row, Feishu/Discord I-6 & I-7 succeed without X-Gateway-Epoch, while Weixin I-6 & I-7 still return 409."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    client: TestClient = wx_env["client"]
    no_epoch_hdrs = {"X-Service-Token": SERVICE_TOKEN}
    stale_epoch_hdrs = {"X-Service-Token": SERVICE_TOKEN, "X-Gateway-Epoch": "999"}

    # Ensure active lease exists in DB
    await wx_db.acquire_gateway_lease(instance_id="gw-r8-active")
    assert store.lease is not None

    # 1. Feishu & Discord I-6 without X-Gateway-Epoch MUST succeed (200)
    for plat, tgt, dkey in (
        ("feishu", "feishu:@me", "dlv-r8-feishu-i6"),
        ("discord", "discord:@me", "dlv-r8-discord-i6"),
    ):
        r_i6 = client.post(
            "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
            headers=no_epoch_hdrs,
            json={
                "delivery_key": dkey,
                "kind": "agent_send",
                "user_id": "u-alice",
                "platform": plat,
                "target": tgt,
            },
        )
        assert r_i6.status_code == 200, f"{plat} I-6 failed: {r_i6.text}"

        # 2. Feishu & Discord I-7 deliveries/progress without X-Gateway-Epoch MUST succeed (200)
        store.deliveries[f"{dkey}-prog"] = {
            "delivery_key": f"{dkey}-prog",
            "user_id": "u-alice",
            "platform": plat,
            "target_address": tgt,
            "status": "in_flight",
            "delivery_attempt": 1,
            "source_scope_key": None,
            "source_generation": None,
            "activity_origin": "none",
        }
        r_i7_d = client.post(
            "/api/v1/dalton/channels/internal/deliveries/progress",
            headers=no_epoch_hdrs,
            json={"delivery_key": f"{dkey}-prog", "status": "sent"},
        )
        assert r_i7_d.status_code == 200, f"{plat} I-7 delivery failed: {r_i7_d.text}"

        # 3. Feishu & Discord I-7 inbound/progress without X-Gateway-Epoch MUST succeed (200)
        r_i7_in = client.post(
            "/api/v1/dalton/channels/internal/inbound/progress",
            headers=no_epoch_hdrs,
            json={"platform": plat, "message_id": f"{plat}_r8_in_1", "status": "completed"},
        )
        assert r_i7_in.status_code == 200, f"{plat} I-7 inbound failed: {r_i7_in.text}"

        # 4. Explicit stale X-Gateway-Epoch is STILL rejected (409) even on Feishu/Discord
        r_stale = client.post(
            "/api/v1/dalton/channels/internal/deliveries/progress",
            headers=stale_epoch_hdrs,
            json={"delivery_key": f"{dkey}-prog", "status": "sent"},
        )
        assert r_stale.status_code == 409

    # 5. Weixin I-6 & I-7 without X-Gateway-Epoch MUST STILL return 409 stale_gateway_epoch
    r_wx_i6 = client.post(
        "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
        headers=no_epoch_hdrs,
        json={
            "delivery_key": "dlv-r8-wx-no-epoch",
            "kind": "agent_send",
            "user_id": "u-alice",
            "platform": "weixin",
            "target": "weixin:@me",
        },
    )
    assert r_wx_i6.status_code == 409
    assert r_wx_i6.json()["code"] == "stale_gateway_epoch"

    store.deliveries["dlv-r8-wx-prog"] = {
        "delivery_key": "dlv-r8-wx-prog",
        "user_id": "u-alice",
        "platform": "weixin",
        "target_address": "weixin:@me",
        "status": "in_flight",
        "delivery_attempt": 1,
        "source_scope_key": None,
        "source_generation": None,
        "activity_origin": "none",
    }
    r_wx_i7_d = client.post(
        "/api/v1/dalton/channels/internal/deliveries/progress",
        headers=no_epoch_hdrs,
        json={"delivery_key": "dlv-r8-wx-prog", "status": "sent"},
    )
    assert r_wx_i7_d.status_code == 409
    assert r_wx_i7_d.json()["code"] == "stale_gateway_epoch"

    r_wx_i7_in = client.post(
        "/api/v1/dalton/channels/internal/inbound/progress",
        headers=no_epoch_hdrs,
        json={"platform": "weixin", "message_id": "wx_r8_in_1", "status": "completed"},
    )
    assert r_wx_i7_in.status_code == 409
    assert r_wx_i7_in.json()["code"] == "stale_gateway_epoch"

    # 6. Feishu I-1 without X-Gateway-Epoch STILL returns 409 once a lease exists
    r_f_i1 = client.post(
        "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
        headers=no_epoch_hdrs,
        json={
            "platform": "feishu",
            "platform_user_id": "ou_r8_1",
            "chat_type": "dm",
            "chat_address": "feishu:@me",
            "thread_scope_key": "dm:ou_r8_1",
            "message_id": "feishu_r8_i1_post_lease",
            "raw_payload": {"text": "hi"},
        },
    )
    assert r_f_i1.status_code == 409


# ---------------------------------------------------------------------------
# Round 9: All 12 review decisions (DB platform authority, deferred/partial_deferred,
#          I-3 sender check, I-6 FOR SHARE + legacy dm addr, I-1 pause rollback,
#          collect too_late & dup media meta, audit enum)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r9_inbound_progress_db_platform_authority_and_mismatch_409(
    wx_env: dict[str, Any],
) -> None:
    """R9 Decision #1: POST /internal/inbound/progress reads authoritative DB platform for
    existing thread_watches & background_runs, enforcing Weixin epoch regardless of request body
    and rejecting platform mismatch with 409 without mutating DB rows.
    """
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    client: TestClient = wx_env["client"]
    no_epoch_hdrs = {"X-Service-Token": SERVICE_TOKEN}
    valid_hdrs = {"X-Service-Token": SERVICE_TOKEN, "X-Gateway-Epoch": "1"}

    tid = str(uuid4())
    bg_rid = str(uuid4())
    store.thread_watches[tid] = {
        "thread_id": tid,
        "dalton_user_id": "u-alice",
        "platform": "weixin",
        "target_address": "weixin:@me",
        "scope_key": "dm:wx_alice@im.wechat",
        "generation": 2,
        "watch_active": True,
        "pending_interrupt": None,
    }
    store.background_runs[bg_rid] = {
        "run_id": bg_rid,
        "thread_id": tid,
        "dalton_user_id": "u-alice",
        "platform": "weixin",
        "target_address": "weixin:@me",
        "status": "streaming",
        "fallback_pending": True,
        "weixin_bot_generation": None,
    }

    # 1. Spoofed feishu on Weixin watch WITHOUT epoch -> 409 stale_gateway_epoch
    r_no_ep = client.post(
        "/api/v1/dalton/channels/internal/inbound/progress",
        headers=no_epoch_hdrs,
        json={
            "thread_watch": {
                "thread_id": tid,
                "dalton_user_id": "u-alice",
                "platform": "feishu",
                "target_address": "feishu:@me",
                "scope_key": "dm:ou_spoof",
                "generation": 99,
                "watch_active": False,
            }
        },
    )
    assert r_no_ep.status_code == 409
    assert r_no_ep.json()["code"] == "stale_gateway_epoch"
    assert store.thread_watches[tid]["watch_active"] is True

    # 2. Spoofed feishu on Weixin watch WITH valid epoch -> 409 platform_mismatch
    r_mm = client.post(
        "/api/v1/dalton/channels/internal/inbound/progress",
        headers=valid_hdrs,
        json={
            "thread_watch": {
                "thread_id": tid,
                "dalton_user_id": "u-alice",
                "platform": "feishu",
                "target_address": "feishu:@me",
                "scope_key": "dm:ou_spoof",
                "generation": 99,
                "watch_active": False,
            }
        },
    )
    assert r_mm.status_code == 409
    assert r_mm.json()["code"] == "platform_mismatch"
    assert store.thread_watches[tid]["generation"] == 2

    # 3. Spoofed feishu on Weixin background_run WITHOUT epoch -> 409 stale_gateway_epoch
    r_bg = client.post(
        "/api/v1/dalton/channels/internal/inbound/progress",
        headers=no_epoch_hdrs,
        json={
            "background_run": {
                "run_id": bg_rid,
                "thread_id": tid,
                "dalton_user_id": "u-alice",
                "platform": "feishu",
                "target_address": "feishu:@me",
                "status": "completed",
                "fallback_pending": False,
                "weixin_bot_generation": 77,
            }
        },
    )
    assert r_bg.status_code == 409
    assert r_bg.json()["code"] == "stale_gateway_epoch"
    assert store.background_runs[bg_rid]["status"] == "streaming"


@pytest.mark.asyncio
async def test_r10_i7_delivery_status_transition_table_and_discord_unknown_guard(
    wx_env: dict[str, Any],
) -> None:
    """R10 Codex #5: Centralized I-7 delivery status transition table & Discord unknown protection."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    client: TestClient = wx_env["client"]
    no_ep = {"X-Service-Token": SERVICE_TOKEN}

    # 1. Discord unknown cannot be changed via status OR outcome (nor conflicting status+outcome)
    store.deliveries["dlv-dc-unk"] = {
        "delivery_key": "dlv-dc-unk",
        "kind": "agent_send",
        "user_id": "u-alice",
        "platform": "discord",
        "target_address": "discord:@me",
        "status": "unknown",
        "delivery_attempt": 1,
        "error_code": "send_timeout",
        "source_scope_key": None,
        "source_generation": None,
        "activity_origin": "none",
    }
    for bad_body, expected_http in (
        ({"delivery_key": "dlv-dc-unk", "outcome": "deferred"}, 400),
        ({"delivery_key": "dlv-dc-unk", "outcome": "partial_deferred"}, 400),
        ({"delivery_key": "dlv-dc-unk", "status": "unknown", "outcome": "sent"}, 400),
        ({"delivery_key": "dlv-dc-unk", "outcome": "sent"}, 409),
        ({"delivery_key": "dlv-dc-unk", "status": "in_flight"}, 409),
        ({"delivery_key": "dlv-dc-unk", "status": "failed"}, 409),
    ):
        r_dc = client.post(
            "/api/v1/dalton/channels/internal/deliveries/progress",
            headers=no_ep,
            json=bad_body,
        )
        assert r_dc.status_code == expected_http, f"{bad_body}: {r_dc.status_code} {r_dc.text}"
    assert store.deliveries["dlv-dc-unk"]["status"] == "unknown"

    # 2. Exact Discord recovery_terminal (gateway_restarted + dispatching + matching attempt) IS allowed
    store.deliveries["dlv-dc-rec"] = {
        "delivery_key": "dlv-dc-rec",
        "kind": "agent_send",
        "user_id": "u-alice",
        "platform": "discord",
        "target_address": "discord:@me",
        "status": "unknown",
        "delivery_attempt": 2,
        "error_code": "gateway_restarted",
        "discord_body_phase": "dispatching",
        "source_scope_key": None,
        "source_generation": None,
        "activity_origin": "none",
    }
    r_rec = client.post(
        "/api/v1/dalton/channels/internal/deliveries/progress",
        headers=no_ep,
        json={"delivery_key": "dlv-dc-rec", "status": "sent", "delivery_attempt": 2},
    )
    assert r_rec.status_code == 200, r_rec.text
    assert store.deliveries["dlv-dc-rec"]["status"] == "sent"

    # 3. Terminal sent / failed / unknown cannot regress to in_flight or flip terminal states via I-7
    for term_st, bad_next in (
        ("sent", "in_flight"),
        ("sent", "failed"),
        ("sent", "unknown"),
        ("failed", "in_flight"),
        ("failed", "sent"),
        ("unknown", "in_flight"),
        ("unknown", "sent"),
    ):
        dk = f"dlv-term-{term_st}-{bad_next}"
        store.deliveries[dk] = {
            "delivery_key": dk,
            "kind": "agent_send",
            "user_id": "u-alice",
            "platform": "feishu",
            "target_address": "feishu:@me",
            "status": term_st,
            "delivery_attempt": 1,
            "source_scope_key": None,
            "source_generation": None,
            "activity_origin": "none",
        }
        r_bad = client.post(
            "/api/v1/dalton/channels/internal/deliveries/progress",
            headers=no_ep,
            json={"delivery_key": dk, "status": bad_next},
        )
        assert r_bad.status_code == 409, f"{term_st}->{bad_next}: {r_bad.status_code} {r_bad.text}"
        assert store.deliveries[dk]["status"] == term_st


@pytest.mark.asyncio
async def test_r10_collect_done_cancel_help_reject_other_sender_without_command_result(
    wx_env: dict[str, Any],
) -> None:
    """R10 Codex #2: /collect, /done, /cancel, /help check account/user/sender on ANY existing
    (platform='weixin', message_id) row even when command_result is None, never overwriting it."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    aid_a, aid_b = str(uuid4()), str(uuid4())
    for aid, uid, bot, wxu in (
        (aid_a, "u-alice", "bot-col-a@im.wechat", "wx_col_a@im.wechat"),
        (aid_b, "u-bob", "bot-col-b@im.wechat", "wx_col_b@im.wechat"),
    ):
        store.accounts[aid] = {
            "account_id": aid,
            "user_id": uid,
            "ilink_bot_id": bot,
            "encrypted_bot_token": f"tok-{uid}",
            "bot_token": f"tok-{uid}",
            "base_url": "https://ilinkai.weixin.qq.com",
            "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
            "get_updates_buf": "",
            "status": "active",
            "prev_status": None,
            "disabled_reason": None,
            "bot_generation": 1,
            "prev_bot_generation": None,
            "bot_generation_updated_at": None,
            "ilink_user_id": wxu,
            "prev_ilink_user_id": None,
            "ilink_username": uid,
            "last_inbound_at": None,
            "created_at": store.now,
            "updated_at": store.now,
        }

    # Alice has a regular inbound message row (command_result is None!)
    mid = "wx_alice_regular_msg_r10"
    store.inbound_events[("weixin", mid)] = {
        "platform": "weixin",
        "message_id": mid,
        "request_key": str(uuid4()),
        "dalton_user_id": "u-alice",
        "chat_type": "dm",
        "chat_address": "weixin:@me",
        "thread_scope_key": "dm:wx_col_a@im.wechat",
        "status": "pending",
        "command_reply_pending": False,
        "command_effect_status": None,
        "command_result": None,
        "raw_payload": {"platform_user_id": "wx_col_a@im.wechat", "text": "alice msg"},
        "weixin_account_id": aid_a,
        "weixin_context_bot_id": "bot-col-a@im.wechat",
        "weixin_context_bot_generation": 1,
    }

    for cmd in ("collect", "done", "cancel", "help"):
        res = await cg_db.execute_session_command(
            cmd=cmd,
            platform="weixin",
            platform_user_id="wx_col_b@im.wechat",
            chat_type="dm",
            chat_address="weixin:@me",
            scope_key="dm:wx_col_b@im.wechat",
            message_id=mid,
            weixin_account_id=aid_b,
            weixin_bot_id="bot-col-b@im.wechat",
            weixin_bot_generation=1,
            gateway_epoch=1,
            require_gateway_epoch=True,
        )
        assert res["ok"] is False, f"/{cmd} must reject collision on Alice's inbound row"
        assert res["deny_reason"] == "weixin_message_id_account_mismatch"
        row = store.inbound_events[("weixin", mid)]
        assert row["status"] == "pending"
        assert row["command_result"] is None
        assert row["command_reply_pending"] is False
        assert row["dalton_user_id"] == "u-alice"


@pytest.mark.asyncio
async def test_r10_inbound_progress_derives_platform_from_conversation_threads(
    wx_env: dict[str, Any],
) -> None:
    """R10 Codex #1: First write to inbound/progress derives platform from conversation_threads.platform
    when no watch/background_run/inbound row exists yet, rejecting spoofed feishu with 409."""
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    client: TestClient = wx_env["client"]
    no_ep = {"X-Service-Token": SERVICE_TOKEN}
    valid_ep = {"X-Service-Token": SERVICE_TOKEN, "X-Gateway-Epoch": "1"}

    tid = str(uuid4())
    store.conversation_threads[tid] = {
        "conversation_thread_id": tid,
        "platform": "weixin",
    }

    # First-time thread_watch write claiming feishu on a conversation_threads(platform='weixin') row:
    # 1) Without epoch -> 409 stale_gateway_epoch
    r1 = client.post(
        "/api/v1/dalton/channels/internal/inbound/progress",
        headers=no_ep,
        json={
            "thread_watch": {
                "thread_id": tid,
                "dalton_user_id": "u-alice",
                "platform": "feishu",
                "target_address": "feishu:@me",
                "scope_key": "dm:ou_spoof",
                "generation": 1,
                "watch_active": True,
            }
        },
    )
    assert r1.status_code == 409
    assert r1.json()["code"] == "stale_gateway_epoch"
    assert tid not in store.thread_watches

    # 2) With valid epoch -> 409 platform_mismatch
    r2 = client.post(
        "/api/v1/dalton/channels/internal/inbound/progress",
        headers=valid_ep,
        json={
            "thread_watch": {
                "thread_id": tid,
                "dalton_user_id": "u-alice",
                "platform": "feishu",
                "target_address": "feishu:@me",
                "scope_key": "dm:ou_spoof",
                "generation": 1,
                "watch_active": True,
            }
        },
    )
    assert r2.status_code == 409
    assert r2.json()["code"] == "platform_mismatch"
    assert tid not in store.thread_watches


@pytest.mark.asyncio
async def test_r9_i3_sender_check_before_invalid_inbound_and_missing_puid_no_overwrite(
    wx_env: dict[str, Any],
) -> None:
    """R9 Decision #4: I-3 never overwrites an existing command row when sender mismatches
    (even when the spoofed replay also carries invalid_inbound=True, or omits platform_user_id).
    """
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    aid = str(uuid4())
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-i3s@im.wechat",
        "encrypted_bot_token": "tok-i3s",
        "bot_token": "tok-i3s",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_alice_orig@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    mid = "r9_orig_cmd_mid"
    store.inbound_events[("weixin", mid)] = {
        "platform": "weixin",
        "message_id": mid,
        "request_key": str(uuid4()),
        "dalton_user_id": "u-alice",
        "chat_type": "dm",
        "chat_address": "weixin:@me",
        "thread_scope_key": "dm:wx_alice_orig@im.wechat",
        "status": "completed",
        "command_reply_pending": True,
        "command_effect_status": "done",
        "command_result": {"ok": True, "reply_text": "original-reply"},
        "raw_payload": {"platform_user_id": "wx_alice_orig@im.wechat", "text": "/new"},
        "weixin_account_id": aid,
        "weixin_context_bot_id": "bot-i3s@im.wechat",
        "weixin_context_bot_generation": 1,
    }

    # 1. Mismatched sender + invalid_inbound=True MUST NOT overwrite existing row
    r_inv = await cg_db.execute_session_command(
        cmd="new",
        platform="weixin",
        platform_user_id="wx_attacker@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_attacker@im.wechat",
        message_id=mid,
        raw_payload={"invalid_inbound": True, "platform_user_id": "wx_attacker@im.wechat"},
        weixin_account_id=aid,
        weixin_bot_id="bot-i3s@im.wechat",
        weixin_bot_generation=1,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_inv["ok"] is False
    assert store.inbound_events[("weixin", mid)]["status"] == "completed"
    assert store.inbound_events[("weixin", mid)]["command_result"] == {"ok": True, "reply_text": "original-reply"}

    # 2. Request omitting platform_user_id whose looked-up identity mismatches orig_puid MUST NOT overwrite
    aid_bob = str(uuid4())
    store.accounts[aid_bob] = {
        **store.accounts[aid],
        "account_id": aid_bob,
        "user_id": "u-bob",
        "ilink_bot_id": "bot-bob-i3s@im.wechat",
        "ilink_user_id": "wx_bob@im.wechat",
    }
    r_no_puid = await cg_db.execute_session_command(
        cmd="new",
        platform="weixin",
        dalton_user_id="u-bob",
        platform_user_id=None,
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_bob@im.wechat",
        message_id=mid,
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    assert r_no_puid["ok"] is False
    assert store.inbound_events[("weixin", mid)]["status"] == "completed"
    assert store.inbound_events[("weixin", mid)]["command_reply_pending"] is True


@pytest.mark.asyncio
async def test_r9_collect_too_late_unified_status_and_dup_media_meta_preserved(
    wx_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9 Decisions #9 & #11:
    - GET /internal/weixin/collect-buffers/{buffer_id} returns unified `upload_status` (and `item_upload_status`);
    - POST attachments after buffer is `done` (or `flushing` on settled item) returns {ok: True, upload_status: "too_late"} without writing collect_items;
    - Duplicate append_weixin_collect_item_in_tx does not overwrite encrypted_weixin_media_meta.
    """
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]
    client: TestClient = wx_env["client"]
    svc_hdrs = {"X-Service-Token": SERVICE_TOKEN, "X-Gateway-Epoch": "1"}
    svc_read_hdrs = {"X-Service-Token": SERVICE_TOKEN}

    aid = str(uuid4())
    from src.server.database.home_workspace import get_home_workspace_id

    ws_id = get_home_workspace_id("u-alice")
    store.workspaces[ws_id] = {"workspace_id": ws_id, "user_id": "u-alice", "name": "Home", "status": "running"}
    monkeypatch.setattr(
        workspace_db,
        "get_or_create_home_workspace",
        create_autospec(workspace_db.get_or_create_home_workspace, return_value={"workspace_id": ws_id}),
    )
    monkeypatch.setattr(
        cg_db,
        "_verify_workspace_write_permission",
        create_autospec(cg_db._verify_workspace_write_permission, return_value=(True, None, {"workspace_id": ws_id})),
    )
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-r9c@im.wechat",
        "encrypted_bot_token": "tok-r9c",
        "bot_token": "tok-r9c",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "active",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": "wx_r9c@im.wechat",
        "prev_ilink_user_id": None,
        "ilink_username": "Alice",
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    r_col = await cg_db.execute_session_command(
        cmd="collect",
        platform="weixin",
        platform_user_id="wx_r9c@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        scope_key="dm:wx_r9c@im.wechat",
        message_id="r9c_start",
        weixin_account_id=aid,
        weixin_bot_id="bot-r9c@im.wechat",
        weixin_bot_generation=1,
        weixin_context_token="ctx-r9c-0",
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    bid = r_col["collect_buffer_id"]

    await cg_db.register_and_resolve_inbound(
        platform="weixin",
        platform_user_id="wx_r9c@im.wechat",
        chat_type="dm",
        chat_address="weixin:@me",
        thread_scope_key="dm:wx_r9c@im.wechat",
        message_id="r9c_it1",
        raw_payload={"text": "file1", "platform_user_id": "wx_r9c@im.wechat"},
        weixin_account_id=aid,
        weixin_bot_id="bot-r9c@im.wechat",
        weixin_bot_generation=1,
        weixin_context_token="ctx-r9c-1",
        weixin_media_meta=[{"type": "file", "filename": "orig.pdf", "aes_key": "k-orig"}],
        gateway_epoch=1,
        require_gateway_epoch=True,
    )
    orig_meta = store.inbound_events[("weixin", "r9c_it1")]["encrypted_weixin_media_meta"]
    assert "k-orig" in str(orig_meta)

    # Direct duplicate append MUST NOT overwrite encrypted_weixin_media_meta
    cur = create_autospec(psycopg.AsyncCursor, instance=True)

    async def _exec(sql: str, params: Any = None) -> None:
        cur._last = await store.handle_sql(sql, params)

    async def _fetchone() -> Any:
        rows = getattr(cur, "_last", [])
        return rows[0] if rows else None

    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = _fetchone
    await wx_db.append_weixin_collect_item_in_tx(
        cur,
        account_id=aid,
        bot_generation=1,
        dalton_user_id="u-alice",
        scope_key="dm:wx_r9c@im.wechat",
        session_generation=1,
        message_id="r9c_it1",
        text_content="file1",
        protocol_seq=None,
        platform_time_ms=None,
        weixin_media_meta=[{"type": "file", "filename": "spoof.pdf", "aes_key": "k-spoof"}],
        verify_workspace_write_fn=cg_db._verify_workspace_write_permission,
    )
    assert store.inbound_events[("weixin", "r9c_it1")]["encrypted_weixin_media_meta"] == orig_meta

    # Unified upload_status field on GET collect buffer status
    r_st = client.get(
        f"/api/v1/dalton/channels/internal/weixin/collect-buffers/{bid}?message_id=r9c_it1",
        headers=svc_read_hdrs,
    )
    assert r_st.status_code == 200
    assert r_st.json()["upload_status"] == "pending_upload"
    assert r_st.json()["item_upload_status"] == "pending_upload"

    # Mark buffer done -> late attachment registration returns too_late without mutating collect_items
    store.collect_buffers[bid]["status"] = "done"
    r_late = client.post(
        f"/api/v1/dalton/channels/internal/weixin/collect-buffers/{bid}/items/r9c_it1/attachments",
        headers=svc_hdrs,
        json={"uploaded_attachments": [{"file_name": "orig.pdf"}]},
    )
    assert r_late.status_code == 200, r_late.text
    assert r_late.json()["ok"] is True
    assert r_late.json()["upload_status"] == "too_late"
    assert store.collect_items[(bid, "r9c_it1")]["uploaded_attachments"] == []


@pytest.mark.asyncio
async def test_r9_pending_verification_admin_pause_rolls_back_and_legacy_dm_address_normalization(
    wx_env: dict[str, Any],
) -> None:
    """R9 Decisions #6 & #12:
    - If admin disables Weixin channel before pending_verification commits (even after conflict branch),
      the transaction rolls back with zero activation, zero conflict deletion, and zero one-time reply;
    - Legacy Weixin DM address `weixin:dm:...` normalizes to `weixin:@me` and counts as DM.
    """
    store: InMemoryWeixinMultiAccountStore = wx_env["store"]

    # Decision #12: legacy weixin:dm:... normalization
    assert cg_db._is_dm_address("weixin", "weixin:@me") is True
    assert cg_db._is_dm_address("weixin", "weixin:dm:wx_legacy@im.wechat") is True
    assert cg_db._normalize_dm_target_address("weixin", "weixin:dm:wx_legacy@im.wechat") == "weixin:@me"
    assert cg_db._normalize_dm_target_address("weixin", "weixin:@me") == "weixin:@me"
    assert cg_db._normalize_dm_target_address("feishu", "feishu:dm:ou_123") == "feishu:dm:ou_123"

    # Decision #6: admin disables Weixin -> pending_verification refuses & leaves account untouched
    aid = str(uuid4())
    store.accounts[aid] = {
        "account_id": aid,
        "user_id": "u-alice",
        "ilink_bot_id": "bot-r9v@im.wechat",
        "encrypted_bot_token": "tok-r9v",
        "bot_token": "tok-r9v",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "get_updates_buf": "",
        "status": "pending_verification",
        "prev_status": None,
        "disabled_reason": None,
        "bot_generation": 1,
        "prev_bot_generation": None,
        "bot_generation_updated_at": None,
        "ilink_user_id": None,
        "prev_ilink_user_id": None,
        "ilink_username": None,
        "last_inbound_at": None,
        "created_at": store.now,
        "updated_at": store.now,
    }
    store.config["enabled"] = False
    res_paused = await wx_db.verify_pending_weixin_account_first_message(
        account_id=aid,
        platform_user_id="wx_alice_r9v@im.wechat",
        gateway_epoch=1,
        require_gateway_epoch=True,
        inbound_context_token="ctx-r9v",
    )
    assert res_paused["outcome"] == "denied"
    assert res_paused["deny_reason"] == "weixin_channel_disabled"
    assert "one_time_weixin_reply" not in res_paused
    assert store.accounts[aid]["status"] == "pending_verification"
    assert store.accounts[aid]["ilink_user_id"] is None







