"""Unit tests for Channel Phase 2 Batch 2: DM chart_layer snapshots, cross-process timing, and Core tiering.

Covers §2.3, §4.1, §6.2, and Lead Pre-Dev Decisions #1 and #2:
- Decision #1: Same-tx ``pending`` persistence on both sync ``layers._publish`` and background
  ``command_runs.finalize`` / ``settle``, two-process settle vs report delivery via Postgres,
  render-worker crash leaving ``pending`` and 10-minute stale cleanup marking ``failed``.
- Decision #2: Two-tier Core tests — (1) default ``DALTON_PROFILE=core`` keeps snapshot routes
  and capture gates closed; (2) explicit ``CHANNEL_GATEWAY`` + platform sub-capability in Core
  uses built-in ``DALTON_CHAT_UPLOAD_STORAGE_*`` (MinIO) with ``create_autospec`` S3 stand-in.
- Main-loop async OHLCV prefetch (``DailyCacheService`` / ``IntradayCacheService``) + pure
  in-memory sync closure ``backend_market_bars_tool`` on worker thread.
- Deployment-wide concurrency control via Postgres advisory lock slots (max 2 + slot timeout).
- ``_renderer.py`` hardening: ``--host-resolver-rules=MAP * ~NOTFOUND`` and ``context.route``.
- DM-only auth, strict snapshot key regex, owner active binding, outbound-blocked guard, and
  object sha256 integrity checks.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest
from chartkit import _renderer as ck_renderer
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from ptc_agent.core.sandbox.jobs import JobState
from ptc_agent.core.sandbox.jobs.protocol import Outbox
from ptc_agent.core.sandbox.runtime import SandboxRuntime
from src.dalton import channel_chart_snapshots as ccs
from src.dalton import channel_snapshot_storage as css
from src.dalton import chat_upload_storage as cus
from src.dalton.app import channels_router as ch_router_mod
from src.dalton.colleague_outbound_guard import OUTBOUND_BLOCKED_METADATA_KEY
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database import weixin_multi_account as wx_db
from src.server.database.runs import command_runs as cr_db
from src.server.models.chart_layer import ChartRef, CommandOutbox, LayerRef
from src.server.services.cache.daily_cache_service import DailyCacheService
from src.server.services.cache.intraday_cache_service import IntradayCacheService
from src.server.services.commands import settle as cmd_settle
from src.server.services.history.projector import messages_to_history_events
from src.tools.chart_annotation import layers as chart_layers_mod

THREAD_ID = "11111111-1111-4111-8111-111111111111"
OTHER_THREAD_ID = "22222222-2222-4222-8222-222222222222"
WORKSPACE_ID = "33333333-3333-4333-8333-333333333333"
RUN_ID = "run-b2-1"
REPORT_RUN_ID = "run-b2-report-2"
USER_ID = "user-alice"
SERVICE_TOKEN = "batch2-unit-test-service-token-32bytes"

VALID_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
VALID_SHA256 = hashlib.sha256(VALID_PNG).hexdigest()


class _FakeS3ClientSpec:
    """Typed spec interface mirroring boto3 S3 client methods used by snapshot storage."""

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        ContentLength: int,
        ACL: str | None = None,
    ) -> dict[str, Any]: ...

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]: ...

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]: ...

    def create_bucket(self, *, Bucket: str) -> dict[str, Any]: ...


def _make_autospec_s3_client(store: dict[tuple[str, str], bytes]) -> Any:
    """Create a signature-preserving S3 client stand-in backed by an in-memory dict."""
    client = create_autospec(_FakeS3ClientSpec, instance=True)

    def _put_object(
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        ContentLength: int,
        ACL: str | None = None,
    ) -> dict[str, Any]:
        assert ContentType == "image/png"
        assert ContentLength == len(Body)
        assert ACL == "private"
        store[(Bucket, Key)] = bytes(Body)
        return {"ETag": '"test-etag"'}

    def _get_object(*, Bucket: str, Key: str) -> dict[str, Any]:
        data = store.get((Bucket, Key))
        if data is None:
            err = RuntimeError("NoSuchKey")
            err.response = {"Error": {"Code": "NoSuchKey"}}  # type: ignore[attr-defined]
            raise err
        return {"Body": io.BytesIO(data)}

    def _delete_object(*, Bucket: str, Key: str) -> dict[str, Any]:
        store.pop((Bucket, Key), None)
        return {}

    client.put_object.side_effect = _put_object
    client.get_object.side_effect = _get_object
    client.delete_object.side_effect = _delete_object
    client.create_bucket.return_value = {}
    return client


class _SharedPostgresStore:
    """In-memory Postgres shared across simulated Worker Process A and Worker Process B."""

    def __init__(self) -> None:
        self.responses: dict[str, dict[str, Any]] = {}
        self.queries: dict[tuple[str, int], dict[str, Any]] = {}
        self.commands: dict[str, dict[str, Any]] = {}
        self.inbound_events: list[dict[str, Any]] = []
        self.thread_watches: list[dict[str, Any]] = []
        self.deliveries: list[dict[str, Any]] = []
        self.identities: dict[tuple[str, str], dict[str, Any]] = {}
        self.weixin_accounts: dict[str, dict[str, Any]] = {}
        self.chart_annotations: list[tuple[Any, ...]] = []
        self.advisory_locks: set[int] = set()

    def make_conn(self) -> _SharedPgConn:
        return _SharedPgConn(self)


class _SharedPgCursor:
    def __init__(self, db: _SharedPostgresStore) -> None:
        self._db = db
        self._one: Any = None
        self._all: list[Any] = []

    async def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> None:
        norm = " ".join(sql.split()).upper()
        self._one = None
        self._all = []

        if "SELECT PG_TRY_ADVISORY_LOCK" in norm:
            key = int(params[0])
            if key in self._db.advisory_locks:
                self._one = (False,)
            else:
                self._db.advisory_locks.add(key)
                self._one = (True,)
            return

        if "SELECT PG_ADVISORY_UNLOCK" in norm:
            key = int(params[0])
            self._db.advisory_locks.discard(key)
            self._one = (True,)
            return

        if "SELECT METADATA FROM CONVERSATION_RESPONSES WHERE CONVERSATION_RESPONSE_ID = %S" in norm:
            rid = str(params[0])
            tid = str(params[1]) if len(params) > 1 else None
            row = self._db.responses.get(rid)
            if row and (tid is None or row["thread_id"] == tid):
                self._one = {"metadata": json.loads(json.dumps(row["metadata"]))}
            return

        if "UPDATE CONVERSATION_RESPONSES SET METADATA = %S::JSONB" in norm:
            meta_json, rid = params[0], str(params[1])
            if rid in self._db.responses:
                self._db.responses[rid]["metadata"] = json.loads(meta_json)
            return

        if "WHERE METADATA ? 'CHART_SNAPSHOTS'" in norm:
            out: list[dict[str, Any]] = []
            for rid, row in self._db.responses.items():
                snaps = (row.get("metadata") or {}).get("chart_snapshots")
                if isinstance(snaps, list) and any(
                    isinstance(s, dict) and s.get("status") == "pending" for s in snaps
                ):
                    out.append(
                        {
                            "conversation_response_id": rid,
                            "metadata": json.loads(json.dumps(row["metadata"])),
                        }
                    )
            self._all = out
            return

        if "FROM CONVERSATION_RESPONSES R" in norm and "JOIN CONVERSATION_THREADS T" in norm:
            rid = str(params[0])
            row = self._db.responses.get(rid)
            if row:
                self._one = {
                    "conversation_response_id": rid,
                    "thread_id": row["thread_id"],
                    "metadata": json.loads(json.dumps(row["metadata"])),
                    "owner_user_id": row["owner_user_id"],
                }
            return

        if "FROM CONVERSATION_RESPONSES R" in norm and "JOIN CONVERSATION_QUERIES Q" in norm:
            rid, tid = str(params[0]), str(params[1])
            row = self._db.responses.get(rid)
            if row and row["thread_id"] == tid:
                q_row = self._db.queries.get((tid, int(row.get("turn_index", 1))))
                if q_row:
                    self._one = {"metadata": json.loads(json.dumps(q_row["metadata"]))}
            return

        if "FROM COMMAND_RUNS" in norm and "WHERE COMMAND_ID = %S" in norm:
            cmd_id, tid = str(params[0]), str(params[1])
            cmd = self._db.commands.get(cmd_id)
            if cmd and cmd["conversation_thread_id"] == tid:
                self._one = {"source_run_id": cmd.get("run_id")}
            return

        if "FROM CONVERSATION_RESPONSES" in norm and "WHERE CONVERSATION_RESPONSE_ID = %S" in norm:
            rid = str(params[0])
            tid = str(params[1]) if len(params) > 1 else None
            row = self._db.responses.get(rid)
            if row and (tid is None or row["thread_id"] == tid):
                self._one = {
                    "conversation_response_id": rid,
                    "thread_id": row["thread_id"],
                    "metadata": json.loads(json.dumps(row["metadata"])),
                }
            return

        if "FROM DALTON_CHANNEL_INBOUND_EVENTS" in norm:
            if norm.startswith("SELECT THREAD_SCOPE_KEY FROM DALTON_CHANNEL_INBOUND_EVENTS"):
                tid = str(params[0])
                for ev in reversed(self._db.inbound_events):
                    if ev["thread_id"] == tid:
                        self._one = {"thread_scope_key": ev.get("thread_scope_key")}
                        return
                return
            assert "CHAT_ADDRESS" in norm, f"Expected chat_address column, got SQL: {norm}"
            tid = str(params[-1])
            for ev in reversed(self._db.inbound_events):
                if ev["thread_id"] == tid:
                    row = dict(ev)
                    row.setdefault("target_address", row.get("chat_address"))
                    self._one = row
                    return
            return

        if "FROM DALTON_CHANNEL_BACKGROUND_RUNS" in norm:
            return

        if "FROM DALTON_CHANNEL_THREAD_WATCHES" in norm:
            tid = str(params[0])
            matched_w = [dict(w) for w in self._db.thread_watches if w["thread_id"] == tid]
            self._all = matched_w
            self._one = matched_w[0] if matched_w else None
            return

        if "FROM DALTON_CHANNEL_AUTOMATION_RUNS" in norm:
            return

        if "FROM DALTON_CHANNEL_DELIVERIES" in norm:
            if "WHERE DELIVERY_KEY = %S" in norm and params:
                dkey = str(params[0])
                matched = [
                    dict(d)
                    for d in reversed(self._db.deliveries)
                    if d.get("delivery_key") == dkey
                ]
                self._all = matched
                self._one = matched[0] if matched else None
                return
            if "WHERE RUN_ID = %S" in norm:
                rid = str(params[0])
                uid = str(params[1]) if len(params) > 2 else None
                plat = str(params[2]) if len(params) > 2 else (str(params[1]) if len(params) > 1 else "")
                matched = [
                    dict(d)
                    for d in reversed(self._db.deliveries)
                    if d.get("run_id") == rid
                    and (not uid or d.get("user_id") == uid)
                    and (not plat or d.get("platform") == plat)
                    and ("STATUS = 'IN_FLIGHT'" not in norm or d.get("status", "in_flight") == "in_flight")
                ]
                self._all = matched
                self._one = matched[0] if matched else None
                return
            if "WHERE THREAD_ID = %S" in norm:
                tid = str(params[0])
                self._all = [dict(d) for d in self._db.deliveries if d.get("thread_id") == tid]
            return

        if "FROM DALTON_CHANNEL_WEIXIN_ACCOUNTS" in norm:
            assert "WHERE USER_ID = %S" in norm, f"Expected user_id column, got SQL: {sql}"
            uid = str(params[0])
            acct = self._db.weixin_accounts.get(uid)
            if acct:
                self._one = dict(acct)
            return

        if "FROM DALTON_CHANNEL_WEIXIN_CONFIG" in norm or "INTO DALTON_CHANNEL_WEIXIN_CONFIG" in norm:
            self._one = {
                "singleton_id": 1,
                "enabled": True,
                "allow_external": None,
                "accounts_revision": 1,
            }
            return

        if "FROM DALTON_CHANNEL_IDENTITIES" in norm:
            uid, plat = str(params[0]), str(params[1])
            row = self._db.identities.get((uid, plat))
            if row:
                self._one = dict(row)
            return

    async def fetchone(self) -> Any:
        return self._one

    async def fetchall(self) -> list[Any]:
        return list(self._all)


class _SharedPgConn:
    def __init__(self, db: _SharedPostgresStore) -> None:
        self._db = db

    @asynccontextmanager
    async def cursor(self, **_kwargs: Any):
        yield _SharedPgCursor(self._db)

    @asynccontextmanager
    async def transaction(self):
        yield


def _enable_channels_env(monkeypatch: pytest.MonkeyPatch, *, profile: str = "everflow") -> None:
    if profile == "core":
        monkeypatch.setenv("DALTON_PROFILE", "core")
        monkeypatch.setenv("APP_ENV", "core")
    else:
        monkeypatch.delenv("DALTON_PROFILE", raising=False)
        monkeypatch.setenv("APP_ENV", "dalton")
    monkeypatch.setenv("DALTON_ENABLED", "true")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("DALTON_CAP_CHANNEL_GATEWAY", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_FEISHU", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_DISCORD", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_WEIXIN", "true")


def _sample_layer_body(layer_id: str = "layer-nvda-1") -> dict[str, Any]:
    return {
        "id": layer_id,
        "symbol": "NVDA",
        "timeframe": "1day",
        "title": "NVDA 日线关键位",
        "levels": [{"price": 120.5, "title": "Support"}],
        "markers": [{"time": "2026-11-20", "position": "aboveBar", "text": "Breakout"}],
    }


def _sample_layer_ref(layer_id: str = "layer-nvda-1") -> LayerRef:
    return ChartRef.of(WORKSPACE_ID, "NVDA", "1day").layer(layer_id)


# ---------------------------------------------------------------------------
# 1. Lead Decision #1: Two-process command settle vs report delivery & worker crash
# ---------------------------------------------------------------------------


def test_two_process_background_command_settle_pending_then_ready_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Process A finalizes command_runs + commits pending in same tx; Process B reads pending,
    then after Process A finishes background render, Process B reads ready and downloads PNG."""
    _enable_channels_env(monkeypatch)
    shared_db = _SharedPostgresStore()
    shared_db.responses[RUN_ID] = {
        "thread_id": THREAD_ID,
        "owner_user_id": USER_ID,
        "turn_index": 1,
        "metadata": {},
    }
    shared_db.responses[REPORT_RUN_ID] = {
        "thread_id": THREAD_ID,
        "owner_user_id": USER_ID,
        "turn_index": 2,
        "metadata": {},
    }
    shared_db.queries[(THREAD_ID, 2)] = {
        "metadata": {
            "report_source": {
                "kind": "command_report",
                "command_id": "cmd-bg-1",
            }
        }
    }
    shared_db.commands["cmd-bg-1"] = {
        "command_id": "cmd-bg-1",
        "conversation_thread_id": THREAD_ID,
        "run_id": RUN_ID,
        "user_id": USER_ID,
        "workspace_id": WORKSPACE_ID,
    }
    shared_db.inbound_events.append(
        {
            "thread_id": THREAD_ID,
            "chat_type": "dm",
            "platform": "feishu",
            "chat_address": "feishu:@me",
        }
    )
    shared_db.identities[(USER_ID, "feishu")] = {
        "user_id": USER_ID,
        "platform": "feishu",
        "platform_user_id": "ou_alice",
    }

    s3_store: dict[tuple[str, str], bytes] = {}
    s3_client = _make_autospec_s3_client(s3_store)
    monkeypatch.setattr(cus, "_get_client", lambda: s3_client)
    monkeypatch.setattr(cus, "_bucket_name", lambda: "chat-uploads")
    monkeypatch.setattr(cus, "is_chat_upload_storage_configured", lambda: True)
    monkeypatch.setattr(
        cg_db,
        "check_team_member_status_live",
        create_autospec(cg_db.check_team_member_status_live, return_value="active"),
    )

    @asynccontextmanager
    async def _open_shared_conn(existing: Any = None):
        yield existing if existing is not None else shared_db.make_conn()

    monkeypatch.setattr(ccs, "get_db_connection", _open_shared_conn)

    ref = _sample_layer_ref("layer-nvda-1")
    body = _sample_layer_body("layer-nvda-1")
    outbox = CommandOutbox(
        rows=[(ref, body)],
        report="Published NVDA layer",
        layers=[ref.artifact("NVDA 日线关键位", None, None)],
    )

    async def _run() -> None:
        # Process A: command_runs.finalize commits layers + pending snapshot in SAME transaction
        proc_a_conn = shared_db.make_conn()
        cmd_row = {
            "command_id": "cmd-bg-1",
            "conversation_thread_id": THREAD_ID,
            "run_id": RUN_ID,
            "user_id": USER_ID,
            "outbox_layers": list(outbox.layers or []),
        }
        await cr_db._register_pending_command_chart_snapshots(proc_a_conn, cmd_row, outbox)
        assert cmd_row.get("_pending_chart_snapshots")

        # Process B (different connection, serving report_back turn REPORT_RUN_ID) polls status -> sees pending
        status_before = await ccs.get_run_chart_snapshots_status(
            run_id=REPORT_RUN_ID,
            thread_id=THREAD_ID,
            platform="feishu",
        )
        assert status_before["has_pending"] is True
        assert len(status_before["snapshots"]) == 1
        assert status_before["snapshots"][0]["status"] == "pending"
        assert status_before["snapshots"][0]["command_id"] == "cmd-bg-1"

        # Process A completes background render and uploads PNG
        async def _fake_prefetch(**_kw: Any) -> dict[str, Any]:
            return {
                "symbol": "NVDA",
                "timezone": "America/New_York",
                "source": "cache",
                "currency": "USD",
                "data": [
                    {
                        "date": "2026-11-20",
                        "open": 120.0,
                        "high": 125.0,
                        "low": 119.0,
                        "close": 124.0,
                        "volume": 1000000.0,
                    }
                ],
            }

        async def _fake_render(*, out_path: str, **_kw: Any) -> Any:
            await asyncio.to_thread(Path(out_path).write_bytes, VALID_PNG)
            return SimpleNamespace(size=(1200, 720))

        @asynccontextmanager
        async def _fake_slot(**_kw: Any):
            yield 0

        render_res = await ccs.render_and_store_chart_snapshot(
            thread_id=THREAD_ID,
            run_id=RUN_ID,
            user_id=USER_ID,
            ref=ref,
            layer_body=body,
            prefetch_fn=_fake_prefetch,
            render_fn=_fake_render,
            slot_ctx_fn=_fake_slot,
        )
        assert render_res["status"] == "ready"
        snap_key = render_res["snapshot_key"]

        # Process B polls again -> sees ready via command_report linkage and downloads verified PNG
        status_after = await ccs.get_run_chart_snapshots_status(
            run_id=REPORT_RUN_ID,
            thread_id=THREAD_ID,
            platform="feishu",
        )
        assert status_after["has_pending"] is False
        assert status_after["snapshots"][0]["status"] == "ready"
        assert status_after["snapshots"][0]["snapshot_key"] == snap_key

        # Without a matching in_flight DM delivery with pinned recipient, `feishu:@me` MUST be denied
        with pytest.raises(HTTPException) as exc_unpinned:
            await ccs.download_verified_chart_snapshot(
                snapshot_key=snap_key,
                run_id=REPORT_RUN_ID,
                target_address="feishu:@me",
            )
        assert exc_unpinned.value.status_code == 403
        assert exc_unpinned.value.detail["code"] == "snapshot_recipient_denied"

        # Once gateway claims the in_flight DM delivery with pinned recipient, `feishu:@me` downloads PNG
        shared_db.deliveries.append(
            {
                "delivery_key": f"charts:{REPORT_RUN_ID}",
                "kind": "fallback",
                "user_id": USER_ID,
                "thread_id": THREAD_ID,
                "run_id": REPORT_RUN_ID,
                "platform": "feishu",
                "target_address": "feishu:@me",
                "status": "in_flight",
                "delivery_attempt": 1,
                "is_current": True,
                "dm_recipient_platform_user_id": "ou_alice",
            }
        )
        png_bytes, mime, filename = await ccs.download_verified_chart_snapshot(
            snapshot_key=snap_key,
            run_id=REPORT_RUN_ID,
            target_address="feishu:@me",
            delivery_key=f"charts:{REPORT_RUN_ID}",
            delivery_attempt=1,
        )
        assert png_bytes == VALID_PNG
        assert mime == "image/png"
        assert filename == "chart_layer-nvda-1.png"

    asyncio.run(_run())


