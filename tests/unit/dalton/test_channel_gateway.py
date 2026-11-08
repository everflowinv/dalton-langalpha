"""Unit tests for CH-1 Phase 1:
- Two backend colleague-sharing fixes (`handoff_liveness.py` & `channel_settings.py`);
- Internal persistence endpoints `I-1` ~ `I-9` (§1.4);
- Web frontend channel management endpoints (§1.5).

All test doubles are constructed with `unittest.mock.create_autospec`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import create_autospec
from uuid import UUID, uuid4, uuid5

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import psycopg
import pytest

from ptc_agent.agent.backends.db_json_route import UserDataValidationError
from ptc_agent.core.sandbox.livefs_mount import CallContext
from src.dalton import sharing as dalton_sharing
from src.dalton.app import channels_router as ch_router_mod
from src.dalton.app.router import router as dalton_router
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database.channel_gateway import (
    DALTON_CHANNEL_NAMESPACE,
    hash_bind_code,
)
from src.server.database import pool as db_pool
from src.server.database import workspace as workspace_db
from src.server.database.home_workspace import get_home_workspace_id
from src.server.database.runs import handoff_liveness
from src.server.services import channel_settings as cs_mod
from src.server.utils.api import get_current_user_id
from src.tools.messaging import tools as messaging_tools

SERVICE_TOKEN = "s" * 40


def _make_fake_db_from_handler(handler: Any) -> tuple[Any, Any]:
    """Build a `create_autospec`-backed `get_db_connection` context manager from an async SQL handler."""
    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    state: dict[str, Any] = {"rows": [], "idx": 0}

    async def _execute(sql: str, params: Any = None) -> None:
        res = await handler(sql, params)
        if res is None:
            state["rows"] = []
        elif isinstance(res, list):
            state["rows"] = list(res)
        else:
            state["rows"] = [res]
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
    async def _get_db_conn(existing_conn: Any = None):
        yield existing_conn or conn

    mocked_get_db = create_autospec(
        db_pool.get_db_connection, side_effect=_get_db_conn
    )
    return mocked_get_db, cursor


class InMemoryChannelStore:
    """In-memory relational state backing SQL queries in unit tests via `create_autospec` cursor."""

    def __init__(self) -> None:
        self.identities: dict[tuple[str, str], dict[str, Any]] = {}
        self.bind_codes: dict[str, dict[str, Any]] = {}
        self.user_settings: dict[str, dict[str, Any]] = {}
        self.chat_bindings: dict[tuple[str, str], dict[str, Any]] = {}
        self.session_generations: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.thread_watches: dict[str, dict[str, Any]] = {}
        self.inbound_events: dict[tuple[str, str], dict[str, Any]] = {}
        self.background_runs: dict[str, dict[str, Any]] = {}
        self.deliveries: dict[str, dict[str, Any]] = {}
        self.automation_runs: dict[str, dict[str, Any]] = {}
        self.weixin_state: dict[str, Any] | None = None
        self.workspaces: dict[str, dict[str, Any]] = {}
        self.hook_outbox: dict[str, dict[str, Any]] = {}
        self.conversation_responses: dict[str, dict[str, Any]] = {}
        self.conversation_queries: list[dict[str, Any]] = []
        self.advisory_locks: list[str] = []

    async def handle_sql(self, sql: str, params: Any) -> Any:
        norm = " ".join(sql.split())
        now = datetime.now(timezone.utc)

        if "pg_advisory_xact_lock" in norm:
            if params:
                self.advisory_locks.append(str(params[0]))
            return [{"pg_advisory_xact_lock": None}]

        # Workspaces lookup
        if "FROM workspaces WHERE workspace_id = %s" in norm:
            ws_id = str(params[0])
            ws = self.workspaces.get(ws_id)
            if ws and ws.get("status") != "deleted":
                return [dict(ws)]
            return []

        # Inbound events insert (I-1)
        if "INSERT INTO dalton_channel_inbound_events" in norm:
            plat, mid, req_k, ctype, caddr, scope, raw_p = (
                str(params[0]),
                str(params[1]),
                str(params[2]),
                str(params[3]),
                str(params[4]),
                str(params[5]),
                params[6].obj if hasattr(params[6], "obj") else params[6],
            )
            key = (plat, mid)
            if key in self.inbound_events:
                return []
            row = {
                "platform": plat,
                "message_id": mid,
                "request_key": UUID(req_k),
                "retry_request_key": None,
                "resubmitted_request_key": None,
                "steered_into_run_id": None,
                "dalton_user_id": None,
                "chat_type": ctype,
                "chat_address": caddr,
                "thread_scope_key": scope,
                "external_thread_id": None,
                "workspace_id": None,
                "thread_id": None,
                "run_id": None,
                "turn_index": None,
                "status": "pending",
                "fallback_pending": False,
                "sent_to_current_count": 0,
                "raw_payload": dict(raw_p),
                "uploaded_attachments": None,
                "created_at": now,
                "updated_at": now,
            }
            self.inbound_events[key] = row
            return [dict(row)]

        if (
            "SELECT * FROM dalton_channel_inbound_events WHERE platform = %s AND message_id = %s"
            in norm
        ):
            key = (str(params[0]), str(params[1]))
            row = self.inbound_events.get(key)
            return [dict(row)] if row else []

        if "UPDATE dalton_channel_inbound_events" in norm and "WHERE platform = %s AND message_id = %s" in norm:
            plat = str(params[-2])
            mid = str(params[-1])
            row = self.inbound_events.get((plat, mid))
            if not row:
                return []
            if "SET status = 'ignored'" in norm and len(params) == 2:
                row["status"] = "ignored"
            elif "SET dalton_user_id = %s, status = 'ignored'" in norm:
                row["dalton_user_id"] = str(params[0])
                row["status"] = "ignored"
            elif "SET dalton_user_id = %s, workspace_id = %s, external_thread_id = %s" in norm:
                row["dalton_user_id"] = str(params[0])
                row["workspace_id"] = UUID(str(params[1]))
                row["external_thread_id"] = str(params[2])
                if len(params) >= 6:
                    val = params[3].obj if hasattr(params[3], "obj") else params[3]
                    row["raw_payload"] = dict(val)
            elif "SET resubmitted_request_key = %s, status = 'pending_resubmit'" in norm:
                row["resubmitted_request_key"] = UUID(str(params[0]))
                row["status"] = "pending_resubmit"
                row["fallback_pending"] = False
                row["raw_payload"] = (
                    params[1].obj if hasattr(params[1], "obj") else params[1]
                )
            elif "SET status = 'completed', fallback_pending = FALSE, raw_payload = %s" in norm:
                row["status"] = "completed"
                row["fallback_pending"] = False
                row["raw_payload"] = (
                    params[0].obj if hasattr(params[0], "obj") else params[0]
                )
            elif "SET status = %s, fallback_pending = FALSE, raw_payload = %s" in norm:
                row["status"] = str(params[0])
                row["fallback_pending"] = False
                row["raw_payload"] = (
                    params[1].obj if hasattr(params[1], "obj") else params[1]
                )
            else:
                # Generic inbound/progress update
                p_idx = 0
                if "status = %s" in norm:
                    row["status"] = str(params[p_idx])
                    p_idx += 1
                if "thread_id = %s" in norm:
                    row["thread_id"] = UUID(str(params[p_idx])) if params[p_idx] else None
                    p_idx += 1
                if "run_id = %s" in norm:
                    row["run_id"] = str(params[p_idx]) if params[p_idx] else None
                    p_idx += 1
                if "turn_index = %s" in norm:
                    row["turn_index"] = int(params[p_idx])
                    p_idx += 1
                if "request_key = %s" in norm:
                    row["request_key"] = UUID(str(params[p_idx]))
                    p_idx += 1
                if "retry_request_key = %s" in norm:
                    row["retry_request_key"] = UUID(str(params[p_idx]))
                    p_idx += 1
                if "resubmitted_request_key = %s" in norm:
                    row["resubmitted_request_key"] = UUID(str(params[p_idx]))
                    p_idx += 1
                if "steered_into_run_id = %s" in norm:
                    row["steered_into_run_id"] = str(params[p_idx])
                    p_idx += 1
                if "fallback_pending = %s" in norm:
                    row["fallback_pending"] = bool(params[p_idx])
                    p_idx += 1
                if "uploaded_attachments = %s" in norm:
                    val = params[p_idx].obj if hasattr(params[p_idx], "obj") else params[p_idx]
                    row["uploaded_attachments"] = val
                    p_idx += 1
                if "raw_payload = %s" in norm:
                    val = params[p_idx].obj if hasattr(params[p_idx], "obj") else params[p_idx]
                    row["raw_payload"] = dict(val)
                    p_idx += 1
                elif "raw_payload = COALESCE(raw_payload, '{}'::jsonb) || %s" in norm:
                    val = params[p_idx].obj if hasattr(params[p_idx], "obj") else params[p_idx]
                    row["raw_payload"] = {**(row.get("raw_payload") or {}), **dict(val)}
                    p_idx += 1
            row["updated_at"] = now
            return [dict(row)]

        # Identities
        if "SELECT * FROM dalton_channel_identities WHERE platform = %s AND platform_user_id = %s" in norm:
            plat, puid = str(params[0]), str(params[1])
            for row in self.identities.values():
                if row["platform"] == plat and row["platform_user_id"] == puid:
                    return [dict(row)]
            return []

        if "SELECT user_id FROM dalton_channel_identities WHERE platform = %s AND platform_user_id = %s" in norm:
            plat, puid = str(params[0]), str(params[1])
            for row in self.identities.values():
                if row["platform"] == plat and row["platform_user_id"] == puid:
                    return [{"user_id": row["user_id"]}]
            return []

        if "SELECT * FROM dalton_channel_identities WHERE user_id = %s AND platform = %s" in norm:
            uid, plat = str(params[0]), str(params[1])
            row = self.identities.get((uid, plat))
            return [dict(row)] if row else []

        if "SELECT platform_user_id FROM dalton_channel_identities WHERE user_id = %s AND platform = %s" in norm:
            uid, plat = str(params[0]), str(params[1])
            row = self.identities.get((uid, plat))
            return [{"platform_user_id": row["platform_user_id"]}] if row else []

        if "AS has_inbound" in norm and "FROM dalton_channel_inbound_events" in norm:
            plat, addr, puid = (
                str(params[0]),
                str(params[1]),
                str(params[2]),
            )
            ex = any(
                r["platform"] == plat
                and r["chat_address"] == addr
                and (r.get("raw_payload") or {}).get("platform_user_id") == puid
                for r in self.inbound_events.values()
            )
            return [{"has_inbound": ex}]

        if "SELECT weixin_context_token FROM dalton_channel_identities WHERE user_id = %s AND platform = %s" in norm:
            uid, plat = str(params[0]), str(params[1])
            row = self.identities.get((uid, plat))
            return [{"weixin_context_token": row.get("weixin_context_token")}] if row else []

        if "FROM dalton_channel_identities WHERE user_id = %s" in norm and "SELECT" in norm:
            uid = str(params[0])
            return [
                dict(r) for (u, _), r in self.identities.items() if u == uid
            ]

        if "UPDATE dalton_channel_identities SET weixin_context_token = %s" in norm:
            tok, row_id = str(params[0]), str(params[1])
            for row in self.identities.values():
                if str(row["id"]) == row_id:
                    row["weixin_context_token"] = tok
                    row["updated_at"] = now
            return []

        if "DELETE FROM dalton_channel_identities WHERE platform = %s AND platform_user_id = %s AND user_id != %s" in norm:
            plat, puid, uid = str(params[0]), str(params[1]), str(params[2])
            removed = []
            for k in list(self.identities.keys()):
                r = self.identities[k]
                if r["platform"] == plat and r["platform_user_id"] == puid and r["user_id"] != uid:
                    removed.append({"user_id": r["user_id"]})
                    del self.identities[k]
            return removed

        if "DELETE FROM dalton_channel_identities WHERE user_id = %s AND platform = %s" in norm:
            uid, plat = str(params[0]), str(params[1])
            r = self.identities.pop((uid, plat), None)
            return [{"platform_user_id": r["platform_user_id"]}] if r else []

        if "INSERT INTO dalton_channel_identities" in norm:
            uid, plat, puid, uname, tkey, wtok = (
                str(params[0]),
                str(params[1]),
                str(params[2]),
                params[3],
                params[4],
                params[5],
            )
            existing = self.identities.get((uid, plat))
            row = {
                "id": existing["id"] if existing else uuid4(),
                "user_id": uid,
                "platform": plat,
                "platform_user_id": puid,
                "platform_username": uname or (existing or {}).get("platform_username"),
                "platform_tenant_key": tkey or (existing or {}).get("platform_tenant_key"),
                "weixin_context_token": wtok or (existing or {}).get("weixin_context_token"),
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now,
            }
            self.identities[(uid, plat)] = row
            return [dict(row)]

        # Bind codes
        if "UPDATE dalton_channel_bind_codes SET used_at = NOW(), revoked_reason = 'superseded'" in norm:
            uid, plat = str(params[0]), str(params[1])
            for r in self.bind_codes.values():
                if r["user_id"] == uid and r["platform"] == plat and r["used_at"] is None:
                    r["used_at"] = now
                    r["revoked_reason"] = "superseded"
            return []

        if "INSERT INTO dalton_channel_bind_codes" in norm:
            chash, uid, plat, exp = (
                str(params[0]),
                str(params[1]),
                str(params[2]),
                params[3],
            )
            self.bind_codes[chash] = {
                "code_hash": chash,
                "user_id": uid,
                "platform": plat,
                "expires_at": exp,
                "used_at": None,
                "revoked_reason": None,
                "created_at": now,
            }
            return []

        if "UPDATE dalton_channel_bind_codes SET used_at = NOW(), revoked_reason = 'sent_in_group'" in norm:
            chash = str(params[0])
            r = self.bind_codes.get(chash)
            if r and r["used_at"] is None:
                r["used_at"] = now
                r["revoked_reason"] = "sent_in_group"
            return []

        if "FROM dalton_channel_bind_codes WHERE code_hash = %s" in norm and "SELECT" in norm:
            chash = str(params[0])
            r = self.bind_codes.get(chash)
            return [dict(r)] if r else []

        if "UPDATE dalton_channel_bind_codes SET used_at = NOW() WHERE code_hash = %s" in norm:
            chash = str(params[0])
            r = self.bind_codes.get(chash)
            if r:
                r["used_at"] = now
            return []

        # Session generations
        if "INSERT INTO dalton_channel_session_generations" in norm:
            plat, scope, uid = str(params[0]), str(params[1]), str(params[2])
            key = (plat, scope, uid)
            if "ON CONFLICT (platform, scope_key, dalton_user_id) DO NOTHING" in norm:
                if key not in self.session_generations:
                    self.session_generations[key] = {
                        "platform": plat,
                        "scope_key": scope,
                        "dalton_user_id": uid,
                        "generation": 1,
                        "override_workspace_id": None,
                        "pending_interrupt": None,
                        "updated_at": now,
                    }
                return []
            if "override_workspace_id, pending_interrupt, updated_at" in norm:
                ov_ws = params[3]
                if key not in self.session_generations:
                    self.session_generations[key] = {
                        "platform": plat,
                        "scope_key": scope,
                        "dalton_user_id": uid,
                        "generation": 2,
                        "override_workspace_id": ov_ws,
                        "pending_interrupt": None,
                        "updated_at": now,
                    }
                else:
                    self.session_generations[key]["generation"] += 1
                    self.session_generations[key]["override_workspace_id"] = ov_ws
                    self.session_generations[key]["pending_interrupt"] = None
                    self.session_generations[key]["updated_at"] = now
                return [{"generation": self.session_generations[key]["generation"]}]
            if "pending_interrupt = EXCLUDED.pending_interrupt" in norm:
                p_int = params[3].obj if hasattr(params[3], "obj") else params[3]
                if key not in self.session_generations:
                    self.session_generations[key] = {
                        "platform": plat,
                        "scope_key": scope,
                        "dalton_user_id": uid,
                        "generation": 1,
                        "override_workspace_id": None,
                        "pending_interrupt": p_int,
                        "updated_at": now,
                    }
                else:
                    self.session_generations[key]["pending_interrupt"] = p_int
                    self.session_generations[key]["updated_at"] = now
                return []
            # cmd == 'new'
            if key not in self.session_generations:
                self.session_generations[key] = {
                    "platform": plat,
                    "scope_key": scope,
                    "dalton_user_id": uid,
                    "generation": 2,
                    "override_workspace_id": None,
                    "pending_interrupt": None,
                    "updated_at": now,
                }
            else:
                self.session_generations[key]["generation"] += 1
                self.session_generations[key]["pending_interrupt"] = None
                self.session_generations[key]["updated_at"] = now
            return [{"generation": self.session_generations[key]["generation"]}]

        if "UPDATE dalton_channel_session_generations SET generation = generation + 1" in norm:
            if "WHERE platform = %s AND dalton_user_id = %s" in norm:
                plat, uid = str(params[0]), str(params[1])
                updated = []
                for (p, s, u), row in self.session_generations.items():
                    if p == plat and u == uid:
                        row["generation"] += 1
                        row["override_workspace_id"] = None
                        row["pending_interrupt"] = None
                        row["updated_at"] = now
                        updated.append({"scope_key": s, "generation": row["generation"]})
                return updated
            if "scope_key = 'chat:' || %s" in norm:
                plat, cid = str(params[0]), str(params[1])
                prefix = f"chat:{cid}:thread:"
                exact = f"chat:{cid}"
                updated = []
                for (p, s, u), row in self.session_generations.items():
                    if p == plat and (s == exact or s.startswith(prefix)):
                        row["generation"] += 1
                        row["pending_interrupt"] = None
                        row["updated_at"] = now
                        updated.append(
                            {
                                "scope_key": s,
                                "dalton_user_id": u,
                                "generation": row["generation"],
                            }
                        )
                return updated

        if "FROM dalton_channel_session_generations WHERE platform = %s AND scope_key = %s AND dalton_user_id = %s" in norm:
            key = (str(params[0]), str(params[1]), str(params[2]))
            row = self.session_generations.get(key)
            return [dict(row)] if row else []

        # User settings
        if "FROM dalton_channel_user_settings WHERE user_id = %s" in norm:
            uid = str(params[0])
            row = self.user_settings.get(uid)
            return [dict(row)] if row else []

        if "INSERT INTO dalton_channel_user_settings" in norm:
            if "'v1'" in norm:
                uid = str(params[0])
                apps = params[1].obj if hasattr(params[1], "obj") else params[1]
                ver = "v1"
            else:
                uid = str(params[0])
                ver = str(params[1])
                apps = params[2].obj if hasattr(params[2], "obj") else params[2]
            self.user_settings[uid] = {
                "user_id": uid,
                "version": ver,
                "apps": dict(apps),
                "updated_at": now,
            }
            return []

        if "UPDATE dalton_channel_user_settings SET version = %s, apps = %s" in norm:
            ver = str(params[0])
            apps = params[1].obj if hasattr(params[1], "obj") else params[1]
            uid = str(params[2])
            self.user_settings[uid] = {
                "user_id": uid,
                "version": ver,
                "apps": dict(apps),
                "updated_at": now,
            }
            return []

        # Chat bindings
        if "DELETE FROM dalton_channel_chat_bindings WHERE platform = %s AND chat_address = %s" in norm:
            self.chat_bindings.pop((str(params[0]), str(params[1])), None)
            return []

        if "FROM dalton_channel_chat_bindings WHERE platform = %s AND chat_address = %s" in norm:
            key = (str(params[0]), str(params[1]))
            row = self.chat_bindings.get(key)
            return [dict(row)] if row else []

        if "FROM dalton_channel_chat_bindings ORDER BY updated_at DESC" in norm:
            return [dict(r) for r in self.chat_bindings.values()]

        if "INSERT INTO dalton_channel_chat_bindings" in norm:
            plat, addr, cname, ws_id, bound_by = (
                str(params[0]),
                str(params[1]),
                params[2],
                params[3],
                str(params[4]),
            )
            prev = self.chat_bindings.get((plat, addr))
            self.chat_bindings[(plat, addr)] = {
                "platform": plat,
                "chat_address": addr,
                "chat_name": cname or (prev or {}).get("chat_name"),
                "workspace_id": ws_id,
                "bound_by_user_id": bound_by,
                "updated_at": now,
            }
            return []

        # Thread watches
        if "UPDATE dalton_channel_thread_watches SET watch_active = FALSE" in norm:
            uid, plat = str(params[0]), str(params[1])
            for r in self.thread_watches.values():
                if r["dalton_user_id"] == uid and r["platform"] == plat:
                    r["watch_active"] = False
                    r["updated_at"] = now
            return []

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
            self.thread_watches[tid] = {
                "thread_id": UUID(tid),
                "dalton_user_id": uid,
                "platform": plat,
                "target_address": addr,
                "scope_key": scope,
                "generation": gen,
                "last_seen_turn_index": max(
                    last_turn, int((prev or {}).get("last_seen_turn_index") or 0)
                ),
                "watch_active": active,
                "created_at": (prev or {}).get("created_at") or now,
                "updated_at": now,
            }
            return []

        if "UPDATE dalton_channel_thread_watches SET last_seen_turn_index = %s" in norm:
            max_t, tid = int(params[0]), str(params[1])
            if tid in self.thread_watches:
                self.thread_watches[tid]["last_seen_turn_index"] = max_t
            return []

        if "FROM dalton_channel_thread_watches" in norm and "SELECT" in norm:
            if "WHERE thread_id = %s" in norm:
                tid = str(params[0])
                r = self.thread_watches.get(tid)
                return [dict(r)] if r else []
            if "WHERE (%s::uuid IS NULL AND watch_active = TRUE) OR thread_id = %s" in norm:
                tid = str(params[0]) if params[0] else None
                if tid:
                    r = self.thread_watches.get(tid)
                    return [dict(r)] if r else []
                return [
                    dict(r)
                    for r in self.thread_watches.values()
                    if r.get("watch_active")
                ]
            return [
                dict(r)
                for r in self.thread_watches.values()
                if r.get("watch_active")
            ]

        # Background runs
        if "INSERT INTO dalton_channel_background_runs" in norm:
            if "ON CONFLICT (run_id) DO NOTHING" in norm:
                rid, tid, uid, plat, addr, tidx = (
                    str(params[0]),
                    str(params[1]),
                    str(params[2]),
                    str(params[3]),
                    str(params[4]),
                    int(params[5]),
                )
                if rid not in self.background_runs:
                    self.background_runs[rid] = {
                        "run_id": rid,
                        "thread_id": UUID(tid),
                        "dalton_user_id": uid,
                        "platform": plat,
                        "target_address": addr,
                        "turn_index": tidx,
                        "status": "streaming",
                        "fallback_pending": True,
                        "sent_to_current_count": 0,
                        "created_at": now,
                        "updated_at": now,
                    }
                return []
            rid, tid, uid, plat, addr, tidx, st, fb = (
                str(params[0]),
                str(params[1]),
                str(params[2]),
                str(params[3]),
                str(params[4]),
                params[5],
                str(params[6]),
                bool(params[7]),
            )
            prev = self.background_runs.get(rid)
            self.background_runs[rid] = {
                "run_id": rid,
                "thread_id": UUID(tid),
                "dalton_user_id": uid,
                "platform": plat,
                "target_address": addr,
                "turn_index": tidx if tidx is not None else (prev or {}).get("turn_index"),
                "status": st,
                "fallback_pending": fb,
                "sent_to_current_count": (prev or {}).get("sent_to_current_count", 0),
                "created_at": (prev or {}).get("created_at") or now,
                "updated_at": now,
            }
            return []

        if (
            "FROM dalton_channel_background_runs" in norm
            and "SELECT" in norm
            and "SELECT COALESCE(MAX(sent_to_current_count)" not in norm
        ):
            if "WHERE thread_id = %s AND fallback_pending = TRUE" in norm:
                tid = str(params[0])
                return [
                    dict(r)
                    for r in self.background_runs.values()
                    if str(r["thread_id"]) == tid and r.get("fallback_pending")
                ]
            if "WHERE fallback_pending = TRUE" in norm:
                return [
                    dict(r)
                    for r in self.background_runs.values()
                    if r.get("fallback_pending")
                ]
            return [dict(r) for r in self.background_runs.values()]

        # Deliveries
        if "SELECT * FROM dalton_channel_deliveries WHERE delivery_key = %s" in norm:
            dkey = str(params[0])
            r = self.deliveries.get(dkey)
            return [dict(r)] if r else []

        if "INSERT INTO dalton_channel_deliveries" in norm:
            (
                dkey,
                kind,
                uid,
                tid,
                rid,
                exec_id,
                is_cur,
                has_txt,
                plat,
                taddr,
                tchunks,
            ) = params
            self.deliveries[str(dkey)] = {
                "delivery_key": str(dkey),
                "kind": str(kind),
                "user_id": str(uid),
                "thread_id": UUID(str(tid)) if tid else None,
                "run_id": str(rid) if rid else None,
                "execution_id": UUID(str(exec_id)) if exec_id else None,
                "is_current": bool(is_cur),
                "has_non_empty_text": bool(has_txt),
                "platform": str(plat),
                "target_address": str(taddr),
                "status": "in_flight",
                "total_chunks": int(tchunks),
                "sent_chunks": 0,
                "platform_message_ids": [],
                "files_status": [],
                "first_response_json": None,
                "error_code": None,
                "error_message": None,
                "created_at": now,
                "updated_at": now,
            }
            return []

        if "UPDATE dalton_channel_deliveries SET sent_chunks = %s" in norm:
            schunks, tchunks, pmids, fstat, fresp, st, ecode, emsg, dkey = params
            r = self.deliveries[str(dkey)]
            r["sent_chunks"] = int(schunks)
            r["total_chunks"] = int(tchunks)
            r["platform_message_ids"] = pmids.obj if hasattr(pmids, "obj") else pmids
            r["files_status"] = fstat.obj if hasattr(fstat, "obj") else fstat
            r["first_response_json"] = (
                fresp.obj if hasattr(fresp, "obj") else fresp
            )
            r["status"] = str(st)
            if ecode is not None:
                r["error_code"] = str(ecode)
            if emsg is not None:
                r["error_message"] = str(emsg)
            r["updated_at"] = now
            return []

        if "UPDATE dalton_channel_deliveries SET status = 'unknown', error_code = 'gateway_restarted'" in norm:
            for r in self.deliveries.values():
                if r["status"] == "in_flight":
                    r["status"] = "unknown"
                    r["error_code"] = "gateway_restarted"
            return []

        if "UPDATE dalton_channel_inbound_events SET sent_to_current_count = sent_to_current_count + 1" in norm:
            rid = str(params[0])
            for r in self.inbound_events.values():
                if r.get("run_id") == rid:
                    r["sent_to_current_count"] = int(r.get("sent_to_current_count") or 0) + 1
                    return [{"sent_to_current_count": r["sent_to_current_count"]}]
            return []

        if "UPDATE dalton_channel_background_runs SET sent_to_current_count = sent_to_current_count + 1" in norm:
            rid = str(params[0])
            r = self.background_runs.get(rid)
            if r:
                r["sent_to_current_count"] = int(r.get("sent_to_current_count") or 0) + 1
                return [{"sent_to_current_count": r["sent_to_current_count"]}]
            return []

        if "SELECT EXISTS ( SELECT 1 FROM dalton_channel_deliveries WHERE run_id = %s" in norm:
            rid = str(params[0])
            if "status = 'unknown'" in norm:
                ex = any(
                    r.get("run_id") == rid
                    and r.get("is_current")
                    and r.get("has_non_empty_text")
                    and r.get("status") == "unknown"
                    for r in self.deliveries.values()
                )
                return [{"has_unknown_text": ex}]
            ex = any(
                r.get("run_id") == rid
                and r.get("is_current")
                and r.get("has_non_empty_text")
                and r.get("status") == "in_flight"
                for r in self.deliveries.values()
            )
            return [{"in_flight": ex}]

        if "SELECT COALESCE(MAX(sent_to_current_count), 0) AS cnt" in norm:
            rid = str(params[0])
            counts = [
                int(r.get("sent_to_current_count") or 0)
                for r in self.inbound_events.values()
                if r.get("run_id") == rid
            ] + [
                int(r.get("sent_to_current_count") or 0)
                for r in self.background_runs.values()
                if r.get("run_id") == rid
            ]
            return [{"cnt": max(counts) if counts else 0}]

        if "FROM dalton_channel_inbound_events WHERE dalton_user_id = %s" in norm or (
            "SELECT dalton_user_id, platform, chat_address AS target_address" in norm
        ):
            for r in reversed(list(self.inbound_events.values())):
                if r.get("run_id") and r.get("run_id") in params:
                    return [
                        {
                            "dalton_user_id": r["dalton_user_id"],
                            "platform": r["platform"],
                            "target_address": r["chat_address"],
                            "chat_address": r["chat_address"],
                            "thread_id": r.get("thread_id"),
                            "run_id": r.get("run_id"),
                            "external_thread_id": r.get("external_thread_id"),
                        }
                    ]
            return []

        if "SELECT * FROM dalton_channel_inbound_events WHERE status IN" in norm:
            return [
                dict(r)
                for r in self.inbound_events.values()
                if r["status"]
                in (
                    "pending",
                    "accepted",
                    "streaming",
                    "steered",
                    "pending_resubmit",
                )
                or r.get("fallback_pending")
            ]

        # Hook outbox & conversation_responses (steers/reconcile & watch backfill)
        if "FROM hook_outbox WHERE hook_type = 'steer' AND idempotency_key = %s" in norm:
            ikey = str(params[0])
            row = self.hook_outbox.get(ikey)
            return [dict(row)] if row else []

        if "SELECT status FROM conversation_responses WHERE conversation_response_id = %s" in norm:
            rid = str(params[0])
            row = self.conversation_responses.get(rid)
            return [dict(row)] if row else []

        if "FROM conversation_queries q JOIN conversation_responses r" in norm:
            tid, last_t = str(params[0]), int(params[1])
            out = [
                q
                for q in self.conversation_queries
                if str(q["thread_id"]) == tid
                and q["type"] in ("system", "market_target", "wakeup")
                and int(q["turn_index"]) > last_t
            ]
            return out

        # Automation runs (I-8)
        if "SELECT targets FROM dalton_channel_automation_runs WHERE execution_id = %s" in norm:
            r = self.automation_runs.get(str(params[0]))
            return [{"targets": r["targets"]}] if r else []

        if "SELECT * FROM dalton_channel_automation_runs WHERE execution_id = %s" in norm:
            r = self.automation_runs.get(str(params[0]))
            return [dict(r)] if r else []

        if "SELECT * FROM dalton_channel_automation_runs WHERE status = 'finishing'" in norm:
            return [
                dict(r)
                for r in self.automation_runs.values()
                if r.get("status") == "finishing"
            ]

        if "INSERT INTO dalton_channel_automation_runs" in norm:
            exec_id, uid, ws_id, tid, aname, entries, targets = (
                str(params[0]),
                str(params[1]),
                params[2],
                params[3],
                str(params[4]),
                params[5].obj if hasattr(params[5], "obj") else params[5],
                params[6].obj if hasattr(params[6], "obj") else params[6],
            )
            if exec_id not in self.automation_runs:
                self.automation_runs[exec_id] = {
                    "execution_id": UUID(exec_id),
                    "user_id": uid,
                    "workspace_id": UUID(str(ws_id)) if ws_id else None,
                    "thread_id": UUID(str(tid)) if tid else None,
                    "automation_name": aname,
                    "entries": entries,
                    "status": "registered",
                    "finish_status": None,
                    "final_text": None,
                    "targets": targets,
                    "created_at": now,
                    "updated_at": now,
                }
            return [{"targets": self.automation_runs[exec_id]["targets"]}]

        if "SELECT DISTINCT target_address FROM dalton_channel_deliveries WHERE execution_id = %s" in norm:
            exec_id = str(params[0])
            return [
                {"target_address": r["target_address"]}
                for r in self.deliveries.values()
                if str(r.get("execution_id") or "") == exec_id
                and r.get("kind") == "agent_send"
                and r.get("status") in ("sent", "partial")
            ]

        if "UPDATE dalton_channel_automation_runs" in norm:
            f_status, f_text, tid, targets_json, exec_id = params
            r = self.automation_runs[str(exec_id)]
            r["status"] = "finished" if "SET status = 'finished'" in norm else "finishing"
            if f_status is not None:
                r["finish_status"] = str(f_status)
            if f_text is not None:
                r["final_text"] = str(f_text)
            if tid is not None:
                r["thread_id"] = UUID(str(tid))
            r["targets"] = (
                targets_json.obj
                if hasattr(targets_json, "obj")
                else targets_json
            )
            r["updated_at"] = now
            return [dict(r)]

        # Weixin state (I-9)
        if "FROM dalton_channel_weixin_state WHERE singleton_id = 1" in norm:
            if not self.weixin_state:
                return []
            return [dict(self.weixin_state)]

        if "INSERT INTO dalton_channel_weixin_state" in norm:
            bot_id, owner_uid, bot_tok, _key, b_url, buf, st, _, _ = params
            self.weixin_state = {
                "singleton_id": 1,
                "ilink_bot_id": str(bot_id),
                "owner_user_id": str(owner_uid),
                "bot_token": str(bot_tok),
                "base_url": str(b_url or "https://ilinkai.weixin.qq.com"),
                "get_updates_buf": str(buf or ""),
                "status": str(st or "active"),
                "updated_at": now,
            }
            return [dict(self.weixin_state)]

        if "UPDATE dalton_channel_weixin_state" in norm:
            if not self.weixin_state:
                return []
            buf, st, b_url = params
            if buf is not None:
                self.weixin_state["get_updates_buf"] = str(buf)
            if st is not None:
                self.weixin_state["status"] = str(st)
            if b_url is not None:
                self.weixin_state["base_url"] = str(b_url)
            self.weixin_state["updated_at"] = now
            return [dict(self.weixin_state)]

        return []


# ---------------------------------------------------------------------------
# 1. Backend Fix 1: `handoff_liveness.py` Colleague Shared Workspace Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_liveness_colleague_own_run_and_unauthorized_and_external(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify:
    1) Colleague (`u-alice`) finds her own run & steer in `u-bob`'s shared workspace;
    2) Unauthorized user (`u-stranger` or when colleague dispatch is disabled / revoked) cannot find it;
    3) External member (`u-ext`) cannot find a run/steer in a colleague workspace.
    """
    run_id = str(uuid4())
    steer_id = str(uuid4())
    thread_id = str(uuid4())
    ws_id = str(uuid4())
    now = datetime.now(timezone.utc)

    db_run_row = {
        "conversation_response_id": UUID(run_id),
        "conversation_thread_id": UUID(thread_id),
        "status": "in_progress",
        "cancel_requested_at": None,
        "created_at": now,
        "usage_settled_at": None,
        "execution_time": None,
        "workspace_id": UUID(ws_id),
        "thread_user_id": "u-alice",
        "workspace_owner_id": "u-bob",
    }
    db_steer_row = {
        "steer_id": UUID(steer_id),
        "steer_thread_id": UUID(thread_id),
        "steer_workspace_id": UUID(ws_id),
        "steer_thread_user_id": "u-alice",
        "steer_workspace_owner_id": "u-bob",
        "steer_live": True,
        "steer_taker": run_id,
        **db_run_row,
    }

    tiers = {
        "u-alice": "internal",
        "u-bob": "internal",
        "u-stranger": "internal",
        "u-ext": "external",
    }

    async def _fake_get_tier(uid: str, *, email: str | None = None) -> str:
        return tiers.get(uid, "external")

    monkeypatch.setattr(
        dalton_sharing,
        "get_user_tier",
        create_autospec(dalton_sharing.get_user_tier, side_effect=_fake_get_tier),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "is_dalton_supabase_auth_enabled",
        create_autospec(
            dalton_sharing.is_dalton_supabase_auth_enabled, return_value=True
        ),
    )
    is_enabled_mock = create_autospec(
        handoff_liveness.is_colleague_dispatch_enabled, return_value=True
    )
    monkeypatch.setattr(
        handoff_liveness,
        "is_colleague_dispatch_enabled",
        is_enabled_mock,
    )

    async def _fake_require_thread_access(
        tid: str,
        user_id: str | None,
        channel: str = "web",
        need: str = "read",
        **kwargs: Any,
    ) -> str:
        if user_id in ("u-alice", "u-bob") and tiers.get(str(user_id)) != "external":
            return "u-alice"
        raise HTTPException(status_code=403, detail="Forbidden")

    monkeypatch.setattr(
        dalton_sharing,
        "require_thread_access",
        create_autospec(
            dalton_sharing.require_thread_access,
            side_effect=_fake_require_thread_access,
        ),
    )

    async def _sql_handler(sql: str, params: Any) -> Any:
        assert "AND (w.user_id = %s OR COALESCE(ct.user_id, w.user_id) = %s)" in sql
        assert sql.count("%s") == 3
        assert len(params) == 3
        assert params[1] == params[2]
        for line in sql.splitlines():
            if "--" in line:
                assert "%s" not in line.split("--", 1)[1]
        caller = params[1]
        if "FROM hook_outbox" in sql:
            if caller in ("u-alice", "u-bob", "u-ext", "u-stranger"):
                return [dict(db_steer_row)]
            return []
        if caller in ("u-alice", "u-bob", "u-ext", "u-stranger"):
            return [dict(db_run_row)]
        return []

    fake_get_db, _ = _make_fake_db_from_handler(_sql_handler)
    monkeypatch.setattr(handoff_liveness.pool, "get_db_connection", fake_get_db)

    # 1. Colleague u-alice finds her own run & steer in shared workspace
    alice_runs = await handoff_liveness.get_owned_runs([run_id], "u-alice")
    assert run_id in alice_runs
    assert "workspace_owner_id" not in alice_runs[run_id]
    alice_steers = await handoff_liveness.get_owned_steers([steer_id], "u-alice")
    assert steer_id in alice_steers
    assert alice_steers[steer_id]["run_id"] == run_id

    # 1b. Workspace owner u-bob can ALWAYS read a colleague's thread run/steer in his workspace
    #     even when colleague dispatch is disabled (workspace owner read access is never narrowed)
    is_enabled_mock.return_value = False
    bob_runs = await handoff_liveness.get_owned_runs([run_id], "u-bob")
    assert run_id in bob_runs
    bob_steers = await handoff_liveness.get_owned_steers([steer_id], "u-bob")
    assert steer_id in bob_steers
    is_enabled_mock.return_value = True

    # 2. Unauthorized user u-stranger cannot find it
    stranger_runs = await handoff_liveness.get_owned_runs([run_id], "u-stranger")
    assert stranger_runs == {}
    stranger_steers = await handoff_liveness.get_owned_steers([steer_id], "u-stranger")
    assert stranger_steers == {}

    # 3. External member u-ext cannot find it
    ext_runs = await handoff_liveness.get_owned_runs([run_id], "u-ext")
    assert ext_runs == {}
    ext_steers = await handoff_liveness.get_owned_steers([steer_id], "u-ext")
    assert ext_steers == {}


