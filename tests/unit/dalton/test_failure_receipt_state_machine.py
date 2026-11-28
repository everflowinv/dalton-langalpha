"""Unit tests for Batch 4 (§3): Durable failure-notice receipt ledger, claim_token lease fence,
reconciliation state machine, automation_finish dedup, sync_terminal_undelivered_execution guard,
execution summary enrichment, and migration d062.

All test doubles use ``create_autospec`` (no signature-swallowing mocks).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import subprocess
import sys
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
import httpx
import psycopg
import pytest

from src.dalton.database import failure_notice_claims as claims_mod
from src.dalton.migrations.versions import d062_failure_notice_receipts as d062_mig
from src.dalton.notify import channel_transport as transport_mod
from src.dalton.notify import dispatch as dispatch_mod
from src.dalton.notify import failure_receipts as fr_mod
from src.dalton.services import execution_delivery_status as eds_mod
from src.dalton import settings as dalton_settings
from src.server.database import automation_executions as exec_db
from src.server.models.automation import AutomationExecutionResponse, FailureNoticeSummary
from src.server.services import automation_delivery

REPO_ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# In-memory PostgreSQL model simulating d062 tables + dalton_channel_deliveries
# ---------------------------------------------------------------------------


class _InMemoryFailureNoticeDB:
    """Deterministic in-memory PostgreSQL model for d062 unit tests."""

    def __init__(self, *, now: datetime = NOW) -> None:
        self.now = now
        self.notices: dict[str, dict[str, Any]] = {}
        self.streaks: dict[tuple[str, str], int] = {}
        self.targets: dict[str, dict[str, Any]] = {}
        self.channel_deliveries: dict[str, dict[str, Any]] = {}
        self.identities: set[tuple[str, str, str]] = set()
        self.weixin_accounts: dict[str, dict[str, Any]] = {}
        self.user_prefs: dict[str, dict[str, Any]] = {}
        self.soft_deleted_automations: set[str] = set()
        self.automation_executions: dict[str, dict[str, Any]] = {}
        self.data_task_defs: dict[str, dict[str, Any]] = {}
        self.data_task_runs: dict[str, dict[str, Any]] = {}
        self.sql_log: list[str] = []

    def add_identity(self, user_id: str, platform: str, platform_user_id: str) -> None:
        if platform == "weixin":
            self.weixin_accounts[user_id] = {
                "account_id": str(uuid4()),
                "user_id": user_id,
                "ilink_bot_id": "wx_bot_1",
                "bot_generation": 1,
                "ilink_user_id": platform_user_id,
                "status": "active",
            }
        else:
            self.identities.add((user_id, platform, platform_user_id))

    def _find_dedup_conflict(
        self, subject_kind: str, source_key: str, dedup_window_key: str | None
    ) -> dict[str, Any] | None:
        if dedup_window_key is None:
            return None
        for n in self.notices.values():
            if (
                n["subject_kind"] == subject_kind
                and n["source_key"] == source_key
                and n["dedup_window_key"] == dedup_window_key
                and n["aggregate_status"] not in ("failed", "partial_failed")
            ):
                return n
        return None

    def _find_subject(self, subject_kind: str, subject_id: str) -> dict[str, Any] | None:
        for n in self.notices.values():
            if n["subject_kind"] == subject_kind and n["subject_id"] == subject_id:
                return n
        return None

    def make_conn(self) -> Any:
        db = self
        conn = create_autospec(psycopg.AsyncConnection, instance=True)

        @asynccontextmanager
        async def _tx():
            yield

        @asynccontextmanager
        async def _cursor(*_args: Any, **_kwargs: Any):
            cur = create_autospec(psycopg.AsyncCursor, instance=True)
            last_rows: list[dict[str, Any]] = []

            async def _execute(sql: Any, params: Any = None) -> None:
                nonlocal last_rows
                s = " ".join(str(sql).split())
                db.sql_log.append(s)
                p = params or ()
                last_rows = []
                cur.rowcount = 0

                if "FROM dalton_data_task_defs" in s:
                    jk = str(p[0])
                    if jk in db.data_task_defs:
                        last_rows = [dict(db.data_task_defs[jk])]
                    return

                if "SELECT notify_status FROM dalton_data_task_runs" in s:
                    rid = str(p[0])
                    if rid in db.data_task_runs:
                        last_rows = [dict(db.data_task_runs[rid])]
                    return

                if "INSERT INTO dalton_failure_notice_streaks" in s and "DO UPDATE" in s:
                    kind, src_key = p[0], p[1]
                    db.streaks[(kind, src_key)] = db.streaks.get((kind, src_key), 0) + 1
                    last_rows = [{"recovery_epoch": db.streaks[(kind, src_key)]}]
                    cur.rowcount = 1
                    return

                if "INSERT INTO dalton_failure_notice_streaks" in s and "DO NOTHING" in s:
                    kind, src_key = p[0], p[1]
                    db.streaks.setdefault((kind, src_key), 0)
                    cur.rowcount = 1
                    return

                if "SELECT recovery_epoch FROM dalton_failure_notice_streaks" in s:
                    kind, src_key = p[0], p[1]
                    if (kind, src_key) in db.streaks:
                        last_rows = [{"recovery_epoch": db.streaks[(kind, src_key)]}]
                    return

                if "FROM user_preferences" in s:
                    uid = p[0]
                    if uid in db.user_prefs:
                        last_rows = [db.user_prefs[uid]]
                    return

                if "FROM dalton_channel_identities" in s and "SELECT 1" in s:
                    uid, plat, pid = p[0], p[1], p[2]
                    if (uid, plat, pid) in db.identities:
                        last_rows = [{"?column?": 1}]
                    return

                if "FROM dalton_channel_identities" in s:
                    uid = p[0]
                    last_rows = [
                        {"platform": plat, "platform_user_id": pid}
                        for (u, plat, pid) in sorted(db.identities)
                        if u == uid
                    ]
                    return

                if "FROM dalton_channel_weixin_accounts" in s and "SELECT 1" in s:
                    uid, pid = p[0], p[1]
                    acc = db.weixin_accounts.get(uid)
                    if acc and acc["status"] == "active" and acc["ilink_user_id"] == pid:
                        last_rows = [{"?column?": 1}]
                    return

                if "FROM dalton_channel_weixin_accounts" in s and "FOR KEY SHARE" in s:
                    matched: list[dict[str, Any]] = []
                    for acc in db.weixin_accounts.values():
                        if "user_id = %s OR account_id = %s::uuid" in s:
                            if acc.get("user_id") == p[0] or acc.get("account_id") == str(p[1]):
                                matched.append(dict(acc))
                        elif "WHERE account_id = %s::uuid" in s:
                            if acc.get("account_id") == str(p[0]):
                                matched.append(dict(acc))
                        else:
                            if acc.get("user_id") == p[0]:
                                matched.append(dict(acc))
                    matched.sort(key=lambda r: str(r.get("account_id") or ""))
                    last_rows = matched
                    return

                if "FROM dalton_channel_weixin_accounts" in s:
                    uid = p[0]
                    acc = db.weixin_accounts.get(uid)
                    if acc and acc["status"] == "active":
                        last_rows = [{"platform": "weixin", "platform_user_id": acc["ilink_user_id"]}]
                    return

                if "SELECT deleted_at IS NOT NULL FROM automations" in s:
                    aid = str(p[0])
                    last_rows = [{"deleted": aid in db.soft_deleted_automations}]
                    return

                if "INSERT INTO dalton_failure_notices" in s:
                    if "'retrying'" in s:
                        nid, kind, subj_id, src_key, r_uid, reason, dkey, sjson = p
                        agg = "retrying"
                    elif "'suppressed'" in s:
                        nid, kind, subj_id, src_key, r_uid, reason, sjson = p
                        dkey = None
                        agg = "suppressed"
                    else:
                        nid, kind, subj_id, src_key, r_uid, reason, agg, sjson = p
                        dkey = None

                    if db._find_subject(kind, subj_id) is not None:
                        return
                    if db._find_dedup_conflict(kind, src_key, dkey) is not None:
                        return
                    rec = {
                        "notice_id": str(nid),
                        "subject_kind": kind,
                        "subject_id": subj_id,
                        "source_key": src_key,
                        "recipient_user_id": r_uid,
                        "failure_reason": reason,
                        "dedup_window_key": dkey,
                        "aggregate_status": agg,
                        "summary_json": json.loads(sjson) if isinstance(sjson, str) else dict(sjson),
                        "created_at": db.now,
                        "updated_at": db.now,
                    }
                    db.notices[str(nid)] = rec
                    last_rows = [{"notice_id": str(nid), "aggregate_status": agg}]
                    cur.rowcount = 1
                    return

                if "SELECT notice_id::text AS notice_id, aggregate_status" in s and "WHERE subject_kind" in s:
                    kind, subj_id = p[0], p[1]
                    found = db._find_subject(kind, subj_id)
                    if found:
                        last_rows = [dict(found)]
                    return

                if "INSERT INTO dalton_failure_notice_targets" in s:
                    tr_id, nid, ch, tgt, max_att, pjson = p
                    for existing in db.targets.values():
                        if existing["notice_id"] == nid and existing["channel"] == ch and existing["target"] == tgt:
                            return
                    db.targets[str(tr_id)] = {
                        "target_receipt_id": str(tr_id),
                        "notice_id": str(nid),
                        "channel": ch,
                        "target": tgt,
                        "status": "pending",
                        "attempt_count": 0,
                        "max_attempts": int(max_att),
                        "claim_token": None,
                        "claim_expires_at": None,
                        "next_retry_at": db.now,
                        "uncertain_since": None,
                        "error_code": None,
                        "error_message": None,
                        "platform_message_ids": [],
                        "payload_json": json.loads(pjson) if isinstance(pjson, str) else dict(pjson),
                        "created_at": db.now,
                        "updated_at": db.now,
                    }
                    cur.rowcount = 1
                    return

                if "WITH due AS (" in s and "UPDATE dalton_failure_notice_targets" in s:
                    lim, ttl_s = int(p[0]), int(p[1])
                    claimed: list[dict[str, Any]] = []
                    for t in sorted(db.targets.values(), key=lambda r: (r["next_retry_at"] or r["created_at"], r["created_at"])):
                        if len(claimed) >= lim:
                            break
                        if (
                            t["status"] in ("pending", "retryable_failed")
                            and (t["next_retry_at"] is None or t["next_retry_at"] <= db.now)
                            and t["attempt_count"] < t["max_attempts"]
                        ):
                            t["status"] = "sending"
                            t["attempt_count"] += 1
                            t["claim_token"] = str(uuid4())
                            t["claim_expires_at"] = db.now + timedelta(seconds=ttl_s)
                            t["next_retry_at"] = None
                            t["error_code"] = None
                            t["error_message"] = None
                            t["updated_at"] = db.now
                            claimed.append(dict(t))
                    last_rows = claimed
                    cur.rowcount = len(claimed)
                    return

                if "FROM dalton_failure_notice_targets t JOIN dalton_failure_notices n" in s:
                    tr_id = str(p[0])
                    t = db.targets.get(tr_id)
                    if t:
                        n = db.notices[t["notice_id"]]
                        exp = t["claim_expires_at"]
                        lease_valid = bool(exp is not None and exp > db.now)
                        last_rows = [
                            {
                                **t,
                                "lease_valid": lease_valid,
                                "subject_kind": n["subject_kind"],
                                "subject_id": n["subject_id"],
                                "source_key": n["source_key"],
                                "recipient_user_id": n["recipient_user_id"],
                                "failure_reason": n["failure_reason"],
                            }
                        ]
                    return

                if "SELECT target_receipt_id::text AS target_receipt_id FROM dalton_failure_notice_targets WHERE (status = 'sending'" in s:
                    ref_now, _ref_now2, lim = p[0], p[1], int(p[2])
                    out = []
                    for t in db.targets.values():
                        if (t["status"] == "sending" and t["claim_expires_at"] is not None and t["claim_expires_at"] <= ref_now) or (
                            t["status"] == "uncertain" and (t["claim_expires_at"] is None or t["claim_expires_at"] <= ref_now)
                        ):
                            out.append({"target_receipt_id": t["target_receipt_id"]})
                    last_rows = out[:lim]
                    return

                if "FROM dalton_failure_notice_targets WHERE target_receipt_id = %s::uuid FOR UPDATE" in s:
                    tr_id = str(p[0])
                    t = db.targets.get(tr_id)
                    if t:
                        last_rows = [dict(t)]
                    return

                if "UPDATE dalton_failure_notice_targets SET claim_token = NULL, claim_expires_at = NULL, uncertain_since = COALESCE" in s:
                    us, tr_id = p[0], str(p[1])
                    t = db.targets[tr_id]
                    t["claim_token"] = None
                    t["claim_expires_at"] = None
                    t["uncertain_since"] = t["uncertain_since"] or us
                    t["updated_at"] = db.now
                    cur.rowcount = 1
                    return

                if "UPDATE dalton_failure_notice_targets SET status = 'sent'" in s:
                    if "AND claim_token = %s::uuid" in s:
                        mids, pjson, tr_id, tok = p
                        t = db.targets.get(str(tr_id))
                        if t and t["status"] == "sending" and t["claim_token"] == str(tok):
                            t["status"] = "sent"
                            t["claim_token"] = None
                            t["claim_expires_at"] = None
                            t["next_retry_at"] = None
                            t["error_code"] = None
                            t["error_message"] = None
                            t["platform_message_ids"] = json.loads(mids)
                            t["payload_json"] = json.loads(pjson)
                            t["updated_at"] = db.now
                            cur.rowcount = 1
                    else:
                        mids, pjson, tr_id = p
                        t = db.targets[str(tr_id)]
                        t["status"] = "sent"
                        t["claim_token"] = None
                        t["claim_expires_at"] = None
                        t["next_retry_at"] = None
                        t["error_code"] = None
                        t["error_message"] = None
                        t["platform_message_ids"] = json.loads(mids)
                        t["payload_json"] = json.loads(pjson)
                        t["updated_at"] = db.now
                        cur.rowcount = 1
                    return

                if "UPDATE dalton_failure_notice_targets SET status = 'uncertain', uncertain_since = COALESCE" in s:
                    ec, em, tr_id, tok = p
                    t = db.targets.get(str(tr_id))
                    if t and t["status"] == "sending" and t["claim_token"] == str(tok):
                        t["status"] = "uncertain"
                        t["uncertain_since"] = t["uncertain_since"] or db.now
                        t["error_code"] = ec
                        t["error_message"] = em
                        t["updated_at"] = db.now
                        cur.rowcount = 1
                    return

                if "UPDATE dalton_failure_notice_targets SET status = 'uncertain', claim_token = NULL" in s:
                    us, ec, tr_id = p
                    t = db.targets[str(tr_id)]
                    t["status"] = "uncertain"
                    t["claim_token"] = None
                    t["claim_expires_at"] = None
                    t["uncertain_since"] = us
                    t["error_code"] = ec
                    t["updated_at"] = db.now
                    cur.rowcount = 1
                    return

                if "UPDATE dalton_failure_notice_targets SET status = 'terminal_failed'" in s:
                    em, tr_id = p
                    t = db.targets[str(tr_id)]
                    t["status"] = "terminal_failed"
                    t["claim_token"] = None
                    t["claim_expires_at"] = None
                    t["next_retry_at"] = None
                    t["error_code"] = "delivery_outcome_unknown_timeout"
                    t["error_message"] = em
                    t["updated_at"] = db.now
                    cur.rowcount = 1
                    return

                if "UPDATE dalton_failure_notice_targets SET status = %s" in s:
                    if "AND claim_token = %s::uuid" in s:
                        new_st, nrs, _nrs2, ec, em, pjson, tr_id, tok = p
                        t = db.targets.get(str(tr_id))
                        if t and t["status"] == "sending" and t["claim_token"] == str(tok):
                            t["status"] = new_st
                            t["claim_token"] = None
                            t["claim_expires_at"] = None
                            t["next_retry_at"] = db.now + timedelta(seconds=nrs) if nrs is not None else None
                            t["error_code"] = ec
                            t["error_message"] = em
                            t["payload_json"] = json.loads(pjson)
                            t["updated_at"] = db.now
                            cur.rowcount = 1
                    elif "payload_json = %s::jsonb" in s:
                        new_st, nrs, ref_now, _nrs2, ec, em, pjson, tr_id = p
                        t = db.targets[str(tr_id)]
                        t["status"] = new_st
                        t["claim_token"] = None
                        t["claim_expires_at"] = None
                        t["next_retry_at"] = ref_now + timedelta(seconds=nrs) if nrs is not None else None
                        t["error_code"] = ec
                        t["error_message"] = em
                        t["payload_json"] = json.loads(pjson)
                        t["updated_at"] = db.now
                        cur.rowcount = 1
                    else:
                        new_st, nrs, ref_now, _nrs2, ec, tr_id = p
                        t = db.targets[str(tr_id)]
                        t["status"] = new_st
                        t["claim_token"] = None
                        t["claim_expires_at"] = None
                        t["next_retry_at"] = ref_now + timedelta(seconds=nrs) if nrs is not None else None
                        t["error_code"] = ec
                        t["updated_at"] = db.now
                        cur.rowcount = 1
                    return

                if "SELECT notice_id::text AS notice_id, aggregate_status, summary_json FROM dalton_failure_notices" in s:
                    nid = str(p[0])
                    if nid in db.notices:
                        last_rows = [dict(db.notices[nid])]
                    return

                if "SELECT status, error_code FROM dalton_failure_notice_targets WHERE notice_id" in s:
                    nid = str(p[0])
                    last_rows = [
                        {"status": t["status"], "error_code": t["error_code"]}
                        for t in db.targets.values()
                        if t["notice_id"] == nid
                    ]
                    return

                if "UPDATE dalton_failure_notices SET aggregate_status" in s:
                    agg, sjson, nid = p
                    n = db.notices[str(nid)]
                    n["aggregate_status"] = agg
                    n["summary_json"] = json.loads(sjson)
                    n["updated_at"] = db.now
                    cur.rowcount = 1
                    return

                if "FROM dalton_channel_deliveries WHERE delivery_key = %s" in s:
                    dkey = str(p[0])
                    ch = db.channel_deliveries.get(dkey)
                    if ch:
                        last_rows = [dict(ch)]
                    return

                if "INSERT INTO dalton_channel_deliveries" in s:
                    dkey = str(p[0])
                    db.channel_deliveries[dkey] = {
                        "delivery_key": dkey,
                        "kind": "failure_notice",
                        "user_id": p[1],
                        "run_id": p[2],
                        "execution_id": p[3],
                        "platform": p[5],
                        "target_address": p[6],
                        "status": "in_flight",
                        "total_chunks": p[7],
                        "sent_chunks": 0,
                        "platform_message_ids": [],
                        "error_code": None,
                        "error_message": None,
                        "first_response_json": None,
                    }
                    cur.rowcount = 1
                    return

                if "FROM dalton_failure_notices n LEFT JOIN LATERAL" in s:
                    wanted = set(p[0])
                    out = []
                    for n in db.notices.values():
                        if n["subject_id"].lower() in wanted and n["subject_kind"] in (
                            "personal_automation",
                            "company_automation",
                            "data_task",
                        ):
                            t_rows = [t for t in db.targets.values() if t["notice_id"] == n["notice_id"]]
                            err_codes = [t["error_code"] for t in t_rows if t["error_code"]]
                            out.append(
                                {
                                    "notice_id": n["notice_id"],
                                    "subject_kind": n["subject_kind"],
                                    "subject_id": n["subject_id"],
                                    "failure_reason": n["failure_reason"],
                                    "aggregate_status": n["aggregate_status"],
                                    "summary_json": n["summary_json"],
                                    "updated_at": n["updated_at"],
                                    "total_targets": len(t_rows),
                                    "sent_count": sum(1 for t in t_rows if t["status"] == "sent"),
                                    "failed_count": sum(1 for t in t_rows if t["status"] == "terminal_failed"),
                                    "retrying_count": sum(
                                        1
                                        for t in t_rows
                                        if t["status"] in ("pending", "sending", "uncertain", "retryable_failed")
                                    ),
                                    "last_error_code": max(err_codes) if err_codes else None,
                                }
                            )
                    last_rows = out
                    return

                raise AssertionError(f"Unhandled SQL in in-memory DB: {s}")

            async def _fetchone() -> dict[str, Any] | None:
                return last_rows[0] if last_rows else None

            async def _fetchall() -> list[dict[str, Any]]:
                return list(last_rows)

            cur.execute.side_effect = _execute
            cur.fetchone.side_effect = _fetchone
            cur.fetchall.side_effect = _fetchall
            yield cur

        conn.transaction.side_effect = _tx
        conn.cursor.side_effect = _cursor
        return conn


# ---------------------------------------------------------------------------
# 1. Migration d062 & Feature Flag Default Tests
# ---------------------------------------------------------------------------


def test_d062_migration_single_head_and_offline_sql_dalton_only() -> None:
    """d062 anchors cleanly on d061, forms a single Alembic head, and touches only dalton_* tables."""
    assert d062_mig.revision == "d062"
    assert d062_mig.down_revision == "d061"

    alembic_ini = REPO_ROOT / "src" / "dalton" / "alembic.ini"
    cfg = AlembicConfig(str(alembic_ini))
    cfg.set_main_option("script_location", str(REPO_ROOT / "src" / "dalton" / "migrations"))
    script_dir = ScriptDirectory.from_config(cfg)
    assert script_dir.get_heads() == ["d062"]
    rev = script_dir.get_revision("d062")
    assert rev is not None and rev.down_revision == "d061"

    up_proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(alembic_ini), "upgrade", "d061:d062", "--sql"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    up_sql = up_proc.stdout
    assert "CREATE TABLE IF NOT EXISTS dalton_failure_notices" in up_sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_dalton_failure_notices_dedup_key" in up_sql
    assert "CREATE TABLE IF NOT EXISTS dalton_failure_notice_streaks" in up_sql
    assert "CREATE TABLE IF NOT EXISTS dalton_failure_notice_targets" in up_sql
    assert "CREATE INDEX IF NOT EXISTS idx_dalton_failure_notice_targets_work" in up_sql
    assert "ALTER TABLE automation_executions" not in up_sql
    assert "ALTER TABLE dalton_notify_deliveries" not in up_sql

    down_proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(alembic_ini), "downgrade", "d062:d061", "--sql"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    down_sql = down_proc.stdout
    assert "DROP TABLE IF EXISTS dalton_failure_notice_targets" in down_sql
    assert "DROP TABLE IF EXISTS dalton_failure_notice_streaks" in down_sql
    assert "DROP TABLE IF EXISTS dalton_failure_notices" in down_sql


@pytest.mark.asyncio
async def test_failure_notice_switch_defaults_false_and_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DALTON_FAILURE_NOTICE_ENABLED", raising=False)
    assert dalton_settings.is_failure_notice_enabled() is False

    db = _InMemoryFailureNoticeDB()
    conn = db.make_conn()
    res = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="personal_automation",
        subject_id=str(uuid4()),
        source_key=str(uuid4()),
        recipient_user_id="user-1",
        failure_reason="plan_limit",
        targets=["feishu:user:ou_alice"],
    )
    assert res is None
    assert db.notices == {}
    assert db.targets == {}

    epoch = await fr_mod.bump_failure_notice_recovery_epoch_in_tx(
        conn, subject_kind="personal_automation", source_key="auto-1"
    )
    assert epoch is None
    assert db.streaks == {}


# ---------------------------------------------------------------------------
# 2. Terminal Rejection Codes & sync_terminal_undelivered_execution Guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_code",
    [
        "weixin_context_token_stale",
        "not_allowed",
        "http_403",
        "dm_binding_missing",
        "weixin_session_expired",
        "card_limit_exceeded",
    ],
)
async def test_terminal_rejection_code_transitions_directly_to_terminal_failed(
    monkeypatch: pytest.MonkeyPatch,
    terminal_code: str,
) -> None:
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    db = _InMemoryFailureNoticeDB()
    conn = db.make_conn()
    exec_id = str(uuid4())
    auto_id = str(uuid4())

    enq = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="personal_automation",
        subject_id=exec_id,
        source_key=auto_id,
        recipient_user_id="user-1",
        failure_reason="plan_limit",
        targets=["feishu:user:ou_alice"],
    )
    assert enq is not None and enq["aggregate_status"] == "retrying"

    claimed = await fr_mod.claim_due_failure_notice_targets(limit=5, conn=conn)
    assert len(claimed) == 1
    tr = claimed[0]
    assert tr["attempt_count"] == 1

    final_st = await fr_mod.settle_failure_notice_target_attempt(
        target_receipt_id=tr["target_receipt_id"],
        claim_token=tr["claim_token"],
        transport_res={
            "status": "failed",
            "error_code": terminal_code,
            "error_message": f"Rejected: {terminal_code}",
            "sent_chunks": 0,
        },
        conn=conn,
    )
    assert final_st == "terminal_failed"
    assert db.targets[tr["target_receipt_id"]]["status"] == "terminal_failed"
    assert db.targets[tr["target_receipt_id"]]["claim_token"] is None
    assert db.notices[enq["notice_id"]]["aggregate_status"] == "failed"


@pytest.mark.asyncio
async def test_sync_terminal_undelivered_execution_blocks_non_completed_executions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed executions must never have delivery_result or result_excerpt overwritten."""
    exec_uuid = str(uuid4())
    executed_sqls: list[str] = []

    cursor_double = create_autospec(psycopg.AsyncCursor, instance=True)

    async def _exec(sql: Any, _params: Any = None) -> None:
        executed_sqls.append(" ".join(str(sql).split()))

    cursor_double.execute.side_effect = _exec
    cursor_double.fetchone.return_value = {"status": "failed"}

    conn_double = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _cur_cm(*_a: Any, **_kw: Any):
        yield cursor_double

    conn_double.cursor.side_effect = _cur_cm

    @asynccontextmanager
    async def _fake_conn(_conn: Any = None):
        yield conn_double

    monkeypatch.setattr(dispatch_mod, "get_db_connection", _fake_conn)
    record_spy = create_autospec(exec_db.record_delivery, return_value=None)
    monkeypatch.setattr(exec_db, "record_delivery", record_spy)

    updated = await dispatch_mod.sync_terminal_undelivered_execution(
        exec_uuid,
        rows=[
            {
                "channel": "feishu",
                "target": "feishu:user:ou_alice",
                "status": "failed",
                "error_code": "not_allowed",
                "attempt_count": 3,
                "payload_json": {},
            }
        ],
    )
    assert updated is False
    assert record_spy.call_count == 0
    assert not any("UPDATE automation_executions" in sql for sql in executed_sqls)