def test_render_process_crash_leaves_pending_and_10min_cleanup_marks_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When render worker exits after layer commit, status stays pending and cleanup marks failed after 10 min."""
    _enable_channels_env(monkeypatch)
    shared_db = _SharedPostgresStore()
    shared_db.responses[RUN_ID] = {
        "thread_id": THREAD_ID,
        "owner_user_id": USER_ID,
        "turn_index": 1,
        "metadata": {},
    }
    shared_db.inbound_events.append(
        {
            "thread_id": THREAD_ID,
            "chat_type": "dm",
            "platform": "discord",
            "chat_address": "discord:@me",
        }
    )

    @asynccontextmanager
    async def _open_shared_conn(existing: Any = None):
        yield existing if existing is not None else shared_db.make_conn()

    monkeypatch.setattr(ccs, "get_db_connection", _open_shared_conn)

    ref = _sample_layer_ref("layer-crash-1")
    body = _sample_layer_body("layer-crash-1")

    async def _run() -> None:
        # Commit pending inside tx, then simulate worker process crashing (no render runs)
        t0 = datetime(2026, 11, 25, 10, 0, 0, tzinfo=UTC)
        monkeypatch.setattr(ccs, "_utc_now", lambda: t0)
        registered = await ccs.register_pending_chart_snapshots_in_tx(
            shared_db.make_conn(),
            thread_id=THREAD_ID,
            run_id=RUN_ID,
            user_id=USER_ID,
            bodies=[(ref, body)],
            command_id="cmd-crash-1",
        )
        assert len(registered) == 1
        assert registered[0]["status"] == "pending"

        # At +5 minutes: cleanup must NOT expire it yet
        expired_early = await ccs.expire_stale_pending_chart_snapshots(
            stale_after_seconds=600,
            now=t0 + timedelta(minutes=5),
        )
        assert expired_early == 0
        st_5m = await ccs.get_run_chart_snapshots_status(run_id=RUN_ID, thread_id=THREAD_ID)
        assert st_5m["has_pending"] is True
        assert st_5m["snapshots"][0]["status"] == "pending"

        # At +10 minutes 1 second: cleanup marks it failed
        expired_late = await ccs.expire_stale_pending_chart_snapshots(
            stale_after_seconds=600,
            now=t0 + timedelta(minutes=10, seconds=1),
        )
        assert expired_late == 1
        st_10m = await ccs.get_run_chart_snapshots_status(run_id=RUN_ID, thread_id=THREAD_ID)
        assert st_10m["has_pending"] is False
        assert st_10m["snapshots"][0]["status"] == "failed"
        assert st_10m["snapshots"][0]["error"] == "stale_pending_timeout"

    asyncio.run(_run())


def test_sync_publish_and_settle_survive_render_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot scheduling or render failures never break layers._publish or commands.settle."""
    _enable_channels_env(monkeypatch)
    shared_db = _SharedPostgresStore()
    shared_db.responses[RUN_ID] = {
        "thread_id": THREAD_ID,
        "owner_user_id": USER_ID,
        "turn_index": 1,
        "metadata": {},
    }
    shared_db.inbound_events.append(
        {"thread_id": THREAD_ID, "chat_type": "dm", "platform": "feishu", "chat_address": "feishu:@me"}
    )

    @asynccontextmanager
    async def _open_shared_conn(existing: Any = None):
        yield existing if existing is not None else shared_db.make_conn()

    monkeypatch.setattr("src.server.database.pool.get_db_connection", _open_shared_conn)
    monkeypatch.setattr(ccs, "get_db_connection", _open_shared_conn)
    monkeypatch.setattr(
        "src.server.database.chart_annotation.insert_layers",
        create_autospec(
            __import__("src.server.database.chart_annotation", fromlist=["insert_layers"]).insert_layers,
            return_value=None,
        ),
    )
    monkeypatch.setattr(
        ccs,
        "schedule_chart_snapshots_render",
        create_autospec(
            ccs.schedule_chart_snapshots_render,
            side_effect=RuntimeError("render scheduler boom"),
        ),
    )

    layer_line = json.dumps(
        {
            "version": 1,
            "id": "layer-sync-1",
            "symbol": "NVDA",
            "timeframe": "1day",
            "title": "Sync layer",
            "levels": [{"price": 120.0, "title": "S1"}],
        }
    )
    published = asyncio.run(
        chart_layers_mod._publish(
            Outbox(lines=(layer_line,)),
            WORKSPACE_ID,
            thread_id=THREAD_ID,
            run_id=RUN_ID,
            user_id=USER_ID,
        )
    )
    assert len(published.layers) == 1
    assert published.layers[0]["layer_id"] == "layer-sync-1"
    # Pending row committed in the same transaction even though background schedule threw
    assert shared_db.responses[RUN_ID]["metadata"]["chart_snapshots"][0]["status"] == "pending"

    # Also verify settle.py survives schedule_chart_snapshots_render error
    monkeypatch.setattr(
        cr_db,
        "finalize",
        create_autospec(
            cr_db.finalize,
            return_value={
                "command_id": "cmd-err-1",
                "conversation_thread_id": THREAD_ID,
                "run_id": RUN_ID,
                "user_id": USER_ID,
                "outbox_layers": [published.layers[0]],
                "_pending_chart_snapshots": [{"layer_id": "layer-sync-1", "status": "pending"}],
            },
        ),
    )
    monkeypatch.setattr(
        cmd_settle.jobs,
        "read_job_end",
        create_autospec(
            cmd_settle.jobs.read_job_end,
            return_value=(None, Outbox(lines=(layer_line,))),
        ),
    )
    monkeypatch.setattr(
        cmd_settle.jobs,
        "discard_jobs",
        create_autospec(cmd_settle.jobs.discard_jobs, return_value=None),
    )
    monkeypatch.setattr(
        cmd_settle,
        "mirror_after_end",
        create_autospec(cmd_settle.mirror_after_end, return_value=None),
    )
    monkeypatch.setattr(
        cmd_settle,
        "_settle_thread_execution_if_idle",
        create_autospec(cmd_settle._settle_thread_execution_if_idle, return_value=None),
    )
    row = {
        "command_id": "cmd-err-1",
        "conversation_thread_id": THREAD_ID,
        "workspace_id": WORKSPACE_ID,
        "computer_id": "44444444-4444-4444-8444-444444444444",
        "job_ref": {
            "command_id": "cmd-err-1",
            "state_dir": "/r/.system/jobs/cmd-err-1",
            "log_path": "/r/w/cmd-err-1.log",
            "trace_path": "/r/.system/trace/cmd-err-1.jsonl",
            "log_relpath": "w/cmd-err-1.log",
        },
    }
    runtime = create_autospec(SandboxRuntime, instance=True)
    state = JobState(found=True, exit_code=0, vanished=False, tail="")
    ended = asyncio.run(
        cmd_settle.settle(
            row,
            cr_db.Outcome("completed", exit_code=0, up=False),
            state,
            runtime,
            None,
        )
    )
    assert ended is not None
    assert ended["command_id"] == "cmd-err-1"
    assert ccs.schedule_chart_snapshots_render.call_count == 2