# ---------------------------------------------------------------------------
# 2. Backend Fix 2: `channel_settings.py` Colleague Writable Workspace Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_settings_colleague_writable_vs_readonly_and_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify:
    - Colleague writable workspace (`web_access == 'rw'`) is listed in `available.json` and accepted in `channels.json`;
    - Read-only (`web_access == 'ro'`) or unauthorized workspace is excluded from `available.json` and rejected in `channels.json`.
    """
    own_ws = str(uuid4())
    colleague_rw_ws = str(uuid4())
    colleague_ro_ws = str(uuid4())
    unauthorized_ws = str(uuid4())

    monkeypatch.setattr(
        cs_mod,
        "is_colleague_dispatch_enabled",
        create_autospec(cs_mod.is_colleague_dispatch_enabled, return_value=True),
    )

    async def _fake_get_ws_names(
        uid: str, workspace_ids: Any = None
    ) -> dict[str, str]:
        return {own_ws: "My Own Workspace"}

    monkeypatch.setattr(
        workspace_db,
        "get_workspace_names",
        create_autospec(
            workspace_db.get_workspace_names, side_effect=_fake_get_ws_names
        ),
    )

    async def _fake_list_team_ws(uid: str) -> list[dict[str, Any]]:
        return [
            {
                "user_id": "u-bob",
                "workspaces": [
                    {
                        "workspace_id": colleague_rw_ws,
                        "user_id": "u-bob",
                        "name": "Bob RW Workspace",
                        "web_access": "rw",
                        "access": "rw",
                    },
                    {
                        "workspace_id": colleague_ro_ws,
                        "user_id": "u-bob",
                        "name": "Bob RO Workspace",
                        "web_access": "ro",
                        "access": "ro",
                    },
                ],
            }
        ]

    async def _fake_require_ws_access(
        workspace_id: Any,
        user_id: str | None,
        channel: str = "web",
        need: str = "read",
        **kwargs: Any,
    ) -> dict[str, Any]:
        wid = (
            workspace_id.get("workspace_id")
            if isinstance(workspace_id, dict)
            else str(workspace_id)
        )
        if wid == colleague_rw_ws and need == "write":
            return {"workspace_id": colleague_rw_ws, "name": "Bob RW Workspace"}
        raise HTTPException(status_code=403, detail="Forbidden")

    monkeypatch.setattr(
        dalton_sharing,
        "list_team_workspaces",
        create_autospec(
            dalton_sharing.list_team_workspaces, side_effect=_fake_list_team_ws
        ),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "require_workspace_access",
        create_autospec(
            dalton_sharing.require_workspace_access,
            side_effect=_fake_require_ws_access,
        ),
    )

    async def _fake_gw_req(
        method: str, path: str, *, user_id: str, **kwargs: Any
    ) -> messaging_tools.GatewayAnswer:
        if path == "/agent/settings/available":
            return messaging_tools.GatewayAnswer(
                status=200,
                data={
                    "apps": {
                        "feishu": {
                            "chats": [
                                {
                                    "address": "feishu:oc_1",
                                    "name": "Group 1",
                                    "kind": "channel",
                                }
                            ],
                            "complete": True,
                            "error": None,
                        }
                    }
                },
                text="",
            )
        return messaging_tools.GatewayAnswer(
            status=200,
            data={
                "version": "v1",
                "settings": {
                    "feishu": {
                        "preferred": "feishu:@me",
                        "chats": {},
                        "automation_output": {},
                        "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
                    }
                },
            },
            text="",
        )

    monkeypatch.setattr(
        messaging_tools,
        "gateway_request",
        create_autospec(messaging_tools.gateway_request, side_effect=_fake_gw_req),
    )

    # 1. available.json includes own_ws and colleague_rw_ws, excludes colleague_ro_ws
    choices = await cs_mod.CHANNEL_FILES[cs_mod.AVAILABLE_FILE].fetch("u-alice")
    assert own_ws in choices.workspaces
    assert colleague_rw_ws in choices.workspaces
    assert choices.workspaces[colleague_rw_ws] == "Bob RW Workspace"
    assert colleague_ro_ws not in choices.workspaces

    # 2. channels.json parse + plan accepts colleague_rw_ws
    channels_file = cs_mod.CHANNEL_FILES[cs_mod.CHANNELS_FILE]
    snapshot = await channels_file.fetch("u-alice")
    call_ctx = CallContext(workspace_id=own_ws, thread_id=None, timezone="UTC")

    valid_json = cs_mod.serialize_json(
        {
            "feishu": {
                "preferred": "feishu:@me",
                "chats": {"feishu:oc_1": {"workspace_id": colleague_rw_ws}},
                "automation_output": {colleague_rw_ws: "feishu:oc_1"},
                "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
            }
        }
    )
    parsed_ok = await channels_file.parse("u-alice", call_ctx, valid_json, "v1")
    assert parsed_ok.unknown == []
    plan_ok = channels_file.plan(call_ctx, parsed_ok, snapshot)
    assert plan_ok.changes is not None

    # 3. channels.json parse + plan rejects colleague_ro_ws and unauthorized_ws
    for bad_ws in (colleague_ro_ws, unauthorized_ws):
        bad_json = cs_mod.serialize_json(
            {
                "feishu": {
                    "preferred": "feishu:@me",
                    "chats": {"feishu:oc_1": {"workspace_id": bad_ws}},
                    "automation_output": {},
                    "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
                }
            }
        )
        parsed_bad = await channels_file.parse("u-alice", call_ctx, bad_json, "v1")
        assert len(parsed_bad.unknown) == 1
        with pytest.raises(UserDataValidationError):
            channels_file.plan(call_ctx, parsed_bad, snapshot)


# ---------------------------------------------------------------------------
# 3. Full API Contract Tests for I-1 ~ I-9 & §1.5 via FastAPI TestClient
# ---------------------------------------------------------------------------


@pytest.fixture
def channel_test_env(monkeypatch: pytest.MonkeyPatch):
    store = InMemoryChannelStore()
    fake_get_db, _ = _make_fake_db_from_handler(store.handle_sql)
    monkeypatch.setattr(cg_db, "get_db_connection", fake_get_db)
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("BYOK_ENCRYPTION_KEY", "test-byok-secret-key-32chars!!")
    monkeypatch.setattr(
        cg_db,
        "is_colleague_dispatch_enabled",
        create_autospec(cg_db.is_colleague_dispatch_enabled, return_value=True),
    )

    tiers: dict[str, str] = {
        "u-admin": "admin",
        "u-alice": "internal",
        "u-bob": "internal",
        "u-ext": "external",
    }

    async def _fake_tier(uid: str, *, email: str | None = None) -> str:
        return tiers.get(uid, "internal")

    monkeypatch.setattr(
        cg_db,
        "get_user_tier",
        create_autospec(cg_db.get_user_tier, side_effect=_fake_tier),
    )
    monkeypatch.setattr(
        ch_router_mod,
        "get_user_tier",
        create_autospec(ch_router_mod.get_user_tier, side_effect=_fake_tier),
    )

    writable_by_user: dict[str, dict[str, str]] = {}

    async def _fake_writable_names(
        uid: str, workspace_ids: Any = None
    ) -> dict[str, str]:
        return dict(writable_by_user.get(uid, {}))

    monkeypatch.setattr(
        cg_db,
        "_get_writable_workspace_names",
        create_autospec(
            cg_db._get_writable_workspace_names, side_effect=_fake_writable_names
        ),
    )

    async def _fake_require_ws_access(
        workspace_id: Any,
        user_id: str | None,
        channel: str = "web",
        need: str = "read",
        **kwargs: Any,
    ) -> dict[str, Any]:
        wid = (
            str(workspace_id.get("workspace_id"))
            if isinstance(workspace_id, dict)
            else str(workspace_id)
        )
        allowed = writable_by_user.get(str(user_id), {})
        if wid in allowed:
            return {"workspace_id": wid, "name": allowed[wid]}
        raise HTTPException(status_code=403, detail="Forbidden")

    monkeypatch.setattr(
        dalton_sharing,
        "require_workspace_access",
        create_autospec(
            dalton_sharing.require_workspace_access,
            side_effect=_fake_require_ws_access,
        ),
    )

    monkeypatch.setattr(
        cg_db,
        "lookup_thread_by_external_id",
        create_autospec(cg_db.lookup_thread_by_external_id, return_value=None),
    )

    app = FastAPI()
    current_web_user = {"user_id": "u-alice"}
    app.dependency_overrides[get_current_user_id] = lambda: current_web_user["user_id"]
    app.include_router(dalton_router)

    client = TestClient(app)
    return {
        "client": client,
        "store": store,
        "tiers": tiers,
        "writable_by_user": writable_by_user,
        "current_web_user": current_web_user,
        "headers": {"X-Service-Token": SERVICE_TOKEN},
    }


def test_internal_endpoints_reject_missing_or_invalid_service_token(
    channel_test_env: dict[str, Any],
) -> None:
    client: TestClient = channel_test_env["client"]
    for bad_headers in ({}, {"X-Service-Token": "short"}, {"X-Service-Token": "x" * 40}):
        r = client.get("/api/v1/dalton/channels/internal/recover", headers=bad_headers)
        assert r.status_code == 401


def test_bind_code_group_leak_revocation_dm_platform_mismatch_and_unbind_generation(
    channel_test_env: dict[str, Any],
) -> None:
    """Verify I-2 & §1.5:
    1) Bind code appearing in group chat is immediately revoked across platforms;
    2) DM cross-platform bind attempt is rejected with `platform_mismatch` WITHOUT revoking the code;
    3) Subsequent DM bind on the expected platform succeeds;
    4) Unbind increments `generation` by 1 and does not delete the session_generations row;
    5) Re-binding advances `generation` again so old `:gen:1` is never reused.
    """
    client: TestClient = channel_test_env["client"]
    headers = channel_test_env["headers"]
    store: InMemoryChannelStore = channel_test_env["store"]

    # 1. Create a Feishu bind code and leak it in a Discord group chat -> revoked!
    r1 = client.post(
        "/api/v1/dalton/channels/bind-codes", json={"platform": "feishu"}
    )
    assert r1.status_code == 200
    code1 = r1.json()["code"]
    assert code1.startswith("DLT-")

    r_leak = client.post(
        "/api/v1/dalton/channels/internal/bind-code/consume",
        headers=headers,
        json={
            "action": "bind",
            "platform": "discord",
            "platform_user_id": "d_user_1",
            "code": code1.lower(),
            "is_group": True,
        },
    )
    assert r_leak.status_code == 200
    assert r_leak.json() == {"ok": False, "reason": "revoked_in_group"}
    assert store.bind_codes[hash_bind_code(code1)]["revoked_reason"] == "sent_in_group"

    # Attempting to use leaked code1 in Feishu DM now fails
    r_after_leak = client.post(
        "/api/v1/dalton/channels/internal/bind-code/consume",
        headers=headers,
        json={
            "action": "bind",
            "platform": "feishu",
            "platform_user_id": "ou_alice",
            "code": code1,
            "is_group": False,
        },
    )
    assert r_after_leak.json() == {"ok": False, "reason": "invalid_or_expired"}

    # 2. Create a second Feishu bind code; try consuming in Discord DM -> platform_mismatch, NOT revoked
    r2 = client.post(
        "/api/v1/dalton/channels/bind-codes", json={"platform": "feishu"}
    )
    code2 = r2.json()["code"]

    r_mismatch = client.post(
        "/api/v1/dalton/channels/internal/bind-code/consume",
        headers=headers,
        json={
            "action": "bind",
            "platform": "discord",
            "platform_user_id": "d_user_1",
            "code": code2,
            "is_group": False,
        },
    )
    assert r_mismatch.status_code == 200
    assert r_mismatch.json() == {
        "ok": False,
        "reason": "platform_mismatch",
        "expected_platform": "feishu",
    }
    assert store.bind_codes[hash_bind_code(code2)]["used_at"] is None

    # 3. Consume code2 in Feishu DM -> succeeds at generation 1
    r_bind = client.post(
        "/api/v1/dalton/channels/internal/bind-code/consume",
        headers=headers,
        json={
            "action": "bind",
            "platform": "feishu",
            "platform_user_id": "ou_alice",
            "platform_username": "Alice",
            "code": code2,
            "is_group": False,
        },
    )
    assert r_bind.status_code == 200
    assert r_bind.json() == {
        "ok": True,
        "user_id": "u-alice",
        "platform": "feishu",
        "platform_user_id": "ou_alice",
        "generation": 1,
    }

    # 4. Unbind via Web DELETE /bindings/feishu -> generation increments to 2
    r_unbind = client.delete("/api/v1/dalton/channels/bindings/feishu")
    assert r_unbind.status_code == 200
    assert r_unbind.json()["ok"] is True
    assert r_unbind.json()["generations"]["dm:ou_alice"] == 2
    assert ("feishu", "dm:ou_alice", "u-alice") in store.session_generations

    # 5. Re-bind Feishu -> generation advances to 3 (never falls back to 1)
    r3 = client.post(
        "/api/v1/dalton/channels/bind-codes", json={"platform": "feishu"}
    )
    code3 = r3.json()["code"]
    r_rebind = client.post(
        "/api/v1/dalton/channels/internal/bind-code/consume",
        headers=headers,
        json={
            "action": "bind",
            "platform": "feishu",
            "platform_user_id": "ou_alice",
            "code": code3,
            "is_group": False,
        },
    )
    assert r_rebind.json()["ok"] is True
    assert r_rebind.json()["generation"] == 3


def test_inbound_register_resolve_and_session_commands_and_state_and_settings(
    channel_test_env: dict[str, Any],
) -> None:
    """Verify I-1, I-3, I-4, I-5 end-to-end:
    - I-1 unlinked -> ignored / not_linked;
    - I-1 external member in group -> ignored / external_member_group_forbidden;
    - I-1 DM & Group resolution, idempotent replay;
    - I-3 `/workspace`, `/new`, `/stop` generation advancement;
    - I-4 state query;
    - I-5 settings validation (`problems[].field`), version conflict (`409`), and group workspace switch generation advancement.
    """
    client: TestClient = channel_test_env["client"]
    headers = channel_test_env["headers"]
    store: InMemoryChannelStore = channel_test_env["store"]
    writable_by_user = channel_test_env["writable_by_user"]

    ws_shared = str(uuid4())
    ws_readonly = str(uuid4())
    store.workspaces[ws_shared] = {
        "workspace_id": UUID(ws_shared),
        "user_id": "u-bob",
        "name": "Semiconductor Shared",
        "status": "running",
        "is_home": False,
    }
    writable_by_user["u-alice"] = {ws_shared: "Semiconductor Shared"}

    # 1. Unlinked user inbound -> not_linked
    r_unlinked = client.post(
        "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
        headers=headers,
        json={
            "platform": "feishu",
            "platform_user_id": "ou_unknown",
            "chat_type": "dm",
            "chat_address": "feishu:@me",
            "thread_scope_key": "dm:ou_unknown",
            "message_id": "om_unlinked_1",
        },
    )
    assert r_unlinked.status_code == 200
    assert r_unlinked.json()["linked"] is False
    assert r_unlinked.json()["allowed"] is False
    assert r_unlinked.json()["deny_reason"] == "not_linked"

    # Bind u-alice and u-ext on feishu
    for uid, puid in (("u-alice", "ou_alice"), ("u-ext", "ou_ext")):
        channel_test_env["current_web_user"]["user_id"] = uid
        code = client.post(
            "/api/v1/dalton/channels/bind-codes", json={"platform": "feishu"}
        ).json()["code"]
        client.post(
            "/api/v1/dalton/channels/internal/bind-code/consume",
            headers=headers,
            json={
                "action": "bind",
                "platform": "feishu",
                "platform_user_id": puid,
                "code": code,
                "is_group": False,
            },
        )
    channel_test_env["current_web_user"]["user_id"] = "u-alice"

    # 2. External member in group chat -> external_member_group_forbidden
    r_ext_grp = client.post(
        "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
        headers=headers,
        json={
            "platform": "feishu",
            "platform_user_id": "ou_ext",
            "chat_type": "group",
            "chat_address": "feishu:oc_semi",
            "thread_scope_key": "chat:oc_semi",
            "message_id": "om_ext_grp_1",
        },
    )
    assert r_ext_grp.status_code == 200
    assert r_ext_grp.json()["linked"] is True
    assert r_ext_grp.json()["allowed"] is False
    assert r_ext_grp.json()["deny_reason"] == "external_member_group_forbidden"

    # 3. u-alice DM inbound -> resolves to Home workspace, gen 1, and replays idempotently
    r_dm1 = client.post(
        "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
        headers=headers,
        json={
            "platform": "feishu",
            "platform_user_id": "ou_alice",
            "chat_type": "dm",
            "chat_address": "feishu:@me",
            "thread_scope_key": "dm:ou_alice",
            "message_id": "om_alice_dm_1",
        },
    )
    assert r_dm1.status_code == 200
    dm1 = r_dm1.json()
    assert dm1["existing_status"] is None
    assert dm1["allowed"] is True
    assert dm1["workspace_id"] == get_home_workspace_id("u-alice")
    assert dm1["external_thread_id"] == "feishu:dm:ou_alice:gen:1"

    # Idempotent replay of om_alice_dm_1
    r_dm1_replay = client.post(
        "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
        headers=headers,
        json={
            "platform": "feishu",
            "platform_user_id": "ou_alice",
            "chat_type": "dm",
            "chat_address": "feishu:@me",
            "thread_scope_key": "dm:ou_alice",
            "message_id": "om_alice_dm_1",
        },
    )
    assert r_dm1_replay.json()["existing_status"] == "pending"
    assert r_dm1_replay.json()["request_key"] == dm1["request_key"]

    # 3b. u-alice group inbound in feishu:oc_semi -> establishes group membership in dalton_channel_inbound_events
    r_grp1 = client.post(
        "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
        headers=headers,
        json={
            "platform": "feishu",
            "platform_user_id": "ou_alice",
            "chat_type": "group",
            "chat_address": "feishu:oc_semi",
            "thread_scope_key": "chat:oc_semi",
            "message_id": "om_alice_grp_1",
        },
    )
    assert r_grp1.status_code == 200
    assert r_grp1.json()["allowed"] is True
    assert "dalton_group_chat:feishu:feishu:oc_semi" in store.advisory_locks

    # 4. I-3 session commands: workspace_list, workspace_switch (DM & Group), new, stop
    r_wlist = client.post(
        "/api/v1/dalton/channels/internal/session/command",
        headers=headers,
        json={
            "cmd": "workspace_list",
            "platform": "feishu",
            "dalton_user_id": "u-alice",
            "chat_type": "dm",
            "scope_key": "dm:ou_alice",
        },
    )
    assert r_wlist.status_code == 200
    assert r_wlist.json()["ok"] is True
    assert any(
        w["workspace_id"] == ws_shared for w in r_wlist.json()["workspaces"]
    )

    r_wswitch_dm = client.post(
        "/api/v1/dalton/channels/internal/session/command",
        headers=headers,
        json={
            "cmd": "workspace_switch",
            "platform": "feishu",
            "dalton_user_id": "u-alice",
            "chat_type": "dm",
            "scope_key": "dm:ou_alice",
            "workspace_id": ws_shared,
        },
    )
    assert r_wswitch_dm.json()["ok"] is True
    assert r_wswitch_dm.json()["generation"] == 2
    assert r_wswitch_dm.json()["external_thread_id"] == "feishu:dm:ou_alice:gen:2"

    # Seed group sessions for u-alice and u-bob in chat:oc_semi and chat:oc_semi:thread:t1
    store.session_generations[("feishu", "chat:oc_semi", "u-alice")] = {
        "platform": "feishu",
        "scope_key": "chat:oc_semi",
        "dalton_user_id": "u-alice",
        "generation": 1,
        "override_workspace_id": None,
        "pending_interrupt": {"interrupt_id": "int_1"},
        "updated_at": datetime.now(timezone.utc),
    }
    store.session_generations[("feishu", "chat:oc_semi:thread:t1", "u-bob")] = {
        "platform": "feishu",
        "scope_key": "chat:oc_semi:thread:t1",
        "dalton_user_id": "u-bob",
        "generation": 4,
        "override_workspace_id": None,
        "pending_interrupt": {"interrupt_id": "int_2"},
        "updated_at": datetime.now(timezone.utc),
    }

    r_wswitch_grp = client.post(
        "/api/v1/dalton/channels/internal/session/command",
        headers=headers,
        json={
            "cmd": "workspace_switch",
            "platform": "feishu",
            "dalton_user_id": "u-alice",
            "chat_type": "group",
            "chat_address": "feishu:oc_semi",
            "chat_name": "半导体研究群",
            "scope_key": "chat:oc_semi",
            "workspace_id": ws_shared,
        },
    )
    assert r_wswitch_grp.json()["ok"] is True
    assert r_wswitch_grp.json()["generation"] == 2
    assert (
        store.session_generations[("feishu", "chat:oc_semi:thread:t1", "u-bob")][
            "generation"
        ]
        == 5
    )
    assert (
        store.session_generations[("feishu", "chat:oc_semi:thread:t1", "u-bob")][
            "pending_interrupt"
        ]
        is None
    )

    # I-3 /new advances only operator's row
    r_new = client.post(
        "/api/v1/dalton/channels/internal/session/command",
        headers=headers,
        json={
            "cmd": "new",
            "platform": "feishu",
            "dalton_user_id": "u-alice",
            "chat_type": "group",
            "scope_key": "chat:oc_semi",
        },
    )
    assert r_new.json()["ok"] is True
    assert r_new.json()["generation"] == 3
    assert (
        store.session_generations[("feishu", "chat:oc_semi:thread:t1", "u-bob")][
            "generation"
        ]
        == 5
    )

    # 5. I-5 PUT /internal/settings validation & CAS
    # External member group chat forbidden -> 400 with field
    r_ext_set = client.put(
        "/api/v1/dalton/channels/internal/settings",
        headers=headers,
        json={
            "user_id": "u-ext",
            "version": "v1",
            "settings": {
                "feishu": {
                    "preferred": "feishu:oc_semi",
                    "chats": {"feishu:oc_semi": {"workspace_id": None}},
                    "automation_output": {},
                    "agent_messages": {
                        "enabled": True,
                        "allowed": ["feishu:@me", "feishu:oc_semi"],
                    },
                }
            },
        },
    )
    assert r_ext_set.status_code == 400
    assert r_ext_set.json()["code"] == "invalid"
    assert any("field" in p for p in r_ext_set.json()["problems"])

    # Readonly/unauthorized workspace -> 400
    r_ro_set = client.put(
        "/api/v1/dalton/channels/internal/settings",
        headers=headers,
        json={
            "user_id": "u-alice",
            "version": "v1",
            "settings": {
                "feishu": {
                    "preferred": "feishu:@me",
                    "chats": {"feishu:oc_semi": {"workspace_id": ws_readonly}},
                    "automation_output": {},
                    "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
                }
            },
        },
    )
    assert r_ro_set.status_code == 400
    assert r_ro_set.json()["problems"][0]["field"] == 'feishu.chats["feishu:oc_semi"].workspace_id'

    # Valid save with writable colleague workspace -> 200, version v2, non-empty changes
    r_ok_set = client.put(
        "/api/v1/dalton/channels/internal/settings",
        headers=headers,
        json={
            "user_id": "u-alice",
            "version": "v1",
            "settings": {
                "feishu": {
                    "preferred": "feishu:@me",
                    "chats": {
                        "feishu:oc_semi": {
                            "workspace_id": ws_shared,
                            "name": "半导体研究群",
                        }
                    },
                    "automation_output": {ws_shared: "feishu:oc_semi"},
                    "agent_messages": {
                        "enabled": True,
                        "allowed": ["feishu:@me", "feishu:oc_semi"],
                    },
                }
            },
        },
    )
    assert r_ok_set.status_code == 200
    assert r_ok_set.json()["version"] == "v2"
    assert len(r_ok_set.json()["changes"]) >= 1

    # Stale version -> 409 version_conflict
    r_conflict = client.put(
        "/api/v1/dalton/channels/internal/settings",
        headers=headers,
        json={
            "user_id": "u-alice",
            "version": "v1",
            "settings": r_ok_set.json()["settings"],
        },
    )
    assert r_conflict.status_code == 409
    assert r_conflict.json()["code"] == "version_conflict"

    # Clearing automation_output via address=null deletes the key and returns {address: null, name: null}
    r_clear_auto = client.put(
        "/api/v1/dalton/channels/internal/settings",
        headers=headers,
        json={
            "user_id": "u-alice",
            "workspace_id": ws_shared,
            "platform": "feishu",
            "address": None,
        },
    )
    assert r_clear_auto.status_code == 200
    assert r_clear_auto.json()["address"] is None
    assert r_clear_auto.json()["name"] is None
    assert (
        ws_shared
        not in r_clear_auto.json()["settings"]["feishu"]["automation_output"]
    )


def test_outbound_deliveries_progress_steers_recover_automation_and_weixin(
    channel_test_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify I-6, I-7, I-8, I-9, and §1.5 Weixin Admin QR endpoints."""
    client: TestClient = channel_test_env["client"]
    headers = channel_test_env["headers"]
    store: InMemoryChannelStore = channel_test_env["store"]
    writable_by_user = channel_test_env["writable_by_user"]

    ws_id = str(uuid4())
    thread_id = str(uuid4())
    run_id = str(uuid4())
    exec_id = str(uuid4())
    writable_by_user["u-alice"] = {ws_id: "Alice Workspace"}
    store.workspaces[ws_id] = {
        "workspace_id": UUID(ws_id),
        "user_id": "u-alice",
        "name": "Alice Workspace",
        "status": "running",
        "is_home": False,
    }

    # Bind u-alice on feishu
    code = client.post(
        "/api/v1/dalton/channels/bind-codes", json={"platform": "feishu"}
    ).json()["code"]
    client.post(
        "/api/v1/dalton/channels/internal/bind-code/consume",
        headers=headers,
        json={
            "action": "bind",
            "platform": "feishu",
            "platform_user_id": "ou_alice",
            "platform_username": "Alice",
            "code": code,
            "is_group": False,
        },
    )

    # Register inbound event & progress with thread_id, run_id, turn_index
    r_in = client.post(
        "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
        headers=headers,
        json={
            "platform": "feishu",
            "platform_user_id": "ou_alice",
            "chat_type": "dm",
            "chat_address": "feishu:@me",
            "thread_scope_key": "dm:ou_alice",
            "message_id": "om_run_1",
        },
    )
    assert r_in.json()["allowed"] is True

    client.post(
        "/api/v1/dalton/channels/internal/inbound/progress",
        headers=headers,
        json={
            "platform": "feishu",
            "message_id": "om_run_1",
            "status": "streaming",
            "thread_id": thread_id,
            "run_id": run_id,
            "turn_index": 1,
            "fallback_pending": True,
            "thread_watch": {
                "thread_id": thread_id,
                "dalton_user_id": "u-alice",
                "platform": "feishu",
                "target_address": "feishu:@me",
                "scope_key": "dm:ou_alice",
                "generation": 1,
                "last_seen_turn_index": 1,
                "watch_active": True,
            },
        },
    )

    # I-6: Claim delivery for current turn (Scenario A2)
    dkey = f"tool:{run_id}:call_1"
    r_claim = client.post(
        "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
        headers=headers,
        json={
            "delivery_key": dkey,
            "kind": "agent_send",
            "user_id": "u-alice",
            "thread_id": thread_id,
            "run_id": run_id,
            "platform": "feishu",
            "target": None,
            "has_non_empty_text": True,
            "total_chunks": 1,
        },
    )
    assert r_claim.status_code == 200
    assert r_claim.json()["claim_status"] == "claimed"
    assert r_claim.json()["is_current"] is True

    # Duplicate claim while in_flight -> in_flight_or_unknown
    r_claim_dup = client.post(
        "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
        headers=headers,
        json={
            "delivery_key": dkey,
            "kind": "agent_send",
            "user_id": "u-alice",
            "thread_id": thread_id,
            "run_id": run_id,
        },
    )
    assert r_claim_dup.json()["claim_status"] == "in_flight_or_unknown"
    assert r_claim_dup.json()["code"] == "in_flight"

    # I-7 deliveries/progress -> marks sent and increments sent_to_current_count to 1
    first_resp = {"status": "sent", "address": "feishu:@me", "current": True}
    r_dprog = client.post(
        "/api/v1/dalton/channels/internal/deliveries/progress",
        headers=headers,
        json={
            "delivery_key": dkey,
            "sent_chunks": 1,
            "total_chunks": 1,
            "platform_message_ids": ["om_out_1"],
            "first_response_json": first_resp,
            "status": "sent",
        },
    )
    assert r_dprog.json()["ok"] is True
    assert r_dprog.json()["sent_to_current_count"] == 1

    # Duplicate claim after sent -> already_completed with first_response_json
    r_claim_done = client.post(
        "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
        headers=headers,
        json={"delivery_key": dkey, "kind": "agent_send", "user_id": "u-alice"},
    )
    assert r_claim_done.json()["claim_status"] == "already_completed"
    assert r_claim_done.json()["first_response_json"] == first_resp

    # Scenario C: Proactive send to target not in agent_messages.allowed -> denied (not_allowed)
    r_proactive_denied = client.post(
        "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
        headers=headers,
        json={
            "delivery_key": "tool:proactive:1",
            "kind": "agent_send",
            "user_id": "u-alice",
            "target": "feishu:oc_unlisted",
        },
    )
    assert r_proactive_denied.json()["claim_status"] == "denied"
    assert r_proactive_denied.json()["code"] == "not_allowed"

    # I-7 steers/reconcile: returned steer when run completed -> pending_resubmit with :returned: UUIDv5
    steer_msg_id = "om_steer_1"
    steer_req_key = str(
        uuid5(DALTON_CHANNEL_NAMESPACE, f"feishu:{steer_msg_id}")
    )
    store.inbound_events[("feishu", steer_msg_id)] = {
        "platform": "feishu",
        "message_id": steer_msg_id,
        "request_key": UUID(steer_req_key),
        "retry_request_key": None,
        "resubmitted_request_key": None,
        "steered_into_run_id": run_id,
        "dalton_user_id": "u-alice",
        "chat_type": "dm",
        "chat_address": "feishu:@me",
        "thread_scope_key": "dm:ou_alice",
        "external_thread_id": "feishu:dm:ou_alice:gen:1",
        "workspace_id": UUID(ws_id),
        "thread_id": UUID(thread_id),
        "run_id": None,
        "turn_index": None,
        "status": "steered",
        "fallback_pending": False,
        "sent_to_current_count": 0,
        "raw_payload": {},
        "uploaded_attachments": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    store.hook_outbox[f"steer:{steer_req_key}"] = {
        "status": "done",
        "payload": {"outcome": "returned", "returned_run_id": run_id},
    }
    store.conversation_responses[run_id] = {"status": "completed"}

    r_rec = client.post(
        "/api/v1/dalton/channels/internal/steers/reconcile",
        headers=headers,
        json={"platform": "feishu", "message_id": steer_msg_id, "run_id": run_id},
    )
    assert r_rec.status_code == 200
    expected_resub = str(
        uuid5(DALTON_CHANNEL_NAMESPACE, f"feishu:{steer_msg_id}:returned:{run_id}")
    )
    assert r_rec.json()["reconcile_status"] == "pending_resubmit"
    assert r_rec.json()["resubmitted_request_key"] == expected_resub

    # I-7 recover: watch_reconnect vs startup
    bg_wakeup_run_id = str(uuid4())
    store.conversation_queries.append(
        {
            "thread_id": UUID(thread_id),
            "turn_index": 2,
            "type": "wakeup",
            "run_id": UUID(bg_wakeup_run_id),
        }
    )
    store.deliveries["in_flight_keep"] = {
        "delivery_key": "in_flight_keep",
        "status": "in_flight",
    }

    r_watch_rec = client.get(
        f"/api/v1/dalton/channels/internal/recover?mode=watch_reconnect&thread_id={thread_id}",
        headers=headers,
    )
    assert r_watch_rec.status_code == 200
    assert store.deliveries["in_flight_keep"]["status"] == "in_flight"
    assert any(
        b["run_id"] == bg_wakeup_run_id
        for b in r_watch_rec.json()["background_runs"]
    )

    r_startup_rec = client.get(
        "/api/v1/dalton/channels/internal/recover?mode=startup",
        headers=headers,
    )
    assert r_startup_rec.status_code == 200
    assert store.deliveries["in_flight_keep"]["status"] == "unknown"
    assert store.deliveries["in_flight_keep"]["error_code"] == "gateway_restarted"

    # I-8: Automation runs register & finish
    r_auto_reg = client.post(
        "/api/v1/dalton/channels/internal/automation-runs",
        headers=headers,
        json={
            "execution_id": exec_id,
            "user_id": "u-alice",
            "workspace_id": ws_id,
            "automation_name": "每日复盘",
            "entries": ["feishu"],
        },
    )
    assert r_auto_reg.status_code == 200
    assert r_auto_reg.json()["targets"] == [
        {
            "entry": "feishu",
            "address": "feishu:@me",
            "name": "Alice (feishu DM)",
            "ok": True,
            "message": None,
        }
    ]

    r_auto_404 = client.post(
        f"/api/v1/dalton/channels/internal/automation-runs/{uuid4()}/finish",
        headers=headers,
        json={"status": "completed", "final_text": "Report"},
    )
    assert r_auto_404.status_code == 404

    r_auto_fin1 = client.post(
        f"/api/v1/dalton/channels/internal/automation-runs/{exec_id}/finish",
        headers=headers,
        json={"status": "completed", "final_text": "Report"},
    )
    assert r_auto_fin1.status_code == 200
    assert r_auto_fin1.json()["status"] == "finishing"

    r_auto_fin2 = client.post(
        f"/api/v1/dalton/channels/internal/automation-runs/{exec_id}/finish",
        headers=headers,
        json={
            "status": "completed",
            "mark_finished": True,
            "targets": [
                {
                    "entry": "feishu",
                    "address": "feishu:@me",
                    "name": "Alice (feishu DM)",
                    "reached": True,
                    "via": "fallback",
                    "error": None,
                }
            ],
        },
    )
    assert r_auto_fin2.json()["status"] == "finished"

    # I-9: Weixin state GET & PUT
    r_wx_empty = client.get(
        "/api/v1/dalton/channels/internal/weixin-state", headers=headers
    )
    assert r_wx_empty.json()["configured"] is False

    r_wx_put = client.put(
        "/api/v1/dalton/channels/internal/weixin-state",
        headers=headers,
        json={
            "ilink_bot_id": "bot_123",
            "owner_user_id": "u-admin",
            "bot_token": "wx_secret_tok",
            "ilink_user_id": "wx_admin_u1",
            "get_updates_buf": "cursor_1",
        },
    )
    assert r_wx_put.status_code == 200
    assert r_wx_put.json()["ok"] is True

    r_wx_get = client.get(
        "/api/v1/dalton/channels/internal/weixin-state", headers=headers
    )
    assert r_wx_get.json()["configured"] is True
    assert r_wx_get.json()["bot_token"] == "wx_secret_tok"
    assert r_wx_get.json()["get_updates_buf"] == "cursor_1"

    # §1.5 #4: Weixin QR endpoints restrict non-admin with 403 and allow admin
    channel_test_env["current_web_user"]["user_id"] = "u-alice"
    for method, url, payload in (
        ("POST", "/api/v1/dalton/channels/weixin/qrcode/start", None),
        ("GET", "/api/v1/dalton/channels/weixin/qrcode/wait?session_key=s1", None),
        (
            "POST",
            "/api/v1/dalton/channels/weixin/qrcode/verify-code",
            {"session_key": "s1", "verify_code": "123456"},
        ),
    ):
        resp = client.request(method, url, json=payload)
        assert resp.status_code == 403
        assert (
            resp.json()["detail"]
            == "Only admin users can connect or replace the shared Weixin bot."
        )

    # Switch to admin user and verify proxy to gateway
    channel_test_env["current_web_user"]["user_id"] = "u-admin"

    async def _fake_proxy(
        method: str, subpath: str, *, user_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        return {"proxied": subpath, "user_id": user_id}

    monkeypatch.setattr(
        ch_router_mod,
        "_proxy_to_gateway_weixin",
        create_autospec(
            ch_router_mod._proxy_to_gateway_weixin, side_effect=_fake_proxy
        ),
    )
    r_qr_start = client.post("/api/v1/dalton/channels/weixin/qrcode/start")
    assert r_qr_start.status_code == 200
    assert r_qr_start.json() == {
        "proxied": "/internal/weixin/qrcode/start",
        "user_id": "u-admin",
    }


def test_round2_review_fixes_group_permissions_generations_locks_unknown_and_credit_steer(
    channel_test_env: dict[str, Any],
) -> None:
    """Verify Round 2 review fixes (Decisions 2, 3, 4, 5, 6, 7, 8):
    - Decision 2: Web `PUT /settings` requires admin or group inbound membership (403 for non-member);
      `GET /bindings` filters `chat_bindings` by the same rule and omits `weixin_context_token`;
    - Decision 3: First-time group bind, workspace change, and group deletion via I-5 / Web settings
      all advance group session generations (`chat:<id>` and `chat:<id>:thread:%`), and deletion removes
      the `dalton_channel_chat_bindings` row;
    - Decision 4: I-1 group read acquires the same per-group advisory lock (`dalton_group_chat:<platform>:<address>`)
      as I-3, I-5, and Web settings;
    - Decision 5: `unknown` current-turn text delivery sets `has_in_flight_or_unknown_current_text=True` in I-7
      and blocks `fallback:<run_id>` claim (`claim_status="denied"`) in I-6;
    - Decision 6: `create_bind_code` acquires `dalton_bind_code:<user_id>:<platform>` advisory lock and keeps
      at most one active unused code;
    - Decision 7: Credit supplement steer (`credit_steer_request_key`) with no `hook_outbox` row stays
      `still_pending` (`fallback_pending=True`) while the run is `in_progress` or lacks `run_id`, and only
      settles when the run reaches a terminal status;
    - Decision 8: I-8 and I-6 Scenario B share `_verify_group_target_deliverable` (requiring both
      `dalton_channel_chat_bindings` and `user_settings.apps[platform].chats`).
    """
    client: TestClient = channel_test_env["client"]
    headers = channel_test_env["headers"]
    store: InMemoryChannelStore = channel_test_env["store"]
    writable_by_user = channel_test_env["writable_by_user"]

    ws_a = str(uuid4())
    ws_b = str(uuid4())
    for wid, wname in ((ws_a, "Workspace A"), (ws_b, "Workspace B")):
        store.workspaces[wid] = {
            "workspace_id": UUID(wid),
            "user_id": "u-alice",
            "name": wname,
            "status": "running",
            "is_home": False,
        }
    writable_by_user["u-alice"] = {ws_a: "Workspace A", ws_b: "Workspace B"}
    writable_by_user["u-bob"] = {ws_a: "Workspace A", ws_b: "Workspace B"}
    writable_by_user["u-admin"] = {ws_a: "Workspace A", ws_b: "Workspace B"}

    # Bind u-alice, u-bob, u-admin on feishu; also bind u-alice on weixin with a context token
    for uid, puid in (("u-alice", "ou_alice"), ("u-bob", "ou_bob"), ("u-admin", "ou_admin")):
        channel_test_env["current_web_user"]["user_id"] = uid
        store.advisory_locks.clear()
        code_resp = client.post(
            "/api/v1/dalton/channels/bind-codes", json={"platform": "feishu"}
        ).json()
        # Decision 6: advisory lock acquired on create_bind_code
        assert f"dalton_bind_code:{uid}:feishu" in store.advisory_locks
        client.post(
            "/api/v1/dalton/channels/internal/bind-code/consume",
            headers=headers,
            json={
                "action": "bind",
                "platform": "feishu",
                "platform_user_id": puid,
                "code": code_resp["code"],
                "is_group": False,
            },
        )

    channel_test_env["current_web_user"]["user_id"] = "u-alice"
    wx_code = client.post(
        "/api/v1/dalton/channels/bind-codes", json={"platform": "weixin"}
    ).json()["code"]
    client.post(
        "/api/v1/dalton/channels/internal/bind-code/consume",
        headers=headers,
        json={
            "action": "bind",
            "platform": "weixin",
            "platform_user_id": "wx_alice",
            "code": wx_code,
            "is_group": False,
            "weixin_context_token": "secret_ctx_tok_123",
        },
    )

    # Decision 2 (3): Web GET /bindings strips weixin_context_token, while internal I-4 keeps it
    r_web_bindings = client.get("/api/v1/dalton/channels/bindings")
    assert r_web_bindings.status_code == 200
    assert "weixin_context_token" not in r_web_bindings.json()["identities"]["weixin"]
    r_internal_state = client.get(
        "/api/v1/dalton/channels/internal/state?user_id=u-alice", headers=headers
    )
    assert (
        r_internal_state.json()["identities"]["weixin"]["weixin_context_token"]
        == "secret_ctx_tok_123"
    )

    # u-alice sends an inbound message in group feishu:oc_grp1 (u-bob has NEVER sent a message in feishu:oc_grp1)
    store.advisory_locks.clear()
    r_in_grp = client.post(
        "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
        headers=headers,
        json={
            "platform": "feishu",
            "platform_user_id": "ou_alice",
            "chat_type": "group",
            "chat_address": "feishu:oc_grp1",
            "thread_scope_key": "chat:oc_grp1",
            "message_id": "om_grp1_msg1",
        },
    )
    assert r_in_grp.json()["allowed"] is True
    assert r_in_grp.json()["generation"] == 1
    # Decision 4: I-1 acquired the group chat advisory lock
    assert "dalton_group_chat:feishu:feishu:oc_grp1" in store.advisory_locks

    # Seed a sub-thread generation row for u-bob in chat:oc_grp1:thread:sub1 at generation 2
    store.session_generations[("feishu", "chat:oc_grp1:thread:sub1", "u-bob")] = {
        "platform": "feishu",
        "scope_key": "chat:oc_grp1:thread:sub1",
        "dalton_user_id": "u-bob",
        "generation": 2,
        "override_workspace_id": None,
        "pending_interrupt": {"interrupt_id": "i1"},
        "updated_at": datetime.now(timezone.utc),
    }

    # Decision 2 (1): Non-member u-bob tries to bind feishu:oc_grp1 via Web PUT /settings -> 403!
    channel_test_env["current_web_user"]["user_id"] = "u-bob"
    r_bob_forbidden = client.put(
        "/api/v1/dalton/channels/settings",
        json={
            "version": "v1",
            "settings": {
                "feishu": {
                    "preferred": "feishu:@me",
                    "chats": {"feishu:oc_grp1": {"workspace_id": ws_a, "name": "Grp 1"}},
                    "automation_output": {},
                    "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
                }
            },
        },
    )
    assert r_bob_forbidden.status_code == 403

    # Decision 2 (1) & Decision 3 (1): Member u-alice binds feishu:oc_grp1 for the FIRST time via Web PUT /settings -> 200,
    # and advances generations for ALL users in chat:oc_grp1 (1 -> 2) and chat:oc_grp1:thread:sub1 (2 -> 3)!
    channel_test_env["current_web_user"]["user_id"] = "u-alice"
    alice_ver = r_web_bindings.json()["settings"]["version"]
    r_alice_first_bind = client.put(
        "/api/v1/dalton/channels/settings",
        json={
            "version": alice_ver,
            "settings": {
                "feishu": {
                    "preferred": "feishu:@me",
                    "chats": {"feishu:oc_grp1": {"workspace_id": ws_a, "name": "Grp 1"}},
                    "automation_output": {},
                    "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
                }
            },
        },
    )
    assert r_alice_first_bind.status_code == 200
    assert (
        store.session_generations[("feishu", "chat:oc_grp1", "u-alice")]["generation"]
        == 2
    )
    assert (
        store.session_generations[("feishu", "chat:oc_grp1:thread:sub1", "u-bob")][
            "generation"
        ]
        == 3
    )
    assert (
        store.session_generations[("feishu", "chat:oc_grp1:thread:sub1", "u-bob")][
            "pending_interrupt"
        ]
        is None
    )

    # Decision 2 (2): GET /bindings visibility:
    # - u-bob (non-member) does NOT see feishu:oc_grp1 in chat_bindings;
    # - u-alice (member) and u-admin (admin) DO see feishu:oc_grp1 in chat_bindings.
    channel_test_env["current_web_user"]["user_id"] = "u-bob"
    bob_bindings = client.get("/api/v1/dalton/channels/bindings").json()["chat_bindings"]
    assert all(b["chat_address"] != "feishu:oc_grp1" for b in bob_bindings)

    channel_test_env["current_web_user"]["user_id"] = "u-alice"
    alice_bindings = client.get("/api/v1/dalton/channels/bindings").json()["chat_bindings"]
    assert any(b["chat_address"] == "feishu:oc_grp1" for b in alice_bindings)

    channel_test_env["current_web_user"]["user_id"] = "u-admin"
    admin_bindings = client.get("/api/v1/dalton/channels/bindings").json()["chat_bindings"]
    assert any(b["chat_address"] == "feishu:oc_grp1" for b in admin_bindings)

    # Decision 8: Group exists in dalton_channel_chat_bindings (bound by u-alice),
    # but is NOT in u-bob's user_settings.apps.feishu.chats -> BOTH I-8 and I-6 Scenario B reject it!
    exec_bob = str(uuid4())
    r_i8_bob = client.post(
        "/api/v1/dalton/channels/internal/automation-runs",
        headers=headers,
        json={
            "execution_id": exec_bob,
            "user_id": "u-bob",
            "workspace_id": ws_a,
            "automation_name": "Bob Auto",
            "entries": ["feishu:oc_grp1"],
        },
    )
    assert r_i8_bob.status_code == 200
    assert r_i8_bob.json()["targets"][0]["ok"] is False

    # Decision 3 (2): Changing group workspace (ws_a -> ws_b) advances all group generations (2 -> 3 and 3 -> 4)
    channel_test_env["current_web_user"]["user_id"] = "u-alice"
    r_alice_change_ws = client.put(
        "/api/v1/dalton/channels/settings",
        json={
            "version": r_alice_first_bind.json()["version"],
            "settings": {
                "feishu": {
                    "preferred": "feishu:@me",
                    "chats": {"feishu:oc_grp1": {"workspace_id": ws_b, "name": "Grp 1"}},
                    "automation_output": {},
                    "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
                }
            },
        },
    )
    assert r_alice_change_ws.status_code == 200
    assert (
        store.session_generations[("feishu", "chat:oc_grp1", "u-alice")]["generation"]
        == 3
    )
    assert (
        store.session_generations[("feishu", "chat:oc_grp1:thread:sub1", "u-bob")][
            "generation"
        ]
        == 4
    )

    # Decision 3 (3): Deleting group from settings removes dalton_channel_chat_bindings row
    # AND advances all group generations (3 -> 4 and 4 -> 5)
    r_alice_del_grp = client.put(
        "/api/v1/dalton/channels/settings",
        json={
            "version": r_alice_change_ws.json()["version"],
            "settings": {
                "feishu": {
                    "preferred": "feishu:@me",
                    "chats": {},
                    "automation_output": {},
                    "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
                }
            },
        },
    )
    assert r_alice_del_grp.status_code == 200
    assert ("feishu", "feishu:oc_grp1") not in store.chat_bindings
    assert (
        store.session_generations[("feishu", "chat:oc_grp1", "u-alice")]["generation"]
        == 4
    )
    assert (
        store.session_generations[("feishu", "chat:oc_grp1:thread:sub1", "u-bob")][
            "generation"
        ]
        == 5
    )

    # Decision 5: `unknown` current-turn text delivery is reported by I-7 and blocks `fallback:<run_id>` in I-6
    run_unk = str(uuid4())
    thread_unk = str(uuid4())
    client.post(
        "/api/v1/dalton/channels/internal/inbound/progress",
        headers=headers,
        json={
            "platform": "feishu",
            "message_id": "om_grp1_msg1",
            "status": "streaming",
            "thread_id": thread_unk,
            "run_id": run_unk,
            "turn_index": 1,
            "fallback_pending": True,
        },
    )
    dkey_unk = f"tool:{run_unk}:call_unk"
    client.post(
        "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
        headers=headers,
        json={
            "delivery_key": dkey_unk,
            "kind": "agent_send",
            "user_id": "u-alice",
            "thread_id": thread_unk,
            "run_id": run_unk,
            "platform": "feishu",
            "has_non_empty_text": True,
            "total_chunks": 2,
        },
    )
    client.post(
        "/api/v1/dalton/channels/internal/deliveries/progress",
        headers=headers,
        json={
            "delivery_key": dkey_unk,
            "sent_chunks": 1,
            "total_chunks": 2,
            "status": "unknown",
            "error_code": "gateway_restarted",
        },
    )
    r_prog_unk = client.post(
        "/api/v1/dalton/channels/internal/inbound/progress",
        headers=headers,
        json={
            "platform": "feishu",
            "message_id": "om_grp1_msg1",
            "run_id": run_unk,
        },
    )
    assert r_prog_unk.status_code == 200
    assert r_prog_unk.json()["has_unknown_current_text"] is True
    assert r_prog_unk.json()["has_in_flight_or_unknown_current_text"] is True

    r_fb_denied = client.post(
        "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
        headers=headers,
        json={
            "delivery_key": f"fallback:{run_unk}",
            "kind": "fallback",
            "user_id": "u-alice",
            "thread_id": thread_unk,
            "run_id": run_unk,
            "platform": "feishu",
            "has_non_empty_text": True,
            "total_chunks": 1,
        },
    )
    assert r_fb_denied.status_code == 200
    assert r_fb_denied.json()["claim_status"] == "denied"
    assert r_fb_denied.json()["code"] == "unknown_delivery_blocks_fallback"

    # Decision 7: Credit supplement steer with no outbox row yet stays `still_pending` while run is in_progress,
    # and only settles when the run reaches a terminal status.
    credit_mid = "om_credit_steer_1"
    credit_run_id = str(uuid4())
    credit_req_key = str(uuid5(DALTON_CHANNEL_NAMESPACE, f"feishu:{credit_mid}"))
    store.inbound_events[("feishu", credit_mid)] = {
        "platform": "feishu",
        "message_id": credit_mid,
        "request_key": UUID(credit_req_key),
        "retry_request_key": None,
        "resubmitted_request_key": None,
        "steered_into_run_id": credit_run_id,
        "dalton_user_id": "u-alice",
        "chat_type": "dm",
        "chat_address": "feishu:@me",
        "thread_scope_key": "dm:ou_alice",
        "external_thread_id": "feishu:dm:ou_alice:gen:1",
        "workspace_id": UUID(ws_a),
        "thread_id": UUID(thread_unk),
        "run_id": credit_run_id,
        "turn_index": 1,
        "status": "streaming",
        "fallback_pending": True,
        "sent_to_current_count": 0,
        "raw_payload": {
            "credit_steer_request_key": str(uuid4()),
            "credit_steer_state": "pending",
        },
        "uploaded_attachments": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    store.conversation_responses[credit_run_id] = {"status": "in_progress"}

    r_rec_live = client.post(
        "/api/v1/dalton/channels/internal/steers/reconcile",
        headers=headers,
        json={"platform": "feishu", "message_id": credit_mid, "run_id": credit_run_id},
    )
    assert r_rec_live.status_code == 200
    assert r_rec_live.json()["reconcile_status"] == "still_pending"
    assert store.inbound_events[("feishu", credit_mid)]["fallback_pending"] is True
    assert store.inbound_events[("feishu", credit_mid)]["status"] == "streaming"

    # Once run finishes as 'completed', reconcile_steer transitions it to pending_resubmit
    store.conversation_responses[credit_run_id] = {"status": "completed"}
    r_rec_done = client.post(
        "/api/v1/dalton/channels/internal/steers/reconcile",
        headers=headers,
        json={"platform": "feishu", "message_id": credit_mid, "run_id": credit_run_id},
    )
    assert r_rec_done.status_code == 200
    assert r_rec_done.json()["reconcile_status"] == "pending_resubmit"
    assert store.inbound_events[("feishu", credit_mid)]["fallback_pending"] is False
    assert (
        store.inbound_events[("feishu", credit_mid)]["raw_payload"]["credit_steer_state"]
        == "resubmitted"
    )


def test_round3_review_fixes_unit(channel_test_env):
    """Covers Round 3 review fixes:
    1. Group membership check strictly matches current bound `platform_user_id` against `raw_payload->>'platform_user_id'` (rebound identity cannot manage/view old group);
    2. Saving settings only processes groups changed in THIS submission relative to the user's previously saved `chats`, comparing against global binding (`dalton_channel_chat_bindings`):
       - Unchanged groups in `chats` (e.g. saving `automation_output` or re-saving unchanged settings) never touch global binding or generations;
       - Added/modified group with the SAME workspace as current global binding does NOT advance generations or clear `pending_interrupt`;
       - Deleted group only deletes global binding and advances generations if current global binding still matches the workspace in this user's old settings;
    3. Internal member who has spoken in a group sees it in `GET /bindings` (`chat_bindings`) even without write permission on the bound workspace;
    4. I-1 replay returns the creation-time `generation` recorded on the row (`raw_payload["generation"]`), consistent with `external_thread_id` and `workspace_id`.
    """
    client = channel_test_env["client"]
    store: InMemoryChannelStore = channel_test_env["store"]
    headers = channel_test_env["headers"]

    ws_1 = str(uuid4())
    ws_2 = str(uuid4())
    store.workspaces[ws_1] = {
        "workspace_id": UUID(ws_1),
        "user_id": "u-alice",
        "name": "WS 1",
        "status": "running",
    }
    store.workspaces[ws_2] = {
        "workspace_id": UUID(ws_2),
        "user_id": "u-alice",
        "name": "WS 2",
        "status": "running",
    }
    channel_test_env["writable_by_user"]["u-alice"] = {ws_1: "WS 1", ws_2: "WS 2"}
    channel_test_env["writable_by_user"]["u-bob"] = {ws_1: "WS 1", ws_2: "WS 2"}
    # u-carol is an internal member with NO write access to ws_1 or ws_2
    channel_test_env["writable_by_user"]["u-carol"] = {}
    channel_test_env["tiers"]["u-alice"] = "internal"
    channel_test_env["tiers"]["u-bob"] = "internal"
    channel_test_env["tiers"]["u-carol"] = "internal"

    for uid, puid in [
        ("u-alice", "ou_alice"),
        ("u-bob", "ou_bob"),
        ("u-carol", "ou_carol"),
    ]:
        channel_test_env["current_web_user"]["user_id"] = uid
        code = client.post(
            "/api/v1/dalton/channels/bind-codes", json={"platform": "feishu"}
        ).json()["code"]
        client.post(
            "/api/v1/dalton/channels/internal/bind-code/consume",
            headers=headers,
            json={
                "action": "bind",
                "platform": "feishu",
                "platform_user_id": puid,
                "code": code,
                "is_group": False,
            },
        )

    # All three speak in feishu:oc_r3_grp
    for idx, puid in enumerate(["ou_alice", "ou_bob", "ou_carol"], start=1):
        r_in = client.post(
            "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
            headers=headers,
            json={
                "platform": "feishu",
                "platform_user_id": puid,
                "chat_type": "group",
                "chat_address": "feishu:oc_r3_grp",
                "thread_scope_key": "chat:oc_r3_grp",
                "message_id": f"om_r3_init_{idx}",
            },
        )
        assert r_in.status_code == 200
        assert r_in.json()["generation"] == 1

    # Alice binds feishu:oc_r3_grp -> ws_1 via Web PUT /settings (generation advances 1 -> 2)
    channel_test_env["current_web_user"]["user_id"] = "u-alice"
    v_alice_0 = client.get("/api/v1/dalton/channels/bindings").json()["settings"]["version"]
    r_alice_bind = client.put(
        "/api/v1/dalton/channels/settings",
        json={
            "version": v_alice_0,
            "settings": {
                "feishu": {
                    "preferred": "feishu:@me",
                    "chats": {"feishu:oc_r3_grp": {"workspace_id": ws_1, "name": "R3 Grp"}},
                    "automation_output": {},
                    "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
                }
            },
        },
    )
    assert r_alice_bind.status_code == 200
    assert (
        store.session_generations[("feishu", "chat:oc_r3_grp", "u-alice")]["generation"]
        == 2
    )
    assert (
        store.session_generations[("feishu", "chat:oc_r3_grp", "u-bob")]["generation"]
        == 2
    )

    # Decision 4: Replaying I-1 for om_r3_init_1 AFTER generation advanced to 2 still returns generation == 1
    # (consistent with external_thread_id and workspace_id recorded at creation time)
    r_replay = client.post(
        "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
        headers=headers,
        json={
            "platform": "feishu",
            "platform_user_id": "ou_alice",
            "chat_type": "group",
            "chat_address": "feishu:oc_r3_grp",
            "thread_scope_key": "chat:oc_r3_grp",
            "message_id": "om_r3_init_1",
        },
    )
    assert r_replay.status_code == 200
    assert r_replay.json()["existing_status"] == "pending"
    assert r_replay.json()["generation"] == 1
    assert r_replay.json()["external_thread_id"].endswith(":gen:1")

    # Decision 3: u-carol (spoke in group, NO write access to ws_1) CAN see feishu:oc_r3_grp in GET /bindings chat_bindings
    channel_test_env["current_web_user"]["user_id"] = "u-carol"
    carol_bindings = client.get("/api/v1/dalton/channels/bindings").json()["chat_bindings"]
    assert any(
        b["chat_address"] == "feishu:oc_r3_grp" and b["workspace_id"] == ws_1
        for b in carol_bindings
    )

    # Decision 2 (a): Set a pending_interrupt on Bob's group session, then Bob adds feishu:oc_r3_grp -> ws_1
    # (same workspace as current global binding) in Bob's own settings.
    # Global binding is already ws_1 -> MUST NOT advance generations and MUST NOT clear pending_interrupt!
    store.session_generations[("feishu", "chat:oc_r3_grp", "u-bob")][
        "pending_interrupt"
    ] = {"interrupt_id": "int_keep"}
    channel_test_env["current_web_user"]["user_id"] = "u-bob"
    v_bob_0 = client.get("/api/v1/dalton/channels/bindings").json()["settings"]["version"]
    r_bob_same_ws = client.put(
        "/api/v1/dalton/channels/settings",
        json={
            "version": v_bob_0,
            "settings": {
                "feishu": {
                    "preferred": "feishu:@me",
                    "chats": {"feishu:oc_r3_grp": {"workspace_id": ws_1, "name": "R3 Grp"}},
                    "automation_output": {},
                    "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
                }
            },
        },
    )
    assert r_bob_same_ws.status_code == 200
    assert (
        store.session_generations[("feishu", "chat:oc_r3_grp", "u-bob")]["generation"]
        == 2
    )
    assert store.session_generations[("feishu", "chat:oc_r3_grp", "u-bob")][
        "pending_interrupt"
    ] == {"interrupt_id": "int_keep"}

    # Decision 2 (b): Bob switches the group to ws_2 via I-3 `/workspace` (generation advances 2 -> 3, global binding is now ws_2,
    # while Alice's saved settings still have feishu:oc_r3_grp -> ws_1).
    r_slash_sw = client.post(
        "/api/v1/dalton/channels/internal/session/command",
        headers=headers,
        json={
            "cmd": "workspace_switch",
            "platform": "feishu",
            "dalton_user_id": "u-bob",
            "chat_type": "group",
            "chat_address": "feishu:oc_r3_grp",
            "chat_id": "oc_r3_grp",
            "scope_key": "chat:oc_r3_grp",
            "workspace_id": ws_2,
        },
    )
    assert r_slash_sw.status_code == 200
    assert r_slash_sw.json()["ok"] is True
    assert str(store.chat_bindings[("feishu", "feishu:oc_r3_grp")]["workspace_id"]) == ws_2
    assert (
        store.session_generations[("feishu", "chat:oc_r3_grp", "u-alice")]["generation"]
        == 3
    )

    # Alice updates automation_output only (her chats still has feishu:oc_r3_grp -> ws_1 unchanged relative to her last save).
    # Must NOT overwrite global binding back to ws_1 and must NOT advance generations!
    channel_test_env["current_web_user"]["user_id"] = "u-alice"
    r_alice_auto = client.put(
        "/api/v1/dalton/channels/settings",
        json={
            "workspace_id": ws_1,
            "platform": "feishu",
            "address": "feishu:@me",
        },
    )
    assert r_alice_auto.status_code == 200
    assert str(store.chat_bindings[("feishu", "feishu:oc_r3_grp")]["workspace_id"]) == ws_2
    assert (
        store.session_generations[("feishu", "chat:oc_r3_grp", "u-alice")]["generation"]
        == 3
    )

    # Decision 2 (c): Alice now deletes feishu:oc_r3_grp from her own settings.
    # Because global binding is ws_2 (switched by Bob), NOT ws_1 (Alice's old setting),
    # deleting from Alice's settings only removes it from Alice's settings and does NOT delete global binding ws_2 or advance generations!
    r_alice_del_stale = client.put(
        "/api/v1/dalton/channels/settings",
        json={
            "version": r_alice_auto.json()["version"],
            "settings": {
                "feishu": {
                    "preferred": "feishu:@me",
                    "chats": {},
                    "automation_output": {ws_1: "feishu:@me"},
                    "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
                }
            },
        },
    )
    assert r_alice_del_stale.status_code == 200
    assert ("feishu", "feishu:oc_r3_grp") in store.chat_bindings
    assert str(store.chat_bindings[("feishu", "feishu:oc_r3_grp")]["workspace_id"]) == ws_2
    assert (
        store.session_generations[("feishu", "chat:oc_r3_grp", "u-alice")]["generation"]
        == 3
    )

    # Decision 1: Alice unbinds ou_alice and binds a new Feishu account ou_alice_2 that has NEVER spoken in feishu:oc_r3_grp.
    # Even though dalton_channel_inbound_events has old rows with dalton_user_id == 'u-alice',
    # Alice's current bound identity ou_alice_2 has not spoken in feishu:oc_r3_grp -> cannot see it in GET /bindings and gets 403 on PUT /settings!
    client.delete("/api/v1/dalton/channels/bindings/feishu")
    code_a2 = client.post(
        "/api/v1/dalton/channels/bind-codes", json={"platform": "feishu"}
    ).json()["code"]
    client.post(
        "/api/v1/dalton/channels/internal/bind-code/consume",
        headers=headers,
        json={
            "action": "bind",
            "platform": "feishu",
            "platform_user_id": "ou_alice_2",
            "code": code_a2,
            "is_group": False,
        },
    )
    alice_after_rebind = client.get("/api/v1/dalton/channels/bindings").json()
    assert all(
        b["chat_address"] != "feishu:oc_r3_grp"
        for b in alice_after_rebind["chat_bindings"]
    )
    r_alice_rebind_put = client.put(
        "/api/v1/dalton/channels/settings",
        json={
            "version": alice_after_rebind["settings"]["version"],
            "settings": {
                "feishu": {
                    "preferred": "feishu:@me",
                    "chats": {"feishu:oc_r3_grp": {"workspace_id": ws_1, "name": "R3 Grp"}},
                    "automation_output": {},
                    "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
                }
            },
        },
    )
    assert r_alice_rebind_put.status_code == 403


def test_round4_lock_order_and_readonly_name_unit(channel_test_env):
    """Covers Round 4 review fixes:
    1. Unified sorted lock acquisition order across deleted and added/modified groups (preventing deadlock when A deletes G1 + modifies G2 while B deletes G2 + modifies G1);
    2. Stripping read-only display fields (`name`, `workspace` label) when `workspace_id` is unchanged is NOT treated as a group modification, does NOT touch global bindings, and does NOT advance generations.
    """
    client = channel_test_env["client"]
    store: InMemoryChannelStore = channel_test_env["store"]
    headers = channel_test_env["headers"]

    ws_1 = str(uuid4())
    ws_2 = str(uuid4())
    store.workspaces[ws_1] = {
        "workspace_id": UUID(ws_1),
        "user_id": "u-alice",
        "name": "WS 1",
        "status": "running",
    }
    store.workspaces[ws_2] = {
        "workspace_id": UUID(ws_2),
        "user_id": "u-alice",
        "name": "WS 2",
        "status": "running",
    }
    channel_test_env["writable_by_user"]["u-alice"] = {ws_1: "WS 1", ws_2: "WS 2"}
    channel_test_env["writable_by_user"]["u-bob"] = {ws_1: "WS 1", ws_2: "WS 2"}

    for uid, puid in [("u-alice", "ou_alice"), ("u-bob", "ou_bob")]:
        channel_test_env["current_web_user"]["user_id"] = uid
        code = client.post(
            "/api/v1/dalton/channels/bind-codes", json={"platform": "feishu"}
        ).json()["code"]
        client.post(
            "/api/v1/dalton/channels/internal/bind-code/consume",
            headers=headers,
            json={
                "action": "bind",
                "platform": "feishu",
                "platform_user_id": puid,
                "code": code,
                "is_group": False,
            },
        )

    for grp in ("oc_g1", "oc_g2"):
        for idx, puid in enumerate(("ou_alice", "ou_bob"), start=1):
            client.post(
                "/api/v1/dalton/channels/internal/inbound/register-and-resolve",
                headers=headers,
                json={
                    "platform": "feishu",
                    "platform_user_id": puid,
                    "chat_type": "group",
                    "chat_address": f"feishu:{grp}",
                    "thread_scope_key": f"chat:{grp}",
                    "message_id": f"om_{grp}_{idx}",
                },
            )

    # Both Alice and Bob save G1 -> ws_1 (with name) and G2 -> ws_1 (with name)
    for uid in ("u-alice", "u-bob"):
        channel_test_env["current_web_user"]["user_id"] = uid
        ver = client.get("/api/v1/dalton/channels/bindings").json()["settings"]["version"]
        r_init = client.put(
            "/api/v1/dalton/channels/settings",
            json={
                "version": ver,
                "settings": {
                    "feishu": {
                        "preferred": "feishu:@me",
                        "chats": {
                            "feishu:oc_g1": {"workspace_id": ws_1, "name": "Group 1"},
                            "feishu:oc_g2": {"workspace_id": ws_1, "name": "Group 2"},
                        },
                        "automation_output": {},
                        "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
                    }
                },
            },
        )
        assert r_init.status_code == 200

    # Bob switches G1 to ws_2 via `/workspace` (so global binding for G1 is ws_2, while Alice's saved settings still have G1 -> ws_1 with name="Group 1")
    r_sw = client.post(
        "/api/v1/dalton/channels/internal/session/command",
        headers=headers,
        json={
            "cmd": "workspace_switch",
            "platform": "feishu",
            "dalton_user_id": "u-bob",
            "chat_type": "group",
            "chat_address": "feishu:oc_g1",
            "chat_id": "oc_g1",
            "scope_key": "chat:oc_g1",
            "workspace_id": ws_2,
        },
    )
    assert r_sw.status_code == 200
    gen_g1_before = store.session_generations[("feishu", "chat:oc_g1", "u-alice")][
        "generation"
    ]

    # Item 2: Alice saves via `I-5` (simulating `channels.json` `_stored()` which strips `name` and adds `workspace` label)
    # while keeping `workspace_id: ws_1` unchanged relative to her saved settings.
    # Because `name` / `workspace` are read-only labels, neither G1 nor G2 is treated as modified:
    # no group lock is acquired, global binding for G1 stays ws_2, and generation does NOT advance!
    store.advisory_locks.clear()
    channel_test_env["current_web_user"]["user_id"] = "u-alice"
    ver_alice = client.get("/api/v1/dalton/channels/bindings").json()["settings"]["version"]
    r_strip_name = client.put(
        "/api/v1/dalton/channels/settings",
        json={
            "version": ver_alice,
            "settings": {
                "feishu": {
                    "preferred": "feishu:@me",
                    "chats": {
                        "feishu:oc_g1": {"workspace_id": ws_1, "workspace": "WS 1"},
                        "feishu:oc_g2": {"workspace_id": ws_1, "workspace": "WS 1"},
                    },
                    "automation_output": {ws_1: "feishu:@me"},
                    "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
                }
            },
        },
    )
    assert r_strip_name.status_code == 200
    assert [lk for lk in store.advisory_locks if lk.startswith("dalton_group_chat:")] == []
    assert str(store.chat_bindings[("feishu", "feishu:oc_g1")]["workspace_id"]) == ws_2
    assert (
        store.session_generations[("feishu", "chat:oc_g1", "u-alice")]["generation"]
        == gen_g1_before
    )

    # Item 1: Alice deletes G1 and modifies G2 -> ws_2; Bob deletes G2 and modifies G1 -> ws_2.
    # Both MUST acquire per-group advisory locks in the exact same sorted order: [oc_g1, oc_g2]!
    store.advisory_locks.clear()
    channel_test_env["current_web_user"]["user_id"] = "u-alice"
    ver_alice_2 = r_strip_name.json()["version"]
    r_alice_cross = client.put(
        "/api/v1/dalton/channels/settings",
        json={
            "version": ver_alice_2,
            "settings": {
                "feishu": {
                    "preferred": "feishu:@me",
                    "chats": {
                        "feishu:oc_g2": {"workspace_id": ws_2, "name": "Group 2"},
                    },
                    "automation_output": {},
                    "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
                }
            },
        },
    )
    assert r_alice_cross.status_code == 200
    alice_group_locks = [
        lk for lk in store.advisory_locks if lk.startswith("dalton_group_chat:")
    ]
    assert alice_group_locks == [
        "dalton_group_chat:feishu:feishu:oc_g1",
        "dalton_group_chat:feishu:feishu:oc_g2",
    ]

    store.advisory_locks.clear()
    channel_test_env["current_web_user"]["user_id"] = "u-bob"
    ver_bob_2 = client.get("/api/v1/dalton/channels/bindings").json()["settings"]["version"]
    r_bob_cross = client.put(
        "/api/v1/dalton/channels/settings",
        json={
            "version": ver_bob_2,
            "settings": {
                "feishu": {
                    "preferred": "feishu:@me",
                    "chats": {
                        "feishu:oc_g1": {"workspace_id": ws_2, "name": "Group 1"},
                    },
                    "automation_output": {},
                    "agent_messages": {"enabled": True, "allowed": ["feishu:@me"]},
                }
            },
        },
    )
    assert r_bob_cross.status_code == 200
    bob_group_locks = [
        lk for lk in store.advisory_locks if lk.startswith("dalton_group_chat:")
    ]
    assert bob_group_locks == [
        "dalton_group_chat:feishu:feishu:oc_g1",
        "dalton_group_chat:feishu:feishu:oc_g2",
    ]


@pytest.mark.asyncio
async def test_round6_tx_retry_helper_and_unbind_lock_order_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 6 unit tests:
    - `_run_tx_with_retry` retries `DeadlockDetected` and `UniqueViolation(dalton_channel_user_settings_pkey)` and succeeds on attempt 2;
    - does NOT retry `UniqueViolation` with a different constraint name;
    - does NOT retry when caller passes an external `conn`;
    - re-raises the original exception when all 3 attempts fail;
    - `unbind` locks `dalton_channel_user_settings` (`FOR UPDATE`) BEFORE `DELETE FROM dalton_channel_identities`.
    """

    class _TestUniqueViolation(psycopg.errors.UniqueViolation):
        def __init__(self, message: str, constraint_name: str | None) -> None:
            super().__init__(message)
            diag_mock = create_autospec(psycopg.errors.Diagnostic, instance=True)
            diag_mock.constraint_name = constraint_name
            self._diag_mock = diag_mock

        @property
        def diag(self) -> Any:
            return self._diag_mock

    async def _fast_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(
        cg_db.asyncio,
        "sleep",
        create_autospec(cg_db.asyncio.sleep, side_effect=_fast_sleep),
    )

    # 1. DeadlockDetected retries and succeeds on 2nd attempt
    deadlock_calls: list[Any] = []

    async def _deadlock_then_ok(conn_arg: Any) -> dict[str, Any]:
        deadlock_calls.append(conn_arg)
        if len(deadlock_calls) == 1:
            raise psycopg.errors.DeadlockDetected("deadlock detected")
        return {"ok": True, "attempt": len(deadlock_calls)}

    attempt_fn_1 = create_autospec(_deadlock_then_ok, side_effect=_deadlock_then_ok)
    res_1 = await cg_db._run_tx_with_retry("test_deadlock_op", None, attempt_fn_1)
    assert res_1 == {"ok": True, "attempt": 2}
    assert attempt_fn_1.call_count == 2

    # 2. UniqueViolation on dalton_channel_user_settings_pkey retries and succeeds on 2nd attempt
    pkey_calls = 0

    async def _pkey_then_ok(conn_arg: Any) -> str:
        nonlocal pkey_calls
        pkey_calls += 1
        if pkey_calls == 1:
            raise _TestUniqueViolation(
                "duplicate key value violates unique constraint",
                cg_db.USER_SETTINGS_PKEY_CONSTRAINT,
            )
        return "recovered"

    attempt_fn_2 = create_autospec(_pkey_then_ok, side_effect=_pkey_then_ok)
    res_2 = await cg_db._run_tx_with_retry("test_pkey_op", None, attempt_fn_2)
    assert res_2 == "recovered"
    assert attempt_fn_2.call_count == 2

    # 3. UniqueViolation on a different constraint does NOT retry
    async def _other_unique(conn_arg: Any) -> None:
        raise _TestUniqueViolation(
            "duplicate key on other constraint",
            "dalton_channel_identities_user_id_platform_key",
        )

    attempt_fn_3 = create_autospec(_other_unique, side_effect=_other_unique)
    with pytest.raises(psycopg.errors.UniqueViolation):
        await cg_db._run_tx_with_retry("test_other_uq_op", None, attempt_fn_3)
    assert attempt_fn_3.call_count == 1

    # 4. Passing an external `conn` does NOT retry even on DeadlockDetected
    external_conn = create_autospec(psycopg.AsyncConnection, instance=True)

    async def _always_deadlock(conn_arg: Any) -> None:
        raise psycopg.errors.DeadlockDetected("deadlock on external conn")

    attempt_fn_4 = create_autospec(_always_deadlock, side_effect=_always_deadlock)
    with pytest.raises(psycopg.errors.DeadlockDetected):
        await cg_db._run_tx_with_retry("test_ext_conn_op", external_conn, attempt_fn_4)
    assert attempt_fn_4.call_count == 1

    # 5. All 3 attempts failing raises the original exception
    orig_exc = psycopg.errors.DeadlockDetected("persistent deadlock")

    async def _fail_three_times(conn_arg: Any) -> None:
        raise orig_exc

    attempt_fn_5 = create_autospec(_fail_three_times, side_effect=_fail_three_times)
    with pytest.raises(psycopg.errors.DeadlockDetected) as exc_info:
        await cg_db._run_tx_with_retry("test_3_fails_op", None, attempt_fn_5)
    assert exc_info.value is orig_exc
    assert attempt_fn_5.call_count == 3

    # 6. Unbind lock order: locks `dalton_channel_user_settings` FOR UPDATE BEFORE `DELETE FROM dalton_channel_identities`
    store = InMemoryChannelStore()
    executed_sqls: list[str] = []

    async def _tracking_handler(sql: str, params: Any) -> Any:
        executed_sqls.append(" ".join(sql.split()))
        return await store.handle_sql(sql, params)

    mocked_get_db, _ = _make_fake_db_from_handler(_tracking_handler)
    monkeypatch.setattr(cg_db, "get_db_connection", mocked_get_db)

    code_info = await cg_db.create_bind_code(user_id="u-lock-order", platform="feishu")
    await cg_db.consume_bind_code_or_unbind(
        action="bind",
        platform="feishu",
        platform_user_id="ou_lock_order",
        code=code_info["code"],
        is_group=False,
    )
    executed_sqls.clear()
    unbind_res = await cg_db.consume_bind_code_or_unbind(
        action="unbind",
        platform="feishu",
        user_id="u-lock-order",
    )
    assert unbind_res["ok"] is True
    settings_lock_idx = next(
        i
        for i, s in enumerate(executed_sqls)
        if "FROM dalton_channel_user_settings WHERE user_id = %s FOR UPDATE" in s
    )
    identity_delete_idx = next(
        i
        for i, s in enumerate(executed_sqls)
        if "DELETE FROM dalton_channel_identities" in s
    )
    assert settings_lock_idx < identity_delete_idx