# ---------------------------------------------------------------------------
# 3. Late Claim Rejection (claim_token + claim_expires_at Lease Fence)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_late_claim_after_60s_reconcile_and_retry_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attempt 1 stalls >60s -> reconciler clears claim_token & schedules attempt 2 ->
    late attempt 1 arrives with old claim_token and is denied with claim_expired_or_superseded."""
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    db = _InMemoryFailureNoticeDB(now=NOW)
    db.add_identity("user-1", "feishu", "ou_alice")
    conn = db.make_conn()

    enq = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="personal_automation",
        subject_id=str(uuid4()),
        source_key=str(uuid4()),
        recipient_user_id="user-1",
        failure_reason="usage_limit",
        targets=["feishu:user:ou_alice"],
    )
    assert enq is not None
    tr_id = enq["target_receipt_ids"][0]

    # Attempt 1 claimed with token_1 (TTL 60s)
    claimed_1 = await fr_mod.claim_due_failure_notice_targets(limit=5, claim_ttl_seconds=60, conn=conn)
    assert len(claimed_1) == 1
    token_1 = claimed_1[0]["claim_token"]

    # Advance past 60s lease expiry (attempt 1 never reached gateway -> no channel delivery row)
    db.now = NOW + timedelta(seconds=65)
    stats = await fr_mod.reconcile_uncertain_failure_notice_targets(now=db.now, conn=conn)
    assert stats["retryable_failed"] == 1
    assert db.targets[tr_id]["status"] == "retryable_failed"
    assert db.targets[tr_id]["claim_token"] is None

    # Advance past backoff so attempt 2 can be claimed with a fresh token_2
    db.now = db.now + timedelta(seconds=65)
    claimed_2 = await fr_mod.claim_due_failure_notice_targets(limit=5, claim_ttl_seconds=60, conn=conn)
    assert len(claimed_2) == 1
    token_2 = claimed_2[0]["claim_token"]
    assert token_2 != token_1
    assert claimed_2[0]["attempt_count"] == 2

    # Late attempt 1 finally arrives at gateway -> DENIED with claim_expired_or_superseded
    late_res = await claims_mod.authorize_and_claim_failure_notice(
        target_receipt_id=tr_id,
        claim_token=token_1,
        conn=conn,
        now=db.now,
    )
    assert late_res["claim_status"] == "denied"
    assert late_res["code"] == "claim_expired_or_superseded"
    assert fr_mod.failure_notice_delivery_key(tr_id) not in db.channel_deliveries

    # Active attempt 2 with valid token_2 -> CLAIMED!
    ok_res = await claims_mod.authorize_and_claim_failure_notice(
        target_receipt_id=tr_id,
        claim_token=token_2,
        conn=conn,
        now=db.now,
    )
    assert ok_res["claim_status"] == "claimed"
    assert ok_res["recipient_id"] == "ou_alice"
    assert ok_res["platform"] == "feishu"
    assert fr_mod.failure_notice_delivery_key(tr_id) in db.channel_deliveries


# ---------------------------------------------------------------------------
# 4. Partial Delivery, Unknown 15-min Seal, and Zero-Sent vs Partial-Sent Failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_partial_converges_to_sent_without_resend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    db = _InMemoryFailureNoticeDB(now=NOW)
    conn = db.make_conn()

    enq = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="personal_automation",
        subject_id=str(uuid4()),
        source_key=str(uuid4()),
        recipient_user_id="user-1",
        failure_reason="claude_plan",
        targets=["discord:user:123456789012345678"],
    )
    assert enq is not None
    tr_id = enq["target_receipt_ids"][0]
    claimed = await fr_mod.claim_due_failure_notice_targets(limit=5, conn=conn)
    assert len(claimed) == 1

    # Simulate HTTP timeout leaving target uncertain, while gateway delivered partial chunks
    await fr_mod.settle_failure_notice_target_attempt(
        target_receipt_id=tr_id,
        claim_token=claimed[0]["claim_token"],
        transport_res={"status": "unknown", "uncertain": True, "error_code": "delivery_uncertain"},
        conn=conn,
    )
    assert db.targets[tr_id]["status"] == "uncertain"

    db.channel_deliveries[fr_mod.failure_notice_delivery_key(tr_id)] = {
        "delivery_key": fr_mod.failure_notice_delivery_key(tr_id),
        "status": "partial",
        "sent_chunks": 1,
        "platform_message_ids": ["msg_part_1"],
        "error_code": None,
        "error_message": None,
    }

    db.now = NOW + timedelta(seconds=65)
    stats = await fr_mod.reconcile_uncertain_failure_notice_targets(now=db.now, conn=conn)
    assert stats["sent"] == 1
    assert db.targets[tr_id]["status"] == "sent"
    assert db.targets[tr_id]["payload_json"]["partial_delivery"] is True
    assert db.notices[enq["notice_id"]]["aggregate_status"] == "delivered"

    # Must never be re-claimed for resend
    reclaimed = await fr_mod.claim_due_failure_notice_targets(limit=5, conn=conn)
    assert reclaimed == []


@pytest.mark.asyncio
async def test_reconcile_unknown_stays_uncertain_then_seals_at_15m(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    db = _InMemoryFailureNoticeDB(now=NOW)
    conn = db.make_conn()

    enq = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="data_task",
        subject_id=str(uuid4()),
        source_key="pm-market-state",
        recipient_user_id="admin-1",
        failure_reason="subscription_quota_exhausted",
        targets=["feishu:user:ou_admin"],
    )
    assert enq is not None
    tr_id = enq["target_receipt_ids"][0]
    claimed = await fr_mod.claim_due_failure_notice_targets(limit=5, conn=conn)
    await fr_mod.settle_failure_notice_target_attempt(
        target_receipt_id=tr_id,
        claim_token=claimed[0]["claim_token"],
        transport_res={"status": "unknown", "uncertain": True, "error_code": "unknown"},
        conn=conn,
    )
    db.channel_deliveries[fr_mod.failure_notice_delivery_key(tr_id)] = {
        "delivery_key": fr_mod.failure_notice_delivery_key(tr_id),
        "status": "unknown",
        "sent_chunks": 0,
        "platform_message_ids": [],
        "error_code": "unknown",
        "error_message": None,
    }

    # At +5 minutes -> still uncertain
    db.now = NOW + timedelta(minutes=5)
    st_5m = await fr_mod.reconcile_uncertain_failure_notice_targets(now=db.now, conn=conn)
    assert st_5m["still_uncertain"] == 1
    assert db.targets[tr_id]["status"] == "uncertain"

    # At +15 minutes -> sealed to terminal_failed with delivery_outcome_unknown_timeout
    db.now = NOW + timedelta(minutes=15)
    st_15m = await fr_mod.reconcile_uncertain_failure_notice_targets(now=db.now, conn=conn)
    assert st_15m["terminal_failed"] == 1
    assert db.targets[tr_id]["status"] == "terminal_failed"
    assert db.targets[tr_id]["error_code"] == "delivery_outcome_unknown_timeout"
    assert db.notices[enq["notice_id"]]["aggregate_status"] == "failed"


@pytest.mark.asyncio
async def test_reconcile_failed_only_retries_when_sent_chunks_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    db = _InMemoryFailureNoticeDB(now=NOW)
    conn = db.make_conn()

    enq = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="personal_automation",
        subject_id=str(uuid4()),
        source_key=str(uuid4()),
        recipient_user_id="user-1",
        failure_reason="plan_limit",
        targets=["feishu:user:ou_alice", "discord:user:123456789012345678"],
    )
    assert enq is not None
    tr_fs, tr_dc = enq["target_receipt_ids"]
    await fr_mod.claim_due_failure_notice_targets(limit=5, conn=conn)

    # Feishu: failed with sent_chunks=0 (transient unavailable) -> retryable_failed
    db.channel_deliveries[fr_mod.failure_notice_delivery_key(tr_fs)] = {
        "delivery_key": fr_mod.failure_notice_delivery_key(tr_fs),
        "status": "failed",
        "sent_chunks": 0,
        "platform_message_ids": [],
        "error_code": "unavailable",
        "error_message": "temporary 503",
    }
    # Discord: failed with sent_chunks=1 (partial already delivered) -> terminal_failed (no resend)
    db.channel_deliveries[fr_mod.failure_notice_delivery_key(tr_dc)] = {
        "delivery_key": fr_mod.failure_notice_delivery_key(tr_dc),
        "status": "failed",
        "sent_chunks": 1,
        "platform_message_ids": ["dc_msg_1"],
        "error_code": "unavailable",
        "error_message": "chunk 2 failed",
    }

    db.now = NOW + timedelta(seconds=65)
    stats = await fr_mod.reconcile_uncertain_failure_notice_targets(now=db.now, conn=conn)
    assert stats["retryable_failed"] == 1
    assert stats["terminal_failed"] == 1
    assert db.targets[tr_fs]["status"] == "retryable_failed"
    assert db.targets[tr_dc]["status"] == "terminal_failed"
    assert db.targets[tr_dc]["payload_json"]["partial_delivery"] is True


# ---------------------------------------------------------------------------
# 5. Dedup against delivery.held (automation_finish) & Anti-Spam Unique Key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_against_held_automation_finish_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    db = _InMemoryFailureNoticeDB(now=NOW)
    conn = db.make_conn()

    held_delivery = automation_delivery.Delivery(
        id=str(uuid4()),
        targets=[
            automation_delivery.Target(
                entry="feishu:@me",
                address="feishu:@me",
                name="Alice DM",
                ok=True,
            ),
        ],
    )

    # Owner has both Feishu DM (covered by feishu:@me) and Discord DM (not covered)
    res_partial = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="personal_automation",
        subject_id=str(uuid4()),
        source_key=str(uuid4()),
        recipient_user_id="user-1",
        failure_reason="usage_limit",
        delivery=held_delivery,
        targets=["feishu:user:ou_alice", "discord:user:123456789012345678"],
    )
    assert res_partial is not None
    assert res_partial["aggregate_status"] == "retrying"
    assert len(res_partial["target_receipt_ids"]) == 1
    only_target = db.targets[res_partial["target_receipt_ids"][0]]
    assert only_target["channel"] == "discord"

    # When ALL owner targets are covered by automation_finish -> not_applicable, 0 target rows
    res_all_covered = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="personal_automation",
        subject_id=str(uuid4()),
        source_key=str(uuid4()),
        recipient_user_id="user-1",
        failure_reason="usage_limit",
        delivery=held_delivery,
        targets=["feishu:user:ou_alice"],
    )
    assert res_all_covered is not None
    assert res_all_covered["aggregate_status"] == "not_applicable"
    assert res_all_covered["target_receipt_ids"] == []


@pytest.mark.asyncio
async def test_anti_spam_dedup_suppresses_consecutive_failures_and_resets_after_recovery_or_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    db = _InMemoryFailureNoticeDB(now=NOW)
    conn = db.make_conn()
    source_id = str(uuid4())

    # 1st failure wins slot for plan_limit:epoch:0
    r1 = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="personal_automation",
        subject_id=str(uuid4()),
        source_key=source_id,
        recipient_user_id="user-1",
        failure_reason="plan_limit",
        targets=["feishu:user:ou_alice"],
    )
    assert r1 is not None and r1["aggregate_status"] == "retrying" and not r1["suppressed"]

    # 2nd consecutive failure hits partial unique index -> suppressed with dedup_window_key=NULL
    r2 = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="personal_automation",
        subject_id=str(uuid4()),
        source_key=source_id,
        recipient_user_id="user-1",
        failure_reason="plan_limit",
        targets=["feishu:user:ou_alice"],
    )
    assert r2 is not None and r2["aggregate_status"] == "suppressed" and r2["suppressed"] is True
    assert r2["target_receipt_ids"] == []

    # After 1st notice fails terminally, partial unique index frees slot -> 3rd failure can alert!
    claimed = await fr_mod.claim_due_failure_notice_targets(limit=5, conn=conn)
    await fr_mod.settle_failure_notice_target_attempt(
        target_receipt_id=claimed[0]["target_receipt_id"],
        claim_token=claimed[0]["claim_token"],
        transport_res={"status": "failed", "error_code": "not_allowed", "sent_chunks": 0},
        conn=conn,
    )
    assert db.notices[r1["notice_id"]]["aggregate_status"] == "failed"

    r3 = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="personal_automation",
        subject_id=str(uuid4()),
        source_key=source_id,
        recipient_user_id="user-1",
        failure_reason="plan_limit",
        targets=["feishu:user:ou_alice"],
    )
    assert r3 is not None and r3["aggregate_status"] == "retrying" and not r3["suppressed"]

    # Bump recovery epoch on success -> 4th failure gets epoch:1 and alerts even if r3 succeeded!
    epoch = await fr_mod.bump_failure_notice_recovery_epoch_in_tx(
        conn, subject_kind="personal_automation", source_key=source_id
    )
    assert epoch == 1
    r4 = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="personal_automation",
        subject_id=str(uuid4()),
        source_key=source_id,
        recipient_user_id="user-1",
        failure_reason="plan_limit",
        targets=["feishu:user:ou_alice"],
    )
    assert r4 is not None and r4["aggregate_status"] == "retrying"
    assert r4["dedup_window_key"] == "plan_limit:epoch:1"


# ---------------------------------------------------------------------------
# 6. All Four subject_kinds Authorize & Claim Cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "subject_kind",
    ["personal_automation", "company_automation", "data_task", "wakeup"],
)
async def test_all_four_subject_kinds_plan_and_claim_authorize(
    monkeypatch: pytest.MonkeyPatch,
    subject_kind: str,
) -> None:
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    db = _InMemoryFailureNoticeDB(now=NOW)
    recipient = f"user-{subject_kind}"
    open_id = f"ou_{subject_kind}"
    db.add_identity(recipient, "feishu", open_id)
    src_key = f"src-{subject_kind}"
    if subject_kind == "data_task":
        db.data_task_defs[src_key] = {"enabled": True, "notify_enabled": True}
        monkeypatch.setattr(
            dispatch_mod,
            "evaluate_user_notify_eligibility",
            create_autospec(
                dispatch_mod.evaluate_user_notify_eligibility,
                return_value=(True, None, [f"feishu:user:{open_id}"]),
            ),
        )
    conn = db.make_conn()

    enq = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind=subject_kind,
        subject_id=str(uuid4()),
        source_key=src_key,
        recipient_user_id=recipient,
        failure_reason="subscription_quota_exhausted",
        targets=[f"feishu:user:{open_id}"],
    )
    assert enq is not None
    tr_id = enq["target_receipt_ids"][0]
    claimed_rows = await fr_mod.claim_due_failure_notice_targets(limit=5, conn=conn)
    assert len(claimed_rows) == 1
    tok = claimed_rows[0]["claim_token"]

    plan = await claims_mod.authorize_failure_notice_plan(
        target_receipt_id=tr_id,
        claim_token=tok,
        conn=conn,
        now=db.now,
    )
    assert plan["authorized"] is True
    assert plan["subject_kind"] == subject_kind
    assert plan["items"][0]["delivery_key"] == fr_mod.failure_notice_delivery_key(tr_id)

    claim = await claims_mod.authorize_and_claim_failure_notice(
        target_receipt_id=tr_id,
        claim_token=tok,
        conn=conn,
        now=db.now,
    )
    assert claim["claim_status"] == "claimed"
    assert claim["subject_kind"] == subject_kind
    assert claim["recipient_id"] == open_id


# ---------------------------------------------------------------------------
# 7. Execution List failure_notice Enrichment & No-Token Logging Guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execution_delivery_status_attaches_failure_notice_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _InMemoryFailureNoticeDB(now=NOW)
    conn = db.make_conn()
    exec_uuid = uuid4()
    auto_uuid = uuid4()

    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    enq = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="personal_automation",
        subject_id=str(exec_uuid),
        source_key=str(auto_uuid),
        recipient_user_id="user-1",
        failure_reason="plan_limit",
        targets=["feishu:user:ou_alice"],
    )
    assert enq is not None

    monkeypatch.setattr(
        eds_mod,
        "load_execution_notify_rows",
        create_autospec(eds_mod.load_execution_notify_rows, return_value={}),
    )

    resp = AutomationExecutionResponse(
        automation_execution_id=exec_uuid,
        automation_id=auto_uuid,
        status="failed",
        scheduled_at=NOW,
        created_at=NOW,
        failure_reason="plan_limit",
    )
    await eds_mod.enrich_executions_with_delivery_summary([resp], conn=conn)
    assert isinstance(resp.failure_notice, FailureNoticeSummary)
    assert resp.failure_notice.notice_id == enq["notice_id"]
    assert resp.failure_notice.status == "retrying"
    assert resp.failure_notice.total_targets == 1
    assert resp.failure_notice.retrying_count == 1

    # When flag is OFF, failure_notice stays None and load_failure_notice_summaries is not called
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "0")
    load_spy = create_autospec(fr_mod.load_failure_notice_summaries, return_value={})
    monkeypatch.setattr(fr_mod, "load_failure_notice_summaries", load_spy)
    resp_off = AutomationExecutionResponse(
        automation_execution_id=exec_uuid,
        automation_id=auto_uuid,
        status="failed",
        scheduled_at=NOW,
        created_at=NOW,
    )
    await eds_mod.enrich_executions_with_delivery_summary([resp_off], conn=conn)
    assert resp_off.failure_notice is None
    assert load_spy.call_count == 0


@pytest.mark.asyncio
async def test_gateway_transport_never_logs_claim_token_or_service_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tr_id = str(uuid4())
    secret_claim_tok = "11111111-2222-4333-8444-555555555555"
    secret_svc_tok = "svc-super-secret-token-xyz"

    http_client = create_autospec(httpx.AsyncClient, instance=True)
    http_client.post.side_effect = httpx.ConnectError("refused")

    with caplog.at_level(logging.DEBUG, logger=transport_mod.logger.name):
        res = await transport_mod.post_failure_notice_gateway_notify(
            tr_id,
            secret_claim_tok,
            target="feishu:user:ou_alice",
            gateway_url="http://127.0.0.1:8962",
            service_token=secret_svc_tok,
            http_client=http_client,
        )
    assert res["status"] == "failed"
    assert res["error_code"] == "ConnectError"
    all_logs = "\n".join(r.getMessage() for r in caplog.records)
    assert secret_claim_tok not in all_logs
    assert secret_svc_tok not in all_logs


@pytest.mark.asyncio
async def test_automation_settlement_invokes_in_tx_failure_notice_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``automation_settlement.settle`` invokes ``on_automation_settle_in_tx`` inside
    ``leave_finish`` (same transaction as ``settle_execution``) and wakes dispatcher after commit."""
    from src.dalton.database import automation_recycle as recycle_mod
    from src.server.services import automation_outcomes as outcomes_mod
    from src.server.services import automation_settlement as settle_mod
    from src.server.services import webhook_client as wh_mod

    exec_id = str(uuid4())
    auto_id = str(uuid4())
    automation = {
        "automation_id": auto_id,
        "user_id": "user-settle-1",
        "name": "Morning Quota Task",
        "trigger_type": "cron",
    }
    in_tx_calls: list[dict[str, Any]] = []
    wake_calls: list[int] = []

    async def _fake_on_settle(conn: Any, **kwargs: Any) -> dict[str, Any] | None:
        in_tx_calls.append({"conn": conn, **kwargs})
        return {"notice_id": str(uuid4()), "aggregate_status": "retrying"}

    def _fake_wake() -> None:
        wake_calls.append(1)

    monkeypatch.setattr(
        fr_mod,
        "on_automation_settle_in_tx",
        create_autospec(fr_mod.on_automation_settle_in_tx, side_effect=_fake_on_settle),
    )
    monkeypatch.setattr(
        fr_mod,
        "wake_failure_notice_dispatcher",
        create_autospec(fr_mod.wake_failure_notice_dispatcher, side_effect=_fake_wake),
    )
    monkeypatch.setattr(
        recycle_mod,
        "automation_is_soft_deleted",
        create_autospec(recycle_mod.automation_is_soft_deleted, return_value=False),
    )
    wh_double = create_autospec(wh_mod.WebhookClient, instance=True)
    wh_double.fire_event.return_value = None
    monkeypatch.setattr(settle_mod, "WebhookClient", lambda: wh_double)

    tx_conn = create_autospec(psycopg.AsyncConnection, instance=True)

    async def _fake_settle_execution(
        eid: str,
        *,
        in_transaction: Any = None,
        **_kw: Any,
    ) -> dict[str, Any]:
        settled_row = {
            "automation_execution_id": eid,
            "conversation_thread_id": None,
            "conversation_response_id": None,
            "delivery": None,
            "settled_from": "running",
        }
        if in_transaction is not None:
            await in_transaction(tx_conn, settled_row)
        return settled_row

    monkeypatch.setattr(
        exec_db,
        "settle_execution",
        create_autospec(exec_db.settle_execution, side_effect=_fake_settle_execution),
    )

    ok = await settle_mod.settle(
        automation,
        exec_id,
        outcomes_mod.Outcome.SUBSCRIPTION_LIMITED,
        error="Subscription quota exhausted",
    )
    assert ok is True
    assert len(in_tx_calls) == 1
    assert in_tx_calls[0]["conn"] is tx_conn
    assert in_tx_calls[0]["outcome_status"] == "failed"
    assert in_tx_calls[0]["failure_reason"] == "plan_limit"
    assert len(wake_calls) == 1