# ---------------------------------------------------------------------------
# 2. Lead Decision #2: Two-tier Core tests (default off vs explicit enabled + MinIO)
# ---------------------------------------------------------------------------


def test_core_profile_default_keeps_snapshot_routes_and_gates_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core Tier 1: under default DALTON_PROFILE=core, snapshot routes return 404 and gates stay off."""
    for var in (
        "DALTON_CAP_CHANNEL_GATEWAY",
        "DALTON_CAP_CHANNEL_FEISHU",
        "DALTON_CAP_CHANNEL_DISCORD",
        "DALTON_CAP_CHANNEL_WEIXIN",
        "CHANNEL_GATEWAY_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DALTON_PROFILE", "core")
    monkeypatch.setenv("APP_ENV", "core")
    monkeypatch.setenv("DALTON_ENABLED", "true")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)

    app = FastAPI()
    app.include_router(ch_router_mod.channels_router, prefix="/api/v1/dalton")
    client = TestClient(app)
    headers = {"X-Service-Token": SERVICE_TOKEN}

    r_status = client.get(
        f"/api/v1/dalton/channels/internal/snapshots/status?run_id={RUN_ID}&thread_id={THREAD_ID}",
        headers=headers,
    )
    assert r_status.status_code == 404

    snap_key = css.build_snapshot_key(THREAD_ID, VALID_SHA256, "layer-core-off")
    r_dl = client.get(
        f"/api/v1/dalton/channels/internal/snapshots/{snap_key}"
        f"?run_id={RUN_ID}&target_address=feishu%3A%40me",
        headers=headers,
    )
    assert r_dl.status_code == 404

    # Runtime registration and cleanup gates are no-ops when CHANNEL_GATEWAY is off
    shared_db = _SharedPostgresStore()
    registered = asyncio.run(
        ccs.register_pending_chart_snapshots_in_tx(
            shared_db.make_conn(),
            thread_id=THREAD_ID,
            run_id=RUN_ID,
            user_id=USER_ID,
            bodies=[(_sample_layer_ref(), _sample_layer_body())],
        )
    )
    assert registered == []
    expired = asyncio.run(ccs.expire_stale_pending_chart_snapshots(conn=shared_db.make_conn()))
    assert expired == 0


def test_core_profile_explicit_channel_enabled_delivers_dm_png_via_minio_chat_upload_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core Tier 2: DALTON_PROFILE=core with explicit CHANNEL_GATEWAY + CHANNEL_FEISHU and
    DALTON_CHAT_UPLOAD_STORAGE_* (MinIO) completes DM chart PNG upload + authenticated download."""
    _enable_channels_env(monkeypatch, profile="core")
    # Configure dedicated Core MinIO env vars (and ensure ASR fallback env vars are absent)
    for asr_var in (
        "DALTON_ASR_STORAGE_ENDPOINT_URL",
        "DALTON_ASR_STORAGE_BUCKET_NAME",
        "DALTON_ASR_STORAGE_ACCESS_KEY_ID",
        "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(asr_var, raising=False)
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME", "chat-uploads")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID", "minioadmin")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY", "minioadmin")
    cus._reset_clients_for_test()

    s3_store: dict[tuple[str, str], bytes] = {}
    s3_client = _make_autospec_s3_client(s3_store)
    monkeypatch.setattr(cus, "_build_client", lambda *_a, **_kw: s3_client)

    assert css.is_snapshot_storage_configured() is True

    shared_db = _SharedPostgresStore()
    shared_db.responses[RUN_ID] = {
        "thread_id": THREAD_ID,
        "owner_user_id": USER_ID,
        "turn_index": 1,
        "metadata": {},
    }
    shared_db.inbound_events.append(
        {"thread_id": THREAD_ID, "chat_type": "dm", "platform": "feishu", "chat_address": "feishu:@me"}
    )
    shared_db.identities[(USER_ID, "feishu")] = {
        "user_id": USER_ID,
        "platform": "feishu",
        "platform_user_id": "ou_core_alice",
    }

    @asynccontextmanager
    async def _open_shared_conn(existing: Any = None):
        yield existing if existing is not None else shared_db.make_conn()

    monkeypatch.setattr(ccs, "get_db_connection", _open_shared_conn)
    monkeypatch.setattr(
        cg_db,
        "check_team_member_status_live",
        create_autospec(cg_db.check_team_member_status_live, return_value="active"),
    )

    ref = _sample_layer_ref("layer-core-minio")
    body = _sample_layer_body("layer-core-minio")

    async def _fake_prefetch(**_kw: Any) -> dict[str, Any]:
        return {"symbol": "NVDA", "timezone": "America/New_York", "source": "yfinance", "currency": "USD", "data": []}

    async def _fake_render(*, out_path: str, **_kw: Any) -> Any:
        await asyncio.to_thread(Path(out_path).write_bytes, VALID_PNG)
        return SimpleNamespace(size=(1200, 720))

    @asynccontextmanager
    async def _fake_slot(**_kw: Any):
        yield 0

    async def _produce() -> str:
        pending = await ccs.register_pending_chart_snapshots_in_tx(
            shared_db.make_conn(),
            thread_id=THREAD_ID,
            run_id=RUN_ID,
            user_id=USER_ID,
            bodies=[(ref, body)],
        )
        assert len(pending) == 1
        res = await ccs.render_and_store_chart_snapshot(
            thread_id=THREAD_ID,
            run_id=RUN_ID,
            user_id=USER_ID,
            ref=ref,
            layer_body=body,
            prefetch_fn=_fake_prefetch,
            render_fn=_fake_render,
            slot_ctx_fn=_fake_slot,
        )
        assert res["status"] == "ready"
        return str(res["snapshot_key"])

    snap_key = asyncio.run(_produce())
    assert ("chat-uploads", snap_key) in s3_store

    # Serve via real FastAPI router under Core profile with CHANNEL_FEISHU enabled
    app = FastAPI()
    app.include_router(ch_router_mod.channels_router, prefix="/api/v1/dalton")
    client = TestClient(app)
    r = client.get(
        f"/api/v1/dalton/channels/internal/snapshots/{snap_key}"
        f"?run_id={RUN_ID}&target_address=feishu%3Auser%3Aou_core_alice",
        headers={"X-Service-Token": SERVICE_TOKEN},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == VALID_PNG

    # Turning off CHANNEL_FEISHU specifically returns 404 even while CHANNEL_GATEWAY stays on
    monkeypatch.setenv("DALTON_CAP_CHANNEL_FEISHU", "false")
    r_sub_off = client.get(
        f"/api/v1/dalton/channels/internal/snapshots/{snap_key}"
        f"?run_id={RUN_ID}&target_address=feishu%3Auser%3Aou_core_alice",
        headers={"X-Service-Token": SERVICE_TOKEN},
    )
    assert r_sub_off.status_code == 404


# ---------------------------------------------------------------------------
# 3. Main-loop OHLCV prefetch + pure in-memory sync closure + Postgres advisory lock
# ---------------------------------------------------------------------------


def test_main_loop_ohlcv_prefetch_and_sync_closure_in_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DailyCacheService / IntradayCacheService are called on main loop with owner user_id,
    and backend_market_bars_tool runs synchronously on a background thread with zero loop access."""

    @dataclass
    class _CacheResult:
        data: list[dict[str, Any]]
        header: dict[str, Any]
        error: str | None = None

    daily_svc = create_autospec(DailyCacheService, instance=True)
    daily_svc.get_stock_daily.return_value = _CacheResult(
        data=[
            {"time": 1763510400000, "open": 118.0, "high": 122.0, "low": 117.5, "close": 121.0, "volume": 500000},
            {"time": 1763596800000, "open": 121.0, "high": 126.0, "low": 120.5, "close": 125.0, "volume": 650000},
        ],
        header={"publisher": "yfinance"},
    )
    intraday_svc = create_autospec(IntradayCacheService, instance=True)
    intraday_svc.get_stock_intraday.return_value = _CacheResult(
        data=[
            {"time": 1763560200000, "open": 121.0, "high": 122.0, "low": 120.0, "close": 121.5, "volume": 12000},
        ],
        header={"publisher": "fmp"},
    )

    monkeypatch.setattr(DailyCacheService, "get_instance", classmethod(lambda cls: daily_svc))
    monkeypatch.setattr(IntradayCacheService, "get_instance", classmethod(lambda cls: intraday_svc))

    prefetched_daily = asyncio.run(
        ccs.prefetch_market_bars_for_layer(
            symbol="NVDA",
            timeframe="1day",
            user_id="user-owner-42",
            layer_body=_sample_layer_body(),
        )
    )
    daily_svc.get_stock_daily.assert_awaited_once()
    assert daily_svc.get_stock_daily.call_args.kwargs["user_id"] == "user-owner-42"

    tool = ccs.make_backend_market_bars_tool(prefetched_daily)
    thread_box: dict[str, Any] = {}

    def _worker() -> None:
        thread_box["res"] = tool(
            symbol="NVDA",
            interval="1day",
            start_date="2025-11-19",
            end_date="2025-11-20",
        )

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()
    assert len(thread_box["res"]["data"]) == 2
    assert thread_box["res"]["source"] == "yfinance"

    prefetched_intra = asyncio.run(
        ccs.prefetch_market_bars_for_layer(
            symbol="NVDA",
            timeframe="1hour",
            user_id="user-owner-42",
            layer_body=_sample_layer_body(),
        )
    )
    intraday_svc.get_stock_intraday.assert_awaited_once()
    assert intraday_svc.get_stock_intraday.call_args.kwargs["user_id"] == "user-owner-42"
    assert len(prefetched_intra["data"]) == 1


def test_postgres_advisory_lock_caps_multi_worker_concurrency() -> None:
    """Deployment-wide Postgres advisory lock caps concurrent Chromium renders at max_concurrency=2,
    while default local_concurrency=1 prevents a single process from hoarding both global slots."""
    shared_db = _SharedPostgresStore()

    @asynccontextmanager
    async def _conn_factory():
        yield shared_db.make_conn()

    async def _run() -> None:
        # 1. Single process (default local_concurrency=1) holds at most 1 Postgres advisory slot
        async with css.acquire_snapshot_render_slot(
            timeout_s=0.3,
            max_concurrency=2,
            poll_interval_s=0.02,
            conn_factory=_conn_factory,
        ) as local_slot0:
            assert local_slot0 == 0
            assert len(shared_db.advisory_locks) == 1
            # A second concurrent call in the SAME process waits on the local semaphore (cap 1)
            with pytest.raises(css.SnapshotRenderSlotTimeoutError):
                async with css.acquire_snapshot_render_slot(
                    timeout_s=0.08,
                    max_concurrency=2,
                    poll_interval_s=0.02,
                    conn_factory=_conn_factory,
                ):
                    pass
            # Meanwhile, a SECOND worker process (simulated via local_concurrency=2) acquires slot 1!
            async with css.acquire_snapshot_render_slot(
                timeout_s=0.3,
                max_concurrency=2,
                local_concurrency=2,
                poll_interval_s=0.02,
                conn_factory=_conn_factory,
            ) as other_proc_slot1:
                assert other_proc_slot1 == 1
                assert len(shared_db.advisory_locks) == 2
                # A 3rd concurrent worker times out on the deployment-wide Postgres advisory cap
                with pytest.raises(css.SnapshotRenderSlotTimeoutError):
                    async with css.acquire_snapshot_render_slot(
                        timeout_s=0.08,
                        max_concurrency=2,
                        local_concurrency=3,
                        poll_interval_s=0.02,
                        conn_factory=_conn_factory,
                    ):
                        pass
        # After release, advisory locks are cleared and a subsequent worker acquires slot 0
        assert shared_db.advisory_locks == set()
        async with css.acquire_snapshot_render_slot(
            timeout_s=0.3,
            max_concurrency=2,
            poll_interval_s=0.02,
            conn_factory=_conn_factory,
        ) as slot_next:
            assert slot_next == 0

    asyncio.run(_run())


def test_chartkit_renderer_hardening_host_resolver_and_context_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify _renderer._launch blocks DNS via --host-resolver-rules and _render routes at context level."""
    launch_calls: list[dict[str, Any]] = []

    class _FakeChromium:
        def launch(self, **kwargs: Any) -> Any:
            launch_calls.append(kwargs)
            return SimpleNamespace(is_connected=lambda: True)

    fake_pw = SimpleNamespace(chromium=_FakeChromium(), stop=lambda: None)
    monkeypatch.setattr(
        ck_renderer,
        "_playwright",
        lambda: SimpleNamespace(Error=RuntimeError, TimeoutError=TimeoutError),
    )
    ck_renderer._launch(fake_pw)
    assert len(launch_calls) == 1
    assert "--host-resolver-rules=MAP * ~NOTFOUND" in launch_calls[0]["args"]

    # Verify _render registers route on context (not page)
    import inspect

    src = inspect.getsource(ck_renderer._render)
    assert 'context.route("**/*", handle)' in src
    assert 'page.route("**/*", handle)' not in src


# ---------------------------------------------------------------------------
# 4. Snapshot key validation & DM-only download security boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_key",
    [
        f"channel-snapshots/{THREAD_ID}/../secret/chart_a.png",
        f"channel-snapshots/{THREAD_ID}/%2e%2e/chart_a.png",
        f"channel-snapshots/{THREAD_ID}/0123456789abcdef/chart_../a.png",
        "channel-snapshots/NOT-A-UUID/0123456789abcdef/chart_a.png",
        f"channel-snapshots/{THREAD_ID}/0123456789abcdef/chart_a.jpg",
        f"chat-uploads/{THREAD_ID}/0123456789abcdef/chart_a.png",
        f"channel-snapshots/{THREAD_ID}/0123456789abcdef/chart_a.png\x00",
        f" channel-snapshots/{THREAD_ID}/0123456789abcdef/chart_a.png",
    ],
)
def test_snapshot_key_strict_regex_rejects_traversal_and_bypass(bad_key: str) -> None:
    assert css.parse_snapshot_key(bad_key) is None


