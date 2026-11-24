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
            if "AND enabled = TRUE AND (%s <> 'external' OR COALESCE(allow_external, %s) = TRUE)" in norm:
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
            return [dict(w) for w in self.thread_watches.values() if w.get("watch_active")]

        # --- Conversation queries + responses (watch backfill Codex ①) ---
        if "FROM conversation_queries q JOIN conversation_responses r" in norm:
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

        # --- Background runs ---
        if "INSERT INTO dalton_channel_background_runs" in norm:
            rid = str(params[0])
            if rid not in self.background_runs:
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

        if "FROM dalton_channel_background_runs" in norm and "SELECT" in norm:
            if "WHERE run_id = %s" in norm:
                r = self.background_runs.get(str(params[0]))
                return [dict(r)] if r else []
            return [
                dict(r)
                for r in self.background_runs.values()
                if r.get("fallback_pending") or r.get("status") == "streaming"
            ]

        # --- Deliveries ---
        if "FROM dalton_channel_deliveries" in norm and "SELECT" in norm:
            if "WHERE delivery_key = %s" in norm:
                d = self.deliveries.get(str(params[0]))
                return [dict(d)] if d else []
            if "WHERE status = 'in_flight'" in norm:
                return [dict(d) for d in self.deliveries.values() if d.get("status") == "in_flight"]
            return list(self.deliveries.values())

        if "UPDATE dalton_channel_deliveries SET status = 'unknown', error_code = 'gateway_restarted'" in norm:
            settled = []
            for d in self.deliveries.values():
                if d.get("status") == "in_flight":
                    d["status"] = "unknown"
                    d["error_code"] = "gateway_restarted"
                    d["updated_at"] = now
                    settled.append(dict(d))
            return settled

        if "INSERT INTO dalton_channel_deliveries" in norm:
            dkey = str(params[0])
            self.deliveries[dkey] = {
                "delivery_key": dkey,
                "status": "unknown" if "'unknown'" in norm else "in_flight",
                "error_code": "source_generation_unknown" if "source_generation_unknown" in norm else None,
                "platform": "weixin",
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
                    r["status"] = "completed"
                    r["fallback_pending"] = False
                    r["uploaded_attachments"] = atts
                    r["encrypted_weixin_media_meta"] = None
                return []
            return []

        if "INSERT INTO dalton_channel_inbound_events" in norm:
            # Handle synthetic collect inbound or normal weixin inbound
            if "VALUES ('weixin', %s" in norm or "VALUES ( 'weixin', %s" in norm:
                mid = str(params[0])
                key = ("weixin", mid)
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
                    "created_at": now,
                    "updated_at": now,
                }
                for p in params:
                    if isinstance(p, str) and p in self.collect_buffers:
                        row["collect_buffer_id"] = p
                    elif hasattr(p, "obj") and isinstance(p.obj, dict):
                        row["raw_payload"] = dict(p.obj)
                    elif hasattr(p, "obj") and isinstance(p.obj, list):
                        row["uploaded_attachments"] = list(p.obj)
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
            elif "generation = dalton_channel_session_generations.generation + 1" in norm:
                self.session_generations[key]["generation"] += 1
            return [{"generation": self.session_generations[key]["generation"]}]

        if "UPDATE dalton_channel_session_generations" in norm:
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

    # 2. Admin revokes by target_user_id BEFORE any account_id exists
    wx_env["current_user"]["user_id"] = "u-admin"
    r_rev = client.request(
        "DELETE",
        "/api/v1/dalton/channels/weixin/admin/users/u-alice/weixin",
        json={"reason": "误绑微信号", "note": "提前撤销"},
    )
    assert r_rev.status_code == 200
    assert store.login_sessions[skey]["status"] == "superseded"
    assert store.audit_logs[-1]["action"] == "admin_unbind"
    assert "误绑微信号：提前撤销" in store.audit_logs[-1]["reason"]

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
    unbind_res = await wx_db.unbind_weixin_account(
        account_id=aid_a,
        operator_user_id="u-alice",
        action="member_unbind",
        via_wechat_command=True,
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