@pytest.mark.asyncio
async def test_channels_router_internal_failure_notice_plan_and_claim_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Internal failure-notice plan & I-6 claim routes enforce service token and forward claim_token."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.dalton.app.channels_router import channels_router
    from src.dalton.database import channel_gateway as cg_db

    svc_tok = "s" * 40
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", svc_tok)
    tr_id = str(uuid4())
    tok = str(uuid4())

    plan_spy = create_autospec(
        claims_mod.authorize_failure_notice_plan,
        return_value={"authorized": True, "status": "authorized", "target_receipt_id": tr_id},
    )
    claim_spy = create_autospec(
        claims_mod.authorize_and_claim_failure_notice,
        return_value={"claim_status": "claimed", "platform": "feishu", "recipient_id": "ou_alice"},
    )
    monkeypatch.setattr(claims_mod, "authorize_failure_notice_plan", plan_spy)
    monkeypatch.setattr(claims_mod, "authorize_and_claim_failure_notice", claim_spy)
    monkeypatch.setattr(cg_db, "reject_company_system_auth", lambda _u: None)

    app = FastAPI()
    app.include_router(channels_router, prefix="/api/v1/dalton")
    client = TestClient(app)

    r_plan = client.post(
        "/api/v1/dalton/channels/internal/failure-notices/plan",
        headers={"X-Service-Token": svc_tok},
        json={"target_receipt_id": tr_id, "claim_token": tok},
    )
    assert r_plan.status_code == 200
    assert r_plan.json()["authorized"] is True
    plan_spy.assert_awaited_once_with(target_receipt_id=tr_id, claim_token=tok)

    r_claim = client.post(
        "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
        headers={"X-Service-Token": svc_tok},
        json={
            "delivery_key": fr_mod.failure_notice_delivery_key(tr_id),
            "kind": "failure_notice",
            "claim_token": tok,
            "run_id": tr_id,
            "target": "feishu:user:ou_alice",
            "has_non_empty_text": True,
            "total_chunks": 1,
        },
    )
    assert r_claim.status_code == 200
    assert r_claim.json()["claim_status"] == "claimed"
    assert claim_spy.await_count == 1
    assert claim_spy.await_args.kwargs["claim_token"] == tok