def test_download_verified_chart_snapshot_security_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enforces DM-only target, owner binding, thread match, outbound guard, and sha256 integrity."""
    _enable_channels_env(monkeypatch)
    shared_db = _SharedPostgresStore()
    snap_key = css.build_snapshot_key(THREAD_ID, VALID_SHA256, "layer-sec-1")
    shared_db.responses[RUN_ID] = {
        "thread_id": THREAD_ID,
        "owner_user_id": USER_ID,
        "turn_index": 1,
        "metadata": {
            "chart_snapshots": [
                {
                    "artifact_type": "chart_layer",
                    "status": "ready",
                    "workspace_id": WORKSPACE_ID,
                    "chart_id": "NVDA:1day",
                    "layer_id": "layer-sec-1",
                    "title": "Security test",
                    "snapshot_key": snap_key,
                    "sha256": VALID_SHA256,
                    "size_bytes": len(VALID_PNG),
                    "width": 1200,
                    "height": 720,
                }
            ]
        },
    }
    shared_db.identities[(USER_ID, "feishu")] = {
        "user_id": USER_ID,
        "platform": "feishu",
        "platform_user_id": "ou_alice",
    }
    shared_db.identities[(USER_ID, "discord")] = {
        "user_id": USER_ID,
        "platform": "discord",
        "platform_user_id": "100001",
    }
    shared_db.weixin_accounts[USER_ID] = {
        "account_id": "55555555-5555-4555-8555-555555555555",
        "user_id": USER_ID,
        "ilink_user_id": "wx_alice",
        "bot_generation": 1,
        "status": "active",
    }

    async def _fake_verify_epoch(
        _cur: Any,
        gateway_epoch: int | None,
        *,
        required: bool = False,
        platform: str | None = None,
        **_kw: Any,
    ) -> int | None:
        if (required or platform == "weixin") and gateway_epoch is None:
            raise wx_db.StaleGatewayEpochError("Missing X-Gateway-Epoch")
        if gateway_epoch is not None and int(gateway_epoch) != 7:
            raise wx_db.StaleGatewayEpochError("Stale X-Gateway-Epoch")
        return gateway_epoch

    @asynccontextmanager
    async def _open_shared_conn(existing: Any = None):
        yield existing if existing is not None else shared_db.make_conn()

    monkeypatch.setattr(ccs, "get_db_connection", _open_shared_conn)
    monkeypatch.setattr(wx_db, "verify_gateway_epoch_in_tx", _fake_verify_epoch)
    monkeypatch.setattr(
        cg_db,
        "check_team_member_status_live",
        create_autospec(cg_db.check_team_member_status_live, return_value="active"),
    )
    monkeypatch.setattr(
        cg_db,
        "get_user_tier",
        create_autospec(cg_db.get_user_tier, return_value="internal"),
    )

    # 1. Group chat targets refused with 403 snapshot_dm_only
    for grp_target in ("feishu:oc_group_123", "discord:111/222"):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                ccs.download_verified_chart_snapshot(
                    snapshot_key=snap_key,
                    run_id=RUN_ID,
                    target_address=grp_target,
                    get_bytes_fn=lambda _k: VALID_PNG,
                )
            )
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["code"] == "snapshot_dm_only"

    # 2. Wrong DM recipient (not thread owner's active binding) refused with 403
    for wrong_dm, ep in (
        ("feishu:user:ou_bob", None),
        ("discord:user:999999", None),
        ("weixin:dm:wx_bob", 7),
    ):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                ccs.download_verified_chart_snapshot(
                    snapshot_key=snap_key,
                    run_id=RUN_ID,
                    target_address=wrong_dm,
                    gateway_epoch=ep,
                    get_bytes_fn=lambda _k: VALID_PNG,
                )
            )
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["code"] == "snapshot_recipient_denied"

    # 2b. Weixin download without X-Gateway-Epoch is rejected with 409 stale_gateway_epoch
    with pytest.raises(wx_db.StaleGatewayEpochError):
        asyncio.run(
            ccs.download_verified_chart_snapshot(
                snapshot_key=snap_key,
                run_id=RUN_ID,
                target_address="weixin:dm:wx_alice",
                gateway_epoch=None,
                get_bytes_fn=lambda _k: VALID_PNG,
            )
        )

    # 2c. Weixin inactive account NEVER falls back to legacy dalton_channel_identities
    shared_db.identities[(USER_ID, "weixin")] = {
        "user_id": USER_ID,
        "platform": "weixin",
        "platform_user_id": "wx_alice",
    }
    shared_db.weixin_accounts[USER_ID]["status"] = "expired"
    with pytest.raises(HTTPException) as exc_wx_inactive:
        asyncio.run(
            ccs.download_verified_chart_snapshot(
                snapshot_key=snap_key,
                run_id=RUN_ID,
                target_address="weixin:dm:wx_alice",
                gateway_epoch=7,
                get_bytes_fn=lambda _k: VALID_PNG,
            )
        )
    assert exc_wx_inactive.value.status_code == 403
    assert exc_wx_inactive.value.detail["code"] == "snapshot_recipient_denied"
    shared_db.weixin_accounts[USER_ID]["status"] = "active"

    # 3. Mismatched thread_id in snapshot_key refused with 403
    wrong_thread_key = css.build_snapshot_key(OTHER_THREAD_ID, VALID_SHA256, "layer-sec-1")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            ccs.download_verified_chart_snapshot(
                snapshot_key=wrong_thread_key,
                run_id=RUN_ID,
                target_address="feishu:@me",
                get_bytes_fn=lambda _k: VALID_PNG,
            )
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "snapshot_thread_mismatch"

    # 3b. Same thread but snapshot_key not registered on this run refused with 404
    unregistered_key = css.build_snapshot_key(THREAD_ID, VALID_SHA256, "layer-other-run")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            ccs.download_verified_chart_snapshot(
                snapshot_key=unregistered_key,
                run_id=RUN_ID,
                target_address="feishu:user:ou_alice",
                get_bytes_fn=lambda _k: VALID_PNG,
            )
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["code"] == "snapshot_not_found"

    # 3c. Traversal / encoding bypass keys refused with 400
    for bad_key in (
        f"channel-snapshots/{THREAD_ID}/../secret/chart_a.png",
        f"channel-snapshots/{THREAD_ID}/%2e%2e/chart_a.png",
    ):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                ccs.download_verified_chart_snapshot(
                    snapshot_key=bad_key,
                    run_id=RUN_ID,
                    target_address="feishu:@me",
                    get_bytes_fn=lambda _k: VALID_PNG,
                )
            )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == "invalid_snapshot_key"

    # 4. Outbound-blocked colleague run refused with 403
    shared_db.responses[RUN_ID]["metadata"][OUTBOUND_BLOCKED_METADATA_KEY] = True
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            ccs.download_verified_chart_snapshot(
                snapshot_key=snap_key,
                run_id=RUN_ID,
                target_address="feishu:@me",
                get_bytes_fn=lambda _k: VALID_PNG,
            )
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "snapshot_outbound_blocked"
    shared_db.responses[RUN_ID]["metadata"][OUTBOUND_BLOCKED_METADATA_KEY] = False

    # 5. Tampered PNG bytes (sha256 mismatch) refused with 409
    tampered_png = VALID_PNG + b"\x00"
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            ccs.download_verified_chart_snapshot(
                snapshot_key=snap_key,
                run_id=RUN_ID,
                target_address="discord:user:100001",
                get_bytes_fn=lambda _k: tampered_png,
            )
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "snapshot_integrity_mismatch"

    # 6. Valid owner DM on Feishu, Discord, and Weixin all succeed
    for ok_dm, ep in (
        ("feishu:user:ou_alice", None),
        ("discord:user:100001", None),
        ("weixin:dm:wx_alice", 7),
    ):
        data, mime, fname = asyncio.run(
            ccs.download_verified_chart_snapshot(
                snapshot_key=snap_key,
                run_id=RUN_ID,
                target_address=ok_dm,
                gateway_epoch=ep,
                get_bytes_fn=lambda _k: VALID_PNG,
            )
        )
        assert data == VALID_PNG
        assert mime == "image/png"
        assert fname == "chart_layer-sec-1.png"


def test_renderer_prefers_chartkit_render_dir_and_skips_cancelled_queued_jobs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """_resolve_render_dir prefers CHARTKIT_RENDER_DIR with package fallback; cancelled queued jobs are skipped."""
    pkg_dir = tmp_path / "pkg_render"
    pkg_dir.mkdir()
    (pkg_dir / ck_renderer.ENTRY).write_text("<html>pkg</html>")
    env_dir = tmp_path / "opt_chart_render"
    env_dir.mkdir()
    (env_dir / ck_renderer.ENTRY).write_text("<html>env</html>")

    monkeypatch.setattr(ck_renderer, "RENDER_DIR", pkg_dir)
    monkeypatch.setenv("CHARTKIT_RENDER_DIR", str(env_dir))
    assert ck_renderer._resolve_render_dir() == env_dir

    # When CHARTKIT_RENDER_DIR lacks entry file or is unset, falls back to package RENDER_DIR
    monkeypatch.setenv("CHARTKIT_RENDER_DIR", str(tmp_path / "missing_dir"))
    assert ck_renderer._resolve_render_dir() == pkg_dir
    monkeypatch.delenv("CHARTKIT_RENDER_DIR", raising=False)
    assert ck_renderer._resolve_render_dir() == pkg_dir

    # Verify Renderer skips cancelled queued tasks without calling _render
    from concurrent.futures import Future

    rendered_jobs: list[str] = []
    gate = threading.Event()
    started_first = threading.Event()

    def _fake_render(_browser: Any, job: Any, *, timeout_s: float | None = None) -> Any:
        _ = timeout_s
        rendered_jobs.append(str(job))
        if job == "job-1":
            started_first.set()
            gate.wait(timeout=2.0)
        return ck_renderer.Rendered(
            report=None, view=None, framed="default", size=(1200, 720), unanswered=[], page_errors=[]
        )

    monkeypatch.setattr(ck_renderer, "_launch", lambda _pw: (None, SimpleNamespace(is_connected=lambda: True)))
    monkeypatch.setattr(ck_renderer, "_render", _fake_render)

    r = ck_renderer.Renderer()
    t = threading.Thread(target=r._loop, daemon=True)
    t.start()
    try:
        f1: Future[Any] = Future()
        c1, s1, d1 = threading.Event(), threading.Event(), threading.Event()
        f2: Future[Any] = Future()
        c2, s2, d2 = threading.Event(), threading.Event(), threading.Event()
        c2.set()  # Cancel queued job-2 before worker dequeues it
        r._jobs.put(("job-1", f1, 5.0, c1, s1, d1))
        r._jobs.put(("job-2", f2, 5.0, c2, s2, d2))
        assert started_first.wait(timeout=2.0)
        gate.set()
        assert d1.wait(timeout=2.0)
        assert d2.wait(timeout=2.0)
        assert rendered_jobs == ["job-1"]
        assert not s2.is_set()
    finally:
        r._jobs.put(None)
        t.join(timeout=2.0)


def test_snapshot_republish_registration_id_and_reaped_failed_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-published layer rejects stale worker updates; reaped failed entry cannot be overwritten to ready."""
    _enable_channels_env(monkeypatch)
    shared_db = _SharedPostgresStore()
    shared_db.responses[RUN_ID] = {
        "thread_id": THREAD_ID,
        "owner_user_id": USER_ID,
        "turn_index": 1,
        "metadata": {},
    }

    @asynccontextmanager
    async def _open_shared_conn(existing: Any = None):
        yield existing if existing is not None else shared_db.make_conn()

    monkeypatch.setattr(ccs, "get_db_connection", _open_shared_conn)

    ref = _sample_layer_ref("layer-guard-1")
    snap_key = css.build_snapshot_key(THREAD_ID, VALID_SHA256, ref.layer_id)

    async def _run() -> None:
        v1 = await ccs.register_pending_chart_snapshots_in_tx(
            shared_db.make_conn(),
            thread_id=THREAD_ID,
            run_id=RUN_ID,
            user_id=USER_ID,
            bodies=[(ref, _sample_layer_body(ref.layer_id))],
            skip_eligibility_check=True,
        )
        v2 = await ccs.register_pending_chart_snapshots_in_tx(
            shared_db.make_conn(),
            thread_id=THREAD_ID,
            run_id=RUN_ID,
            user_id=USER_ID,
            bodies=[(ref, _sample_layer_body(ref.layer_id))],
            skip_eligibility_check=True,
        )
        assert v1[0]["registration_id"] != v2[0]["registration_id"]

        # Stale worker v1 rejected
        assert (
            await ccs.update_chart_snapshot_status(
                run_id=RUN_ID,
                chart_id=ref.chart_id,
                layer_id=ref.layer_id,
                status="ready",
                snapshot_key=snap_key,
                sha256=VALID_SHA256,
                expected_registration_id=v1[0]["registration_id"],
            )
            is False
        )
        # Reap v2 -> failed
        assert (
            await ccs.expire_stale_pending_chart_snapshots(
                stale_after_seconds=600,
                now=datetime.now(UTC) + timedelta(minutes=15),
            )
            == 1
        )
        # Late v2 worker cannot revive failed -> ready
        assert (
            await ccs.update_chart_snapshot_status(
                run_id=RUN_ID,
                chart_id=ref.chart_id,
                layer_id=ref.layer_id,
                status="ready",
                snapshot_key=snap_key,
                sha256=VALID_SHA256,
                expected_registration_id=v2[0]["registration_id"],
            )
            is False
        )

    asyncio.run(_run())


def test_show_widget_persists_widget_id_and_projector_preserves_it_on_replay() -> None:
    """ShowWidget persists widget_id inside checkpointed artifact and projector reuses it on replay."""
    from langchain_core.messages import AIMessage, ToolMessage

    from ptc_agent.agent.tools.show_widget import create_show_widget_tool

    tool = create_show_widget_tool(backend=None)  # type: ignore[arg-type]
    _content, artifact = asyncio.run(
        tool.coroutine(html="<div>NVDA valuation</div>", title="NVDA Table")
    )
    persisted_id = artifact.get("widget_id")
    assert isinstance(persisted_id, str) and persisted_id.startswith("widget_")

    msgs = [
        AIMessage(
            id="ai-1",
            content="",
            tool_calls=[{"id": "call_widget_99", "name": "ShowWidget", "args": {"html": "<div>NVDA</div>"}}],
        ),
        ToolMessage(
            id="tool-1",
            tool_call_id="call_widget_99",
            name="ShowWidget",
            content="Widget rendered",
            artifact=artifact,
        ),
    ]
    events = messages_to_history_events(msgs)
    widget_ev = next(
        e for e in events if e.kind == "artifact" and e.data.get("artifact_type") == "html_widget"
    )
    assert widget_ev.data["artifact_id"] == persisted_id
    assert widget_ev.data["artifact_id"] != "call_widget_99"