@pytest.mark.asyncio
async def test_data_job_finalize_task_run_enqueues_failure_notice_and_bumps_epoch_in_tx(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``data_jobs/scheduler.py:finalize_task_run`` calls ``enqueue_failure_notice_in_tx`` on
    non-trial failure and ``bump_failure_notice_recovery_epoch_in_tx`` on non-trial success,
    strictly inside the terminal ``conn.transaction()`` block."""
    from src.dalton.data_jobs import container_runner as cr_mod
    from src.dalton.data_jobs import evaluators as eval_mod
    from src.dalton.data_jobs import scheduler as sched_mod

    in_tx_active = {"flag": False}
    enq_events: list[tuple[bool, dict[str, Any]]] = []
    bump_events: list[tuple[bool, dict[str, Any]]] = []
    wake_calls: list[int] = []

    async def _fake_enq(conn: Any, **kwargs: Any) -> dict[str, Any]:
        enq_events.append((in_tx_active["flag"], {"conn": conn, **kwargs}))
        return {"notice_id": str(uuid4()), "aggregate_status": "retrying"}

    async def _fake_bump(conn: Any, **kwargs: Any) -> int:
        bump_events.append((in_tx_active["flag"], {"conn": conn, **kwargs}))
        return 1

    monkeypatch.setattr(
        fr_mod,
        "enqueue_failure_notice_in_tx",
        create_autospec(fr_mod.enqueue_failure_notice_in_tx, side_effect=_fake_enq),
    )
    monkeypatch.setattr(
        fr_mod,
        "bump_failure_notice_recovery_epoch_in_tx",
        create_autospec(fr_mod.bump_failure_notice_recovery_epoch_in_tx, side_effect=_fake_bump),
    )
    monkeypatch.setattr(
        fr_mod,
        "wake_failure_notice_dispatcher",
        create_autospec(fr_mod.wake_failure_notice_dispatcher, side_effect=lambda: wake_calls.append(1)),
    )
    monkeypatch.setattr(
        sched_mod,
        "check_scheduler_lock",
        create_autospec(sched_mod.check_scheduler_lock, return_value=None),
    )
    monkeypatch.setattr(
        sched_mod,
        "freeze_task_run_artifacts",
        create_autospec(sched_mod.freeze_task_run_artifacts, return_value=None),
    )

    conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _tx():
        in_tx_active["flag"] = True
        try:
            yield
        finally:
            in_tx_active["flag"] = False

    @asynccontextmanager
    async def _cur(*_a: Any, **_kw: Any):
        cur = create_autospec(psycopg.AsyncCursor, instance=True)
        cur.rowcount = 1
        cur.fetchone.return_value = {"consecutive_failures": 1}
        yield cur

    conn.transaction.side_effect = _tx
    conn.cursor.side_effect = _cur
    lock_conn = create_autospec(psycopg.AsyncConnection, instance=True)

    paths = cr_mod.DataTaskPathMap(
        host_var_dir=tmp_path,
        backend_var_dir=tmp_path,
    )
    claimed = sched_mod.ClaimedTaskRun(
        run_id=str(uuid4()),
        job_key="daily-macro-fetch",
        generation=1,
        runner="container",
        command_argv=["python", "-c", "pass"],
        workdir="/tmp",
        timeout_seconds=300,
        no_output_timeout_seconds=120,
        required_secrets=[],
        cos_mode="disabled",
        notify_enabled=True,
        scheduled_slot_utc=NOW,
        deadline_utc=NOW + timedelta(minutes=10),
        is_trial=False,
        trigger_source="cron",
    )
    outcome = cr_mod.ContainerInspectionOutcome(
        container_id=None,
        running=False,
        exit_code=1,
        oom_killed=False,
        log_tail="quota error",
    )

    # 1. Non-trial failure -> enqueues inside tx and wakes dispatcher after commit
    committed, _cf = await sched_mod.finalize_task_run(
        conn,
        lock_conn=lock_conn,
        docker_client=None,
        claimed=claimed,
        container_id=None,
        outcome=outcome,
        evaluation=eval_mod.TaskEvaluationResult(
            ok=False,
            status="failed",
            failure_reason="subscription_quota_exhausted",
        ),
        path_map=paths,
    )
    assert committed is True
    assert len(enq_events) == 1
    assert enq_events[0][0] is True  # strictly inside conn.transaction()
    assert enq_events[0][1]["subject_kind"] == "data_task"
    assert enq_events[0][1]["source_key"] == "daily-macro-fetch"
    assert enq_events[0][1]["failure_reason"] == "subscription_quota_exhausted"
    assert len(wake_calls) == 1

    # 2. Non-trial success -> bumps recovery_epoch inside tx
    committed_ok, _cf2 = await sched_mod.finalize_task_run(
        conn,
        lock_conn=lock_conn,
        docker_client=None,
        claimed=claimed,
        container_id=None,
        outcome=cr_mod.ContainerInspectionOutcome(
            container_id=None,
            running=False,
            exit_code=0,
            oom_killed=False,
            log_tail="",
        ),
        evaluation=eval_mod.TaskEvaluationResult(ok=True, status="succeeded"),
        path_map=paths,
    )
    assert committed_ok is True
    assert len(bump_events) == 1
    assert bump_events[0][0] is True  # strictly inside conn.transaction()
    assert bump_events[0][1]["subject_kind"] == "data_task"
    assert bump_events[0][1]["source_key"] == "daily-macro-fetch"


# ---------------------------------------------------------------------------
# 8. Round 2 Review Fixes (Sweeper, Rollback, Per-Target Claim, Data-Task Push
#    Permissions, Weixin Lock Order, Stable Dedup Key, Script List APIs, Seal)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_notice_sweeper_periodic_scan_and_switch_off_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Periodic sweeper runs independently without preflight traffic and is a strict no-op when OFF."""
    import asyncio

    calls: list[dict[str, Any]] = []

    async def _fake_process(**kwargs: Any) -> dict[str, int]:
        calls.append(kwargs)
        return {"reconciled": 1, "claimed": 1, "sent": 1, "failed": 0, "uncertain": 0}

    proc_spy = create_autospec(fr_mod.process_due_failure_notices, side_effect=_fake_process)
    sweeper = fr_mod.FailureNoticeSweeper(interval_s=0.05, limit=7, max_duration_s=10.0, process_fn=proc_spy)

    # Switch OFF -> zero calls
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "0")
    off_res = await sweeper.run_once()
    assert off_res["claimed"] == 0
    assert proc_spy.await_count == 0

    # Switch ON -> periodic background loop ticks without any preflight activity
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    sweeper.start()
    try:
        for _ in range(20):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.03)
    finally:
        await sweeper.stop()
    assert len(calls) >= 2
    assert calls[0]["limit"] == 7


@pytest.mark.asyncio
async def test_on_automation_settle_in_tx_propagates_sql_error_for_rollback_and_is_noop_when_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When enabled, enqueue SQL failures propagate so outer settlement rolls back; when OFF, enqueue never runs."""
    from src.dalton.database import automation_recycle as recycle_mod

    monkeypatch.setattr(
        recycle_mod,
        "automation_is_soft_deleted",
        create_autospec(recycle_mod.automation_is_soft_deleted, return_value=False),
    )
    enq_boom = create_autospec(
        fr_mod.enqueue_failure_notice_in_tx,
        side_effect=psycopg.OperationalError("lock timeout"),
    )
    monkeypatch.setattr(fr_mod, "enqueue_failure_notice_in_tx", enq_boom)

    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    automation = {
        "automation_id": str(uuid4()),
        "user_id": "user-rollback-1",
        "name": "Rollback Test Task",
    }

    # Switch OFF -> strict no-op, enqueue never invoked
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "0")
    res_off = await fr_mod.on_automation_settle_in_tx(
        conn,
        automation=automation,
        execution_id=str(uuid4()),
        outcome_status="failed",
        failure_reason="plan_limit",
    )
    assert res_off is None
    assert enq_boom.await_count == 0

    # Switch ON -> OperationalError MUST propagate (never swallowed)
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    with pytest.raises(psycopg.OperationalError, match="lock timeout"):
        await fr_mod.on_automation_settle_in_tx(
            conn,
            automation=automation,
            execution_id=str(uuid4()),
            outcome_status="failed",
            failure_reason="plan_limit",
        )
    assert enq_boom.await_count == 1


@pytest.mark.asyncio
async def test_process_due_failure_notices_claims_per_target_respecting_time_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Targets are claimed one-by-one right before dispatch; targets remaining after budget expiry stay pending with attempt_count=0."""
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    db = _InMemoryFailureNoticeDB(now=NOW)
    conn = db.make_conn()

    enq = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="personal_automation",
        subject_id=str(uuid4()),
        source_key=str(uuid4()),
        recipient_user_id="user-budget",
        failure_reason="plan_limit",
        targets=["feishu:user:ou_t1", "discord:user:123456789012345678", "weixin:user:wx_t3"],
    )
    assert enq is not None and len(enq["target_receipt_ids"]) == 3
    tr_ids = list(enq["target_receipt_ids"])

    # Advance monotonic clock during first post so budget expires right after target #1
    clock = {"now": 100.0}
    monkeypatch.setattr(fr_mod.time, "monotonic", lambda: clock["now"])

    dispatched: list[str] = []

    async def _slow_post(
        target_receipt_id: str,
        claim_token: str,
        *,
        target: str,
        **_kw: Any,
    ) -> dict[str, Any]:
        _ = claim_token, target
        dispatched.append(target_receipt_id)
        # First target consumes the full 5s cycle budget
        clock["now"] += 6.0
        return {"ok": True, "status": "sent", "items_sent": 1, "items_total": 1}

    post_spy = create_autospec(fr_mod.post_failure_notice_gateway_notify, side_effect=_slow_post)
    stats = await fr_mod.process_due_failure_notices(
        limit=10,
        max_duration_s=5.0,
        post_fn=post_spy,
        conn=conn,
    )
    assert stats["claimed"] == 1
    assert stats["sent"] == 1
    assert len(dispatched) == 1

    # Remaining two targets MUST still be untouched: pending, attempt_count=0, claim_token=None
    untouched = [db.targets[tid] for tid in tr_ids if tid != dispatched[0]]
    assert len(untouched) == 2
    for t in untouched:
        assert t["status"] == "pending"
        assert t["attempt_count"] == 0
        assert t["claim_token"] is None
        assert t["claim_expires_at"] is None