def test_replay_resolves_command_report_linked_snapshots() -> None:
    """resolve_replay_chart_snapshots_by_run maps launching run's command snapshots onto command_report run_id."""
    snap_key = css.build_snapshot_key(THREAD_ID, VALID_SHA256, "layer-cmd-replay")
    launch_snap = {
        "artifact_type": "chart_layer",
        "status": "ready",
        "chart_id": "NVDA:1day",
        "layer_id": "layer-cmd-replay",
        "command_id": "bg-abcdef123456",
        "snapshot_key": snap_key,
        "sha256": VALID_SHA256,
    }
    resolved = asyncio.run(
        ccs.resolve_replay_chart_snapshots_by_run(
            thread_id=THREAD_ID,
            queries=[
                {"turn_index": 1, "metadata": {}},
                {
                    "turn_index": 2,
                    "metadata": {
                        "report_source": {
                            "kind": "command_report",
                            "command_id": "bg-abcdef123456",
                        }
                    },
                },
            ],
            responses_by_turn={
                1: {
                    "conversation_response_id": RUN_ID,
                    "metadata": {"chart_snapshots": [launch_snap]},
                },
                2: {
                    "conversation_response_id": REPORT_RUN_ID,
                    "metadata": {},
                },
            },
        )
    )
    assert REPORT_RUN_ID in resolved
    assert resolved[REPORT_RUN_ID][0]["snapshot_key"] == snap_key


def test_run_end_chart_payload_includes_report_chart_layers_and_outbound_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_run_end_chart_payload exposes query report_source/command_reports chart_layers and outbound_blocked on run_end."""
    _enable_channels_env(monkeypatch)
    shared_db = _SharedPostgresStore()
    shared_db.responses[REPORT_RUN_ID] = {
        "thread_id": THREAD_ID,
        "owner_user_id": USER_ID,
        "turn_index": 2,
        "metadata": {},
    }
    shared_db.queries[(THREAD_ID, 2)] = {
        "metadata": {
            "report_source": {
                "kind": "command_report",
                "command_id": "bg-grp-1",
                "chart_layers": [
                    {
                        "workspace_id": WORKSPACE_ID,
                        "chart_id": "NVDA:1day",
                        "symbol": "NVDA",
                        "timeframe": "1day",
                        "layer_id": "layer-grp-1",
                        "title": "NVDA 群聊后台图",
                    }
                ],
            },
            "command_reports": [
                {
                    "command_id": "bg-grp-2",
                    "chart_layers": [
                        {
                            "workspace_id": WORKSPACE_ID,
                            "chart_id": "AAPL:1day",
                            "symbol": "AAPL",
                            "timeframe": "1day",
                            "layer_id": "layer-grp-2",
                            "title": "AAPL 后台图",
                        }
                    ],
                }
            ],
        }
    }

    @asynccontextmanager
    async def _open_shared_conn(existing: Any = None):
        yield existing if existing is not None else shared_db.make_conn()

    monkeypatch.setattr(ccs, "get_db_connection", _open_shared_conn)

    normal = asyncio.run(
        ccs.get_run_end_chart_payload(run_id=REPORT_RUN_ID, thread_id=THREAD_ID)
    )
    assert normal["outbound_blocked"] is False
    assert normal["chart_snapshots"] == []
    assert [c["layer_id"] for c in normal["chart_layers"]] == ["layer-grp-1", "layer-grp-2"]
    assert normal["chart_layers"][0]["title"] == "NVDA 群聊后台图"

    # Restricted turn: outbound_blocked=True, chart_snapshots=[], titles scrubbed from chart_layers
    shared_db.responses[REPORT_RUN_ID]["metadata"][OUTBOUND_BLOCKED_METADATA_KEY] = True
    blocked = asyncio.run(
        ccs.get_run_end_chart_payload(run_id=REPORT_RUN_ID, thread_id=THREAD_ID)
    )
    assert blocked["outbound_blocked"] is True
    assert blocked["chart_snapshots"] == []
    assert len(blocked["chart_layers"]) == 2
    assert all("title" not in c for c in blocked["chart_layers"])


def test_automation_settlement_forwards_own_run_id_to_finish_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_finish_held_run and _finish_here forward the settlement job's own run_id into automation_delivery.finish_run."""
    from src.server.services import automation_delivery, automation_settlement

    captured: list[dict[str, Any]] = []

    async def _fake_gateway_request(method: str, path: str, **kwargs: Any) -> Any:
        captured.append({"method": method, "path": path, **kwargs})
        return type("Resp", (), {"status": 200, "data": {"targets": []}})()

    monkeypatch.setattr(
        automation_delivery.messaging,
        "gateway_request",
        _fake_gateway_request,
    )
    exec_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    own_run_id = "run-auto-settlement-1"
    automation = {
        "automation_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "user_id": USER_ID,
        "delivery_targets": ["feishu:@me"],
    }
    target = automation_delivery.Target(
        entry="feishu:@me", address="feishu:@me", name="Feishu DM", ok=True
    )
    delivery = automation_delivery.Delivery(id=exec_id, targets=[target])
    assert delivery.held is True

    asyncio.run(
        automation_delivery.finish_run(
            automation,
            exec_id,
            "completed",
            targets=[target],
            final_text="Done",
            thread_id=THREAD_ID,
            run_id=own_run_id,
        )
    )
    assert len(captured) == 1
    assert captured[0]["body"]["run_id"] == own_run_id
    assert captured[0]["body"]["thread_id"] == THREAD_ID

    # Also verify _finish_held_run passes job/payload run_id into finish_run
    finish_calls: list[dict[str, Any]] = []

    async def _fake_finish_run(*args: Any, **kwargs: Any) -> Any:
        finish_calls.append({"args": args, "kwargs": kwargs})
        return automation_delivery.Finish([], retry=False, answered=True)

    monkeypatch.setattr(automation_delivery, "finish_run", _fake_finish_run)
    monkeypatch.setattr(
        automation_settlement.auto_db,
        "get_automation",
        create_autospec(automation_settlement.auto_db.get_automation, return_value=automation),
    )
    monkeypatch.setattr(
        automation_settlement,
        "_keep_record",
        create_autospec(automation_settlement._keep_record, return_value=True),
    )
    asyncio.run(
        automation_settlement._finish_held_run(
            {
                "hook_outbox_id": "job-1",
                "conversation_thread_id": THREAD_ID,
                "run_id": own_run_id,
                "attempts": 1,
                "payload": {
                    "execution_id": exec_id,
                    "automation_id": automation["automation_id"],
                    "user_id": USER_ID,
                    "run_id": own_run_id,
                    "status": "completed",
                    "final_text": "Done",
                    "delivery": automation_delivery.stamp(delivery),
                },
            }
        )
    )
    assert len(finish_calls) == 1
    assert finish_calls[0]["kwargs"]["run_id"] == own_run_id