@pytest.mark.asyncio
async def test_data_task_enqueue_and_claim_respect_push_settings_and_exclude_notify_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Data-task failure notices enforce push permissions at enqueue & claim, never fall back to bound DM,
    and exclude targets already covered by ``notify_on_failure``."""
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    db = _InMemoryFailureNoticeDB(now=NOW)
    admin_uid = "admin-user-1"
    admin_open_id = "ou_admin_1"
    db.add_identity(admin_uid, "feishu", admin_open_id)
    conn = db.make_conn()

    # 1. Data task has notify_enabled=False -> not_applicable, 0 targets (even though admin has bound DM)
    db.data_task_defs["job-disabled-notify"] = {"enabled": True, "notify_enabled": False}
    enq_disabled = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="data_task",
        subject_id=str(uuid4()),
        source_key="job-disabled-notify",
        recipient_user_id=admin_uid,
        failure_reason="exit_code_1",
        data_task_notify_enabled=False,
    )
    assert enq_disabled is not None
    assert enq_disabled["aggregate_status"] == "not_applicable"
    assert enq_disabled["target_receipt_ids"] == []

    # 2. User preferences disabled or empty targets -> never falls back to bound DM
    real_eval_elig = dispatch_mod.evaluate_user_notify_eligibility
    db.data_task_defs["job-empty-targets"] = {"enabled": True, "notify_enabled": True}
    monkeypatch.setattr(
        dispatch_mod,
        "evaluate_user_notify_eligibility",
        create_autospec(
            real_eval_elig,
            return_value=(False, "target_removed", []),
        ),
    )
    enq_empty = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="data_task",
        subject_id=str(uuid4()),
        source_key="job-empty-targets",
        recipient_user_id=admin_uid,
        failure_reason="exit_code_1",
        data_task_notify_enabled=True,
    )
    assert enq_empty is not None
    assert enq_empty["aggregate_status"] == "unconfigured"
    assert enq_empty["target_receipt_ids"] == []

    # 3. notify_on_failure active (notify_status='pending_dispatch') covers the same DM -> excluded (no double notify)
    db.data_task_defs["job-nof"] = {"enabled": True, "notify_enabled": True}
    monkeypatch.setattr(
        dispatch_mod,
        "evaluate_user_notify_eligibility",
        create_autospec(
            real_eval_elig,
            return_value=(True, None, [f"feishu:user:{admin_open_id}"]),
        ),
    )
    enq_nof = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="data_task",
        subject_id=str(uuid4()),
        source_key="job-nof",
        recipient_user_id=admin_uid,
        failure_reason="exit_code_1",
        data_task_notify_enabled=True,
        data_task_notify_status="pending_dispatch",
    )
    assert enq_nof is not None
    assert enq_nof["aggregate_status"] == "not_applicable"
    assert enq_nof["target_receipt_ids"] == []

    # 4. Eligible data task enqueues DM target, then task push is disabled before claim -> claim denies terminally
    db.data_task_defs["job-claim-recheck"] = {"enabled": True, "notify_enabled": True}
    enq_ok = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="data_task",
        subject_id=str(uuid4()),
        source_key="job-claim-recheck",
        recipient_user_id=admin_uid,
        failure_reason="exit_code_1",
        data_task_notify_enabled=True,
        data_task_notify_status="skipped",
    )
    assert enq_ok is not None and len(enq_ok["target_receipt_ids"]) == 1
    tr_id = enq_ok["target_receipt_ids"][0]
    claimed = await fr_mod.claim_due_failure_notice_targets(limit=5, conn=conn)
    tok = next(r["claim_token"] for r in claimed if r["target_receipt_id"] == tr_id)

    # Admin turns off per-task push between enqueue and claim
    db.data_task_defs["job-claim-recheck"]["notify_enabled"] = False
    plan_denied = await claims_mod.authorize_failure_notice_plan(
        target_receipt_id=tr_id,
        claim_token=tok,
        conn=conn,
        now=db.now,
    )
    assert plan_denied["authorized"] is False
    assert plan_denied["code"] == "task_notify_disabled"

    claim_denied = await claims_mod.authorize_and_claim_failure_notice(
        target_receipt_id=tr_id,
        claim_token=tok,
        conn=conn,
        now=db.now,
    )
    assert claim_denied["claim_status"] == "denied"
    assert claim_denied["code"] == "task_notify_disabled"
    assert fr_mod.is_failure_notice_terminal_error_code(claim_denied["code"]) is True


@pytest.mark.asyncio
async def test_weixin_claim_locks_account_for_key_share_before_delivery_for_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Weixin failure-notice retry locks ``dalton_channel_weixin_accounts FOR KEY SHARE``
    strictly BEFORE ``dalton_channel_deliveries FOR UPDATE`` (preventing AB-BA deadlock with unbind)."""
    from src.dalton.database import weixin_multi_account as wx_db
    from src.server.database import encryption as enc_mod

    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    db = _InMemoryFailureNoticeDB(now=NOW)
    uid = "wx-lock-user"
    wx_uid = "wx_user_lock_1@im.wechat"
    db.add_identity(uid, "weixin", wx_uid)
    wx_acc_id = db.weixin_accounts[uid]["account_id"]
    conn = db.make_conn()

    enq = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="personal_automation",
        subject_id=str(uuid4()),
        source_key=str(uuid4()),
        recipient_user_id=uid,
        failure_reason="plan_limit",
        targets=[f"weixin:user:{wx_uid}"],
    )
    assert enq is not None
    tr_id = enq["target_receipt_ids"][0]
    claimed = await fr_mod.claim_due_failure_notice_targets(limit=5, conn=conn)
    tok = claimed[0]["claim_token"]

    # Pre-seed an existing failed delivery row pointing to wx_acc_id (simulating a retry)
    d_key = fr_mod.failure_notice_delivery_key(tr_id)
    db.channel_deliveries[d_key] = {
        "delivery_key": d_key,
        "user_id": uid,
        "platform": "weixin",
        "target_address": "weixin:@me",
        "status": "failed",
        "sent_chunks": 0,
        "weixin_account_id": wx_acc_id,
    }

    monkeypatch.setattr(enc_mod, "get_encryption_key", lambda: "test-key")
    monkeypatch.setattr(
        wx_db,
        "resolve_weixin_account_context_token_in_tx",
        create_autospec(
            wx_db.resolve_weixin_account_context_token_in_tx,
            return_value=("ctx-token-ok", 1, False),
        ),
    )

    db.sql_log.clear()
    claim = await claims_mod.authorize_and_claim_failure_notice(
        target_receipt_id=tr_id,
        claim_token=tok,
        conn=conn,
        now=db.now,
    )
    assert claim["claim_status"] == "claimed"
    assert claim["weixin_account_id"] == wx_acc_id

    wx_lock_indices = [
        idx
        for idx, sql in enumerate(db.sql_log)
        if "FROM dalton_channel_weixin_accounts" in sql and "FOR KEY SHARE" in sql
    ]
    del_lock_indices = [
        idx
        for idx, sql in enumerate(db.sql_log)
        if "FROM dalton_channel_deliveries" in sql and "FOR UPDATE" in sql
    ]
    assert len(wx_lock_indices) == 1
    assert len(del_lock_indices) == 1
    assert wx_lock_indices[0] < del_lock_indices[0]


@pytest.mark.asyncio
async def test_data_task_dedup_key_uses_stable_failure_code_ignoring_dates_and_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consecutive data-task failures of the same type with different dates/details share one dedup_window_key."""
    from src.dalton.data_jobs import evaluators as eval_mod

    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    db = _InMemoryFailureNoticeDB(now=NOW)
    conn = db.make_conn()

    # Evaluator outputs with different dates on day 1 vs day 2
    ev1 = eval_mod.evaluate_task_run(
        eval_mod.TaskEvaluationContext(
            job_key="daily-macro",
            exit_code=1,
            stdout='{"stale_reason": "stale_data", "index_dates": {"csi300": "2026-11-26"}}',
        )
    )
    ev2 = eval_mod.evaluate_task_run(
        eval_mod.TaskEvaluationContext(
            job_key="daily-macro",
            exit_code=1,
            stdout='{"stale_reason": "stale_data", "index_dates": {"csi300": "2026-11-27"}}',
        )
    )
    assert ev1.failure_reason != ev2.failure_reason
    assert ev1.details["index_dates"] == {"csi300": "2026-11-26"}
    assert ev2.details["index_dates"] == {"csi300": "2026-11-27"}

    code1 = fr_mod.normalize_data_task_failure_code(
        ev1.failure_reason, explicit_code=ev1.failure_code, details=ev1.details
    )
    code2 = fr_mod.normalize_data_task_failure_code(
        ev2.failure_reason, explicit_code=ev2.failure_code, details=ev2.details
    )
    assert code1 == code2 == "stale_data"

    # Also verify evaluator colon-delimited date reasons normalize cleanly
    assert (
        fr_mod.normalize_data_task_failure_code(
            "t5_stale_unwind_date:2026-11-25:ref=2026-11-27:min=2026-11-26"
        )
        == "t5_stale_unwind_date"
    )
    assert (
        fr_mod.normalize_data_task_failure_code(
            "sentiment_signals_stale:2026-11-20:ref=2026-11-27:age_days=7"
        )
        == "sentiment_signals_stale"
    )
    assert (
        fr_mod.normalize_data_task_failure_code("2026-11-27 抓取失败：上游超时")
        == "failed"
    )

    r1 = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="data_task",
        subject_id=str(uuid4()),
        source_key="daily-macro",
        recipient_user_id="admin-1",
        failure_reason=ev1.failure_reason or "failed",
        detail=ev1.failure_reason,
        metadata={"job_key": "daily-macro", "index_dates": ev1.details.get("index_dates")},
        targets=["feishu:user:ou_admin"],
    )
    r2 = await fr_mod.enqueue_failure_notice_in_tx(
        conn,
        subject_kind="data_task",
        subject_id=str(uuid4()),
        source_key="daily-macro",
        recipient_user_id="admin-1",
        failure_reason=ev2.failure_reason or "failed",
        detail=ev2.failure_reason,
        metadata={"job_key": "daily-macro", "index_dates": ev2.details.get("index_dates")},
        targets=["feishu:user:ou_admin"],
    )
    assert r1 is not None and r1["aggregate_status"] == "retrying"
    assert r1["dedup_window_key"] == "stale_data:epoch:0"
    assert db.notices[r1["notice_id"]]["summary_json"]["index_dates"] == {"csi300": "2026-11-26"}
    assert "2026-11-26" in db.notices[r1["notice_id"]]["summary_json"]["detail"]
    assert r2 is not None and r2["aggregate_status"] == "suppressed"
    assert r2["suppressed"] is True


def test_execution_list_api_omits_failure_notice_when_off_and_returns_on_script_endpoints_when_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real FastAPI execution-list endpoints (company feed + data-task history) omit ``failure_notice``
    when the flag is OFF and return populated ``failure_notice`` on ``ScriptRunResponse`` when ON."""
    from typing import get_args

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.dalton.database import company_data_tasks as company_data_db
    from src.dalton.services import company_automations as company_auto_svc
    from src.dalton.services import company_task_read as ctr_mod
    from src.server.app import automations as auto_router_mod
    from src.server.dependencies.principal import Principal, ReadPrincipal
    from src.server.models.automation import (
        AutomationRunResponse,
        ScriptExecutionResponse,
        ScriptRunResponse,
    )

    script_run_id = uuid4()
    agent_exec_id = uuid4()
    agent_auto_id = uuid4()
    job_key = "daily-macro-fetch"

    script_row = {
        "engine_kind": "script",
        "automation_execution_id": script_run_id,
        "automation_id": f"data:{job_key}",
        "automation_name": "Daily Macro Fetch",
        "status": "failed",
        "scheduled_at": NOW,
        "started_at": NOW,
        "completed_at": NOW,
        "created_at": NOW,
        "exit_code": 1,
        "failure_reason": "exit_code_1",
        "notify_status": "skipped",
        "triggered_by_user_id": None,
        "log_tail": None,
        "trigger_source": "cron",
    }
    agent_row = {
        "engine_kind": "agent",
        "automation_execution_id": agent_exec_id,
        "automation_id": agent_auto_id,
        "automation_name": "Company Brief",
        "trigger_type": "cron",
        "status": "failed",
        "scheduled_at": NOW,
        "started_at": NOW,
        "completed_at": NOW,
        "created_at": NOW,
        "failure_reason": "plan_limit",
    }

    notice_summaries = {
        str(script_run_id).lower(): {
            "notice_id": str(uuid4()),
            "subject_kind": "data_task",
            "status": "delivered",
            "failure_reason": "exit_code_1",
            "total_targets": 1,
            "sent_count": 1,
            "failed_count": 0,
            "retrying_count": 0,
            "last_error_code": None,
            "updated_at": NOW,
        },
        str(agent_exec_id).lower(): {
            "notice_id": str(uuid4()),
            "subject_kind": "company_automation",
            "status": "retrying",
            "failure_reason": "plan_limit",
            "total_targets": 1,
            "sent_count": 0,
            "failed_count": 0,
            "retrying_count": 1,
            "last_error_code": None,
            "updated_at": NOW,
        },
    }

    monkeypatch.setattr(
        company_auto_svc,
        "require_company_scope_read_access",
        create_autospec(company_auto_svc.require_company_scope_read_access, return_value=None),
    )
    monkeypatch.setattr(
        ctr_mod,
        "require_company_scope_read_access",
        create_autospec(ctr_mod.require_company_scope_read_access, return_value=None),
    )
    monkeypatch.setattr(
        ctr_mod,
        "_require_complete_manifest",
        create_autospec(ctr_mod._require_complete_manifest, return_value=[job_key]),
    )
    monkeypatch.setattr(
        ctr_mod,
        "_require_known_manifest_key",
        create_autospec(ctr_mod._require_known_manifest_key, return_value=None),
    )
    monkeypatch.setattr(
        company_data_db,
        "get_script_def_row",
        create_autospec(
            company_data_db.get_script_def_row,
            return_value={"job_key": job_key, "deleted_at": None},
        ),
    )
    monkeypatch.setattr(
        company_data_db,
        "list_company_agent_execution_window",
        create_autospec(
            company_data_db.list_company_agent_execution_window,
            return_value=[agent_row],
        ),
    )
    monkeypatch.setattr(
        company_data_db,
        "list_script_execution_window",
        create_autospec(
            company_data_db.list_script_execution_window,
            return_value=[script_row],
        ),
    )
    monkeypatch.setattr(
        eds_mod,
        "load_execution_notify_rows",
        create_autospec(eds_mod.load_execution_notify_rows, return_value={}),
    )
    monkeypatch.setattr(
        fr_mod,
        "load_failure_notice_summaries",
        create_autospec(fr_mod.load_failure_notice_summaries, return_value=notice_summaries),
    )

    app = FastAPI()
    app.include_router(auto_router_mod.router)
    read_principal_dep = get_args(ReadPrincipal)[1].dependency
    app.dependency_overrides[read_principal_dep] = lambda: Principal(user_id="admin-1", auth="session")
    client = TestClient(app)

    # 1. Switch OFF -> both endpoints omit ``failure_notice`` key completely (preserving pre-change shape)
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "0")
    r_feed_off = client.get("/api/v1/automations/executions?scope=company")
    assert r_feed_off.status_code == 200
    for item in r_feed_off.json()["executions"]:
        assert "failure_notice" not in item

    r_hist_off = client.get(f"/api/v1/automations/data:{job_key}/executions")
    assert r_hist_off.status_code == 200
    for item in r_hist_off.json()["executions"]:
        assert "failure_notice" not in item

    # Direct model check for ScriptExecutionResponse / AutomationRunResponse / ScriptRunResponse when OFF
    assert "failure_notice" not in ScriptExecutionResponse(
        automation_execution_id=script_run_id,
        automation_id=f"data:{job_key}",
        status="failed",
        scheduled_at=NOW,
        created_at=NOW,
    ).model_dump()
    assert "failure_notice" not in AutomationRunResponse.model_validate(agent_row).model_dump()
    assert "failure_notice" not in ScriptRunResponse.model_validate(script_row).model_dump()

    # 2. Switch ON -> BOTH company execution feed and data-task history return ``failure_notice`` on script runs
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    r_feed_on = client.get("/api/v1/automations/executions?scope=company")
    assert r_feed_on.status_code == 200
    feed_by_id = {e["automation_execution_id"]: e for e in r_feed_on.json()["executions"]}
    assert feed_by_id[str(script_run_id)]["failure_notice"]["status"] == "delivered"
    assert feed_by_id[str(script_run_id)]["failure_notice"]["subject_kind"] == "data_task"
    assert feed_by_id[str(agent_exec_id)]["failure_notice"]["status"] == "retrying"

    r_hist_on = client.get(f"/api/v1/automations/data:{job_key}/executions")
    assert r_hist_on.status_code == 200
    hist_items = r_hist_on.json()["executions"]
    assert len(hist_items) == 1
    assert hist_items[0]["automation_execution_id"] == str(script_run_id)
    assert hist_items[0]["failure_notice"]["status"] == "delivered"