def test_at_me_download_requires_exact_delivery_key_attempt_and_weixin_account_gen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@me download validates the exact (delivery_key, delivery_attempt) row and requires matching Weixin account_id + bot_generation."""
    _enable_channels_env(monkeypatch)
    shared_db = _SharedPostgresStore()
    snap_key = css.build_snapshot_key(THREAD_ID, VALID_SHA256, "layer-me-1")
    shared_db.responses[RUN_ID] = {
        "thread_id": THREAD_ID,
        "owner_user_id": USER_ID,
        "turn_index": 1,
        "metadata": {
            "chart_snapshots": [
                {
                    "artifact_type": "chart_layer",
                    "status": "ready",
                    "workspace_id": WORKSPACE_ID,
                    "chart_id": "NVDA:1day",
                    "layer_id": "layer-me-1",
                    "title": "Pinned Test",
                    "snapshot_key": snap_key,
                    "sha256": VALID_SHA256,
                    "size_bytes": len(VALID_PNG),
                    "width": 1200,
                    "height": 720,
                }
            ]
        },
    }
    shared_db.identities[(USER_ID, "feishu")] = {
        "user_id": USER_ID,
        "platform": "feishu",
        "platform_user_id": "ou_alice",
    }
    wx_acct_id = "55555555-5555-4555-8555-555555555555"
    shared_db.weixin_accounts[USER_ID] = {
        "account_id": wx_acct_id,
        "user_id": USER_ID,
        "ilink_user_id": "wx_alice",
        "bot_generation": 3,
        "status": "active",
    }
    # Two Feishu in_flight deliveries on same run: one pinned, one unpinned
    shared_db.deliveries.extend(
        [
            {
                "delivery_key": f"fallback:{RUN_ID}",
                "kind": "fallback",
                "user_id": USER_ID,
                "thread_id": THREAD_ID,
                "run_id": RUN_ID,
                "platform": "feishu",
                "target_address": "feishu:@me",
                "status": "in_flight",
                "delivery_attempt": 1,
                "is_current": True,
                "dm_recipient_platform_user_id": "ou_alice",
            },
            {
                "delivery_key": f"charts:{RUN_ID}",
                "kind": "fallback",
                "user_id": USER_ID,
                "thread_id": THREAD_ID,
                "run_id": RUN_ID,
                "platform": "feishu",
                "target_address": "feishu:@me",
                "status": "in_flight",
                "delivery_attempt": 2,
                "is_current": True,
                "dm_recipient_platform_user_id": None,
            },
            # Weixin delivery missing account_id / bot_generation
            {
                "delivery_key": f"charts-wx-missing:{RUN_ID}",
                "kind": "fallback",
                "user_id": USER_ID,
                "thread_id": THREAD_ID,
                "run_id": RUN_ID,
                "platform": "weixin",
                "target_address": "weixin:@me",
                "status": "in_flight",
                "delivery_attempt": 1,
                "is_current": True,
                "dm_recipient_platform_user_id": "wx_alice",
                "weixin_recipient_id": "wx_alice",
                "weixin_account_id": None,
                "weixin_bot_generation": None,
            },
            # Weixin delivery with exact account_id + bot_generation
            {
                "delivery_key": f"charts-wx-ok:{RUN_ID}",
                "kind": "fallback",
                "user_id": USER_ID,
                "thread_id": THREAD_ID,
                "run_id": RUN_ID,
                "platform": "weixin",
                "target_address": "weixin:@me",
                "status": "in_flight",
                "delivery_attempt": 1,
                "is_current": True,
                "dm_recipient_platform_user_id": "wx_alice",
                "weixin_recipient_id": "wx_alice",
                "weixin_account_id": wx_acct_id,
                "weixin_bot_generation": 3,
            },
        ]
    )

    @asynccontextmanager
    async def _open_shared_conn(existing: Any = None):
        yield existing if existing is not None else shared_db.make_conn()

    async def _fake_verify_epoch(
        _cur: Any,
        gateway_epoch: int | None,
        *,
        required: bool = False,
        platform: str | None = None,
        **_kw: Any,
    ) -> int | None:
        if (required or platform == "weixin") and gateway_epoch is None:
            raise wx_db.StaleGatewayEpochError("Missing X-Gateway-Epoch")
        return gateway_epoch

    monkeypatch.setattr(ccs, "get_db_connection", _open_shared_conn)
    monkeypatch.setattr(wx_db, "verify_gateway_epoch_in_tx", _fake_verify_epoch)
    monkeypatch.setattr(
        cg_db,
        "check_team_member_status_live",
        create_autospec(cg_db.check_team_member_status_live, return_value="active"),
    )
    monkeypatch.setattr(
        cg_db,
        "get_user_tier",
        create_autospec(cg_db.get_user_tier, return_value="internal"),
    )

    # 1. Missing delivery_key / delivery_attempt -> denied even though a pinned delivery exists on same run
    with pytest.raises(HTTPException) as exc_missing:
        asyncio.run(
            ccs.download_verified_chart_snapshot(
                snapshot_key=snap_key,
                run_id=RUN_ID,
                target_address="feishu:@me",
                get_bytes_fn=lambda _k: VALID_PNG,
            )
        )
    assert exc_missing.value.status_code == 403
    assert exc_missing.value.detail["code"] == "snapshot_recipient_denied"

    # 2. Target delivery lacks pinned recipient -> denied (other pinned in_flight delivery cannot authorize it)
    with pytest.raises(HTTPException) as exc_unpinned:
        asyncio.run(
            ccs.download_verified_chart_snapshot(
                snapshot_key=snap_key,
                run_id=RUN_ID,
                target_address="feishu:@me",
                delivery_key=f"charts:{RUN_ID}",
                delivery_attempt=2,
                get_bytes_fn=lambda _k: VALID_PNG,
            )
        )
    assert exc_unpinned.value.status_code == 403
    assert exc_unpinned.value.detail["code"] == "snapshot_recipient_denied"

    # 3. Stale delivery_attempt -> denied
    with pytest.raises(HTTPException) as exc_attempt:
        asyncio.run(
            ccs.download_verified_chart_snapshot(
                snapshot_key=snap_key,
                run_id=RUN_ID,
                target_address="feishu:@me",
                delivery_key=f"fallback:{RUN_ID}",
                delivery_attempt=99,
                get_bytes_fn=lambda _k: VALID_PNG,
            )
        )
    assert exc_attempt.value.status_code == 403

    # 4. Weixin delivery missing account_id / bot_generation -> denied
    with pytest.raises(HTTPException) as exc_wx_missing:
        asyncio.run(
            ccs.download_verified_chart_snapshot(
                snapshot_key=snap_key,
                run_id=RUN_ID,
                target_address="weixin:@me",
                delivery_key=f"charts-wx-missing:{RUN_ID}",
                delivery_attempt=1,
                gateway_epoch=7,
                get_bytes_fn=lambda _k: VALID_PNG,
            )
        )
    assert exc_wx_missing.value.status_code == 403
    assert exc_wx_missing.value.detail["code"] == "snapshot_recipient_denied"

    # 5. Exact Weixin delivery with matching account_id + bot_generation -> succeeds
    data, mime, _fname = asyncio.run(
        ccs.download_verified_chart_snapshot(
            snapshot_key=snap_key,
            run_id=RUN_ID,
            target_address="weixin:@me",
            delivery_key=f"charts-wx-ok:{RUN_ID}",
            delivery_attempt=1,
            gateway_epoch=7,
            get_bytes_fn=lambda _k: VALID_PNG,
        )
    )
    assert data == VALID_PNG
    assert mime == "image/png"


def test_renderer_timeout_and_coroutine_cancel_hold_lock_until_chromium_terminates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Timeout and asyncio cancellation kill active Chromium and keep the render lock held until render ends."""
    from chartkit import _renderer

    render_dir = tmp_path / "render"
    render_dir.mkdir()
    (render_dir / "chart-render.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setenv("CHARTKIT_RENDER_DIR", str(render_dir))

    class _DummyPlaywright:
        class Error(Exception):
            pass

    monkeypatch.setattr(_renderer, "_playwright", lambda: _DummyPlaywright)

    # Part A: Active render timeout triggers browser.kill() and waits until _render exits
    killed_a = threading.Event()
    exited_a = threading.Event()

    class _KillableBrowser:
        def __init__(self, kill_ev: threading.Event) -> None:
            self._kill_ev = kill_ev

        def is_connected(self) -> bool:
            return not self._kill_ev.is_set()

        def kill(self) -> None:
            self._kill_ev.set()

        def close(self) -> None:
            self._kill_ev.set()

    def _fake_launch_a(_pw: Any) -> tuple[Any, Any]:
        return object(), _KillableBrowser(killed_a)

    def _fake_render_a(_browser: Any, _job: Any, **_kw: Any) -> Any:
        try:
            assert killed_a.wait(timeout=3.0)
            time.sleep(0.05)
            raise RuntimeError("browser terminated")
        finally:
            exited_a.set()

    monkeypatch.setattr(_renderer, "_launch", _fake_launch_a)
    monkeypatch.setattr(_renderer, "_render", _fake_render_a)
    r_a = _renderer.Renderer()
    try:
        # Short wait_timeout by shrinking effective_timeout + monkeypatching wait slice
        t_start = time.monotonic()
        cancel_ev = threading.Event()

        def _trigger_cancel() -> None:
            time.sleep(0.08)
            cancel_ev.set()

        threading.Thread(target=_trigger_cancel, daemon=True).start()
        with pytest.raises(_renderer.SnapshotUnavailable):
            r_a.run(object(), timeout_s=0.1, cancel_event=cancel_ev)  # type: ignore[arg-type]
        # Renderer.run MUST NOT return before browser.kill() ran and _render() fully exited
        assert killed_a.is_set()
        assert exited_a.is_set()
        assert time.monotonic() - t_start < 3.0
    finally:
        r_a.close()

    # Part B: Cancelling asyncio coroutine in render_and_store_chart_snapshot keeps advisory lock
    # held until active Chromium render exits!
    _enable_channels_env(monkeypatch)
    render_started = threading.Event()
    killed_b = threading.Event()
    render_exited = threading.Event()
    lock_released_after_render_exit: list[bool] = []

    def _fake_launch_b(_pw: Any) -> tuple[Any, Any]:
        return object(), _KillableBrowser(killed_b)

    def _fake_render_b(_browser: Any, _job: Any, **_kw: Any) -> Any:
        render_started.set()
        try:
            assert killed_b.wait(timeout=3.0)
            time.sleep(0.06)
            raise RuntimeError("browser killed on cancel")
        finally:
            render_exited.set()

    r_b = _renderer.Renderer()
    monkeypatch.setattr(_renderer, "_launch", _fake_launch_b)
    monkeypatch.setattr(_renderer, "_render", _fake_render_b)
    monkeypatch.setattr(_renderer, "RENDERER", r_b)
    monkeypatch.setattr(ccs, "_build_snapshot_job", lambda **_kw: object())

    @asynccontextmanager
    async def _tracking_slot_cm(**_kw: Any):
        try:
            yield 0
        finally:
            lock_released_after_render_exit.append(render_exited.is_set())

    async def _prefetch_ok(**_kw: Any) -> dict[str, Any]:
        return {"symbol": "NVDA", "data": [], "timezone": "America/New_York"}

    async def _run_cancel_scenario() -> None:
        task = asyncio.create_task(
            ccs.render_and_store_chart_snapshot(
                thread_id=THREAD_ID,
                run_id=RUN_ID,
                user_id=USER_ID,
                ref=_sample_layer_ref("layer-cancel-1"),
                layer_body=_sample_layer_body("layer-cancel-1"),
                prefetch_fn=_prefetch_ok,
                put_bytes_fn=lambda *_a, **_kw: True,
                slot_ctx_fn=_tracking_slot_cm,
            )
        )
        while not render_started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(_run_cancel_scenario())
        assert killed_b.is_set()
        assert render_exited.is_set()
        assert lock_released_after_render_exit == [True]
    finally:
        r_b.close()


def test_resolve_verified_automation_run_id_requires_explicit_link() -> None:
    """_resolve_verified_automation_run_id rejects candidate run_id when neither automation_executions nor response metadata links to execution_id."""
    exec_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    other_exec_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    class _FakeVerifyCur:
        def __init__(
            self,
            resp_row: dict[str, Any] | None,
            exec_row: dict[str, Any] | None,
        ) -> None:
            self._resp_row = resp_row
            self._exec_row = exec_row
            self._next: dict[str, Any] | None = None

        async def execute(self, sql: str, _params: Any = ()) -> None:
            norm = " ".join(sql.split()).upper()
            if "FROM CONVERSATION_RESPONSES" in norm:
                self._next = self._resp_row
            elif "FROM AUTOMATION_EXECUTIONS" in norm:
                self._next = self._exec_row
            else:
                self._next = None

        async def fetchone(self) -> dict[str, Any] | None:
            return self._next

    async def _check(
        resp_row: dict[str, Any] | None,
        exec_row: dict[str, Any] | None,
    ) -> str | None:
        return await cg_db._resolve_verified_automation_run_id(
            _FakeVerifyCur(resp_row, exec_row),
            execution_id=exec_id,
            owner_user_id=USER_ID,
            thread_id=THREAD_ID,
            candidate_run_id=RUN_ID,
        )

    base_resp = {
        "conversation_response_id": RUN_ID,
        "thread_id": THREAD_ID,
        "metadata": {},
        "owner_user_id": USER_ID,
    }

    # 1. Missing automation_executions row AND no metadata.automation_execution_id -> MUST reject
    assert asyncio.run(_check(base_resp, None)) is None

    # 2. automation_executions exists without run_id AND no metadata.automation_execution_id -> MUST reject
    assert (
        asyncio.run(
            _check(
                base_resp,
                {
                    "execution_id": exec_id,
                    "thread_id": THREAD_ID,
                    "run_id": None,
                    "owner_user_id": USER_ID,
                },
            )
        )
        is None
    )

    # 3. automation_executions points to a different run_id -> MUST reject even if metadata matches
    assert (
        asyncio.run(
            _check(
                {**base_resp, "metadata": {"automation_execution_id": exec_id}},
                {
                    "execution_id": exec_id,
                    "thread_id": THREAD_ID,
                    "run_id": REPORT_RUN_ID,
                    "owner_user_id": USER_ID,
                },
            )
        )
        is None
    )

    # 4. Response metadata belongs to a different execution -> MUST reject
    assert (
        asyncio.run(
            _check(
                {**base_resp, "metadata": {"automation_execution_id": other_exec_id}},
                {
                    "execution_id": exec_id,
                    "thread_id": THREAD_ID,
                    "run_id": RUN_ID,
                    "owner_user_id": USER_ID,
                },
            )
        )
        is None
    )

    # 5. automation_executions explicitly points to candidate run_id -> accepted
    assert (
        asyncio.run(
            _check(
                base_resp,
                {
                    "execution_id": exec_id,
                    "thread_id": THREAD_ID,
                    "run_id": RUN_ID,
                    "owner_user_id": USER_ID,
                },
            )
        )
        == RUN_ID
    )

    # 6. No automation_executions row, but response metadata explicitly records execution_id -> accepted
    assert (
        asyncio.run(
            _check(
                {**base_resp, "metadata": {"automation_execution_id": exec_id}},
                None,
            )
        )
        == RUN_ID
    )


def test_run_end_chart_payload_returns_unknown_outbound_blocked_on_missing_or_mismatched_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_run_end_chart_payload returns outbound_blocked=None when run is missing or thread mismatches, and finalization preserves unknown."""
    from src.server.database.runs import outbox as runs_outbox_db
    from src.server.services.runs import finalization as runs_finalization

    _enable_channels_env(monkeypatch)
    shared_db = _SharedPostgresStore()
    shared_db.responses[RUN_ID] = {
        "thread_id": THREAD_ID,
        "owner_user_id": USER_ID,
        "turn_index": 1,
        "metadata": {},
    }

    @asynccontextmanager
    async def _open_shared_conn(existing: Any = None):
        yield existing if existing is not None else shared_db.make_conn()

    monkeypatch.setattr(ccs, "get_db_connection", _open_shared_conn)

    # 1. Empty run_id -> outbound_blocked is None (unknown)
    empty_res = asyncio.run(ccs.get_run_end_chart_payload(run_id="", thread_id=THREAD_ID))
    assert empty_res["outbound_blocked"] is None

    # 2. Missing run row -> outbound_blocked is None (unknown)
    missing_res = asyncio.run(
        ccs.get_run_end_chart_payload(run_id="run-does-not-exist", thread_id=THREAD_ID)
    )
    assert missing_res["outbound_blocked"] is None
    assert missing_res["chart_snapshots"] == []

    # 3. Thread mismatch -> outbound_blocked is None (unknown)
    other_tid = "99999999-9999-4999-8999-999999999999"
    mismatch_res = asyncio.run(
        ccs.get_run_end_chart_payload(run_id=RUN_ID, thread_id=other_tid)
    )
    assert mismatch_res["outbound_blocked"] is None
    assert mismatch_res["chart_snapshots"] == []

    # 4. finalization.assemble_finalize_artifacts MUST NOT coerce None into False
    monkeypatch.setattr(
        runs_outbox_db,
        "list_delivered_steers",
        create_autospec(runs_outbox_db.list_delivered_steers, return_value=[]),
    )
    fin_meta: dict[str, Any] = {"user_id": USER_ID, "workspace_id": WORKSPACE_ID}
    asyncio.run(
        runs_finalization.assemble_finalize_artifacts(
            (other_tid, RUN_ID),
            metadata=fin_meta,
            status="completed",
            phase="completion",
            interrupt_reason=None,
            pause_message=None,
            error=None,
            handler=None,
            cancelled_by_user=False,
            workspace_id=WORKSPACE_ID,
            recorded_as=USER_ID,
        )
    )
    assert "persisted_outbound_blocked" not in fin_meta
    assert "persisted_chart_snapshots" not in fin_meta