@pytest.mark.asyncio
async def test_finish_held_run_passes_final_attempt_on_last_outbox_try_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_finish_held_run`` sends ``final_attempt=True`` on the last outbox attempt when the switch is ON,
    and keeps the original call shape when the switch is OFF."""
    from src.server.database import automation as auto_db
    from src.server.services import automation_settlement as settle_mod
    from src.server.services.hook_outbox import MAX_ATTEMPTS

    exec_id = str(uuid4())
    auto_id = str(uuid4())
    job_base = {
        "hook_outbox_id": str(uuid4()),
        "conversation_thread_id": str(uuid4()),
        "run_id": str(uuid4()),
        "attempts": MAX_ATTEMPTS,
        "payload": {
            "execution_id": exec_id,
            "automation_id": auto_id,
            "user_id": "user-1",
            "status": "failed",
            "delivery": {
                "id": exec_id,
                "targets": [
                    {"entry": "feishu:@me", "address": "feishu:@me", "name": "DM", "ok": True}
                ],
            },
            "recorded": False,
            "answered": False,
        },
    }
    monkeypatch.setattr(
        auto_db,
        "get_automation",
        create_autospec(
            auto_db.get_automation,
            return_value={"automation_id": auto_id, "user_id": "user-1", "name": "Task"},
        ),
    )
    monkeypatch.setattr(
        settle_mod,
        "_keep_record",
        create_autospec(settle_mod._keep_record, return_value=True),
    )

    captured_kwargs: list[dict[str, Any]] = []

    async def _fake_finish_run(*_args: Any, **kwargs: Any) -> automation_delivery.Finish:
        captured_kwargs.append(dict(kwargs))
        return automation_delivery.Finish(
            [{"method": "feishu:@me", "address": "feishu:@me", "name": "DM", "success": False, "via": None, "error": "failed"}],
            retry=False,
            answered=True,
        )

    finish_spy = create_autospec(automation_delivery.finish_run, side_effect=_fake_finish_run)
    monkeypatch.setattr(automation_delivery, "finish_run", finish_spy)

    # Switch OFF -> final_attempt NOT passed
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "0")
    await settle_mod._finish_held_run(dict(job_base))
    assert "final_attempt" not in captured_kwargs[-1]

    # Switch ON + attempts == MAX_ATTEMPTS -> final_attempt=True passed so gateway seals finishing run
    monkeypatch.setenv("DALTON_FAILURE_NOTICE_ENABLED", "1")
    await settle_mod._finish_held_run(dict(job_base))
    assert captured_kwargs[-1].get("final_attempt") is True

    # Switch ON + attempts < MAX_ATTEMPTS -> final_attempt NOT passed yet (503 retry stays active)
    early_job = {**job_base, "attempts": 1}
    await settle_mod._finish_held_run(early_job)
    assert "final_attempt" not in captured_kwargs[-1]


