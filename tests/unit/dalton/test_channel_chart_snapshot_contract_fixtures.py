"""Shared contract tests for Channel Phase 2 Batch 2 (chart snapshots & replay SSE).

Calls real FastAPI routes (``channels_router.py`` and ``src/server/app/threads/messaging.py``)
via ``fastapi.testclient.TestClient``, serializes exact request/response contracts into
``tests/fixtures/dalton/channel_contract/snapshots_*.json``, and asserts repository fixtures
match byte-for-byte. The gateway Vitest suite
(``integrations/dalton-channel-gateway/test/channel-chart-snapshot-contract.test.ts``) loads
these exact same JSON files as ``fetch`` responses.
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src.dalton import channel_chart_snapshots as ccs
from src.dalton.app import channels_router as ch_router_mod
from src.dalton.capabilities import CapabilityId
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database import weixin_multi_account as wx_db
from src.server.app.threads import messaging as threads_messaging
from src.server.database.conversation.replay_rows import ThreadRows
from src.server.services.history.replay import ReplayPage
from src.server.services.runs import stream_writer

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "dalton" / "channel_contract"
SERVICE_TOKEN = "contract-test-service-token-32bytes-ok"
GATEWAY_EPOCH = 7

THREAD_ID = "11111111-1111-4111-8111-111111111111"
RUN_PENDING = "run-snap-pending-1"
RUN_READY = "run-snap-ready-1"
RUN_FAILED = "run-snap-failed-1"

SAMPLE_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
SAMPLE_SHA256 = hashlib.sha256(SAMPLE_PNG).hexdigest()
SAMPLE_SNAPSHOT_KEY = (
    f"channel-snapshots/{THREAD_ID}/{SAMPLE_SHA256[:16]}/chart_layer-nvda-1.png"
)


class _SnapshotContractCursor:
    """In-memory cursor supporting the SQL executed by snapshot status/download routes."""

    def __init__(self) -> None:
        self.responses: dict[str, dict[str, Any]] = {}
        self.identities: dict[tuple[str, str], dict[str, Any]] = {}
        self.deliveries: list[dict[str, Any]] = []
        self._one: dict[str, Any] | None = None
        self._all: list[dict[str, Any]] = []

    async def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> None:
        norm = " ".join(sql.split()).upper()
        self._one = None
        self._all = []

        if "FROM CONVERSATION_RESPONSES R" in norm and "JOIN CONVERSATION_THREADS T" in norm:
            rid = str(params[0])
            row = self.responses.get(rid)
            if row:
                self._one = dict(row)
            return

        if "FROM CONVERSATION_RESPONSES R" in norm and "JOIN CONVERSATION_QUERIES Q" in norm:
            # No command_report link for direct contract runs
            return

        if "FROM CONVERSATION_RESPONSES" in norm:
            rid = str(params[0])
            row = self.responses.get(rid)
            if row:
                self._one = dict(row)
            return

        if "FROM DALTON_CHANNEL_IDENTITIES" in norm:
            uid, plat = str(params[0]), str(params[1])
            row = self.identities.get((uid, plat))
            if row:
                self._one = dict(row)
            return

        if "FROM DALTON_CHANNEL_DELIVERIES" in norm:
            if "WHERE DELIVERY_KEY = %S" in norm and params:
                dkey = str(params[0])
                matched = [dict(d) for d in self.deliveries if d.get("delivery_key") == dkey]
                self._all = matched
                self._one = matched[0] if matched else None
                return
            if params:
                rid = str(params[0])
                uid = str(params[1]) if len(params) > 2 else None
                plat = str(params[2]) if len(params) > 2 else (str(params[1]) if len(params) > 1 else "")
                matched = [
                    dict(d)
                    for d in self.deliveries
                    if d.get("run_id") == rid
                    and (not uid or d.get("user_id") == uid)
                    and (not plat or d.get("platform") == plat)
                ]
                self._all = matched
                self._one = matched[0] if matched else None
            return

    async def fetchone(self) -> dict[str, Any] | None:
        return self._one

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._all)


class _SnapshotContractConn:
    def __init__(self, cur: _SnapshotContractCursor) -> None:
        self._cur = cur

    @asynccontextmanager
    async def cursor(self, **_kwargs: Any):
        yield self._cur

    @asynccontextmanager
    async def transaction(self):
        yield


def _build_contract_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ch_router_mod.channels_router, prefix="/api/v1/dalton")
    app.include_router(threads_messaging.router)
    return app


def _seed_cursor() -> _SnapshotContractCursor:
    cur = _SnapshotContractCursor()
    cur.identities[("user-alice", "feishu")] = {
        "user_id": "user-alice",
        "platform": "feishu",
        "platform_user_id": "ou_alice",
    }
    cur.deliveries.append(
        {
            "delivery_key": f"charts:{RUN_READY}",
            "kind": "fallback",
            "user_id": "user-alice",
            "thread_id": THREAD_ID,
            "run_id": RUN_READY,
            "platform": "feishu",
            "target_address": "feishu:@me",
            "status": "in_flight",
            "delivery_attempt": 1,
            "is_current": True,
            "dm_recipient_platform_user_id": "ou_alice",
        }
    )
    cur.responses[RUN_PENDING] = {
        "conversation_response_id": RUN_PENDING,
        "thread_id": THREAD_ID,
        "owner_user_id": "user-alice",
        "metadata": {
            "chart_snapshots": [
                {
                    "artifact_type": "chart_layer",
                    "status": "pending",
                    "workspace_id": "ws-contract-1",
                    "chart_id": "NVDA:1day",
                    "symbol": "NVDA",
                    "timeframe": "1day",
                    "layer_id": "layer-nvda-1",
                    "title": "NVDA 日线支撑位",
                    "command_id": "cmd-bg-1",
                    "snapshot_key": None,
                    "sha256": None,
                    "size_bytes": None,
                    "width": None,
                    "height": None,
                    "error": None,
                    "pending_since": "2026-11-25T10:00:00.000000Z",
                    "updated_at": "2026-11-25T10:00:00.000000Z",
                }
            ]
        },
    }
    cur.responses[RUN_READY] = {
        "conversation_response_id": RUN_READY,
        "thread_id": THREAD_ID,
        "owner_user_id": "user-alice",
        "metadata": {
            "chart_snapshots": [
                {
                    "artifact_type": "chart_layer",
                    "status": "ready",
                    "workspace_id": "ws-contract-1",
                    "chart_id": "NVDA:1day",
                    "symbol": "NVDA",
                    "timeframe": "1day",
                    "layer_id": "layer-nvda-1",
                    "title": "NVDA 日线支撑位",
                    "command_id": "cmd-bg-1",
                    "snapshot_key": SAMPLE_SNAPSHOT_KEY,
                    "sha256": SAMPLE_SHA256,
                    "size_bytes": len(SAMPLE_PNG),
                    "width": 1200,
                    "height": 720,
                    "error": None,
                    "pending_since": "2026-11-25T10:00:00.000000Z",
                    "updated_at": "2026-11-25T10:00:03.000000Z",
                }
            ]
        },
    }
    cur.responses[RUN_FAILED] = {
        "conversation_response_id": RUN_FAILED,
        "thread_id": THREAD_ID,
        "owner_user_id": "user-alice",
        "metadata": {
            "chart_snapshots": [
                {
                    "artifact_type": "chart_layer",
                    "status": "failed",
                    "workspace_id": "ws-contract-1",
                    "chart_id": "NVDA:1day",
                    "symbol": "NVDA",
                    "timeframe": "1day",
                    "layer_id": "layer-nvda-1",
                    "title": "NVDA 日线支撑位",
                    "command_id": "cmd-bg-1",
                    "snapshot_key": None,
                    "sha256": None,
                    "size_bytes": None,
                    "width": None,
                    "height": None,
                    "error": "stale_pending_timeout",
                    "pending_since": "2026-11-25T10:00:00.000000Z",
                    "updated_at": "2026-11-25T10:10:01.000000Z",
                }
            ]
        },
    }
    return cur


def _parse_sse_events(raw_text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in raw_text.strip().split("\n\n"):
        ev_name = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                ev_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if data_lines:
            events.append((ev_name, json.loads("\n".join(data_lines))))
    return events


def generate_batch2_contract_fixtures(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, dict[str, Any]]:
    """Invoke real FastAPI Batch 2 routes via TestClient and return all contract fixture dicts."""
    import asyncio

    cur = _seed_cursor()
    conn = _SnapshotContractConn(cur)

    @asynccontextmanager
    async def _fake_get_db_connection(existing_conn: Any = None):
        yield existing_conn if existing_conn is not None else conn

    async def _fake_verify_epoch(
        _cur: Any,
        gateway_epoch: int | None,
        *,
        required: bool = False,
        **_kwargs: Any,
    ) -> None:
        if required and gateway_epoch is None:
            raise wx_db.StaleGatewayEpochError("Missing X-Gateway-Epoch header")
        if gateway_epoch is not None and int(gateway_epoch) != GATEWAY_EPOCH:
            raise wx_db.StaleGatewayEpochError("Stale X-Gateway-Epoch header")

    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setattr(
        "src.dalton.capabilities.has_capability",
        lambda cap: cap
        in (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_FEISHU,
            CapabilityId.CHANNEL_DISCORD,
            CapabilityId.CHANNEL_WEIXIN,
        ),
    )
    monkeypatch.setattr(
        "src.dalton.capabilities.require_capability",
        lambda *_caps: None,
    )
    monkeypatch.setattr(
        ccs,
        "require_channel_platform_capability",
        create_autospec(ccs.require_channel_platform_capability, return_value=None),
    )
    monkeypatch.setattr(ccs, "get_db_connection", _fake_get_db_connection)
    monkeypatch.setattr(wx_db, "verify_gateway_epoch_in_tx", _fake_verify_epoch)
    monkeypatch.setattr(
        cg_db,
        "check_team_member_status_live",
        create_autospec(cg_db.check_team_member_status_live, return_value="active"),
    )
    monkeypatch.setattr(
        ccs,
        "get_snapshot_bytes",
        create_autospec(ccs.get_snapshot_bytes, return_value=SAMPLE_PNG),
    )

    app = _build_contract_app()
    client = TestClient(app)
    svc_headers = {
        "X-Service-Token": SERVICE_TOKEN,
        "X-Gateway-Epoch": str(GATEWAY_EPOCH),
    }

    fixtures: dict[str, dict[str, Any]] = {}

    # 1. GET /internal/snapshots/status (pending)
    path_pending = (
        f"/api/v1/dalton/channels/internal/snapshots/status"
        f"?run_id={RUN_PENDING}&thread_id={THREAD_ID}&platform=feishu"
    )
    r_pending = client.get(path_pending, headers=svc_headers)
    assert r_pending.status_code == 200
    fixtures["snapshots_status_pending.json"] = {
        "request": {"method": "GET", "path": path_pending},
        "response": {"status": r_pending.status_code, "body": r_pending.json()},
    }

    # 2. GET /internal/snapshots/status (ready)
    path_ready = (
        f"/api/v1/dalton/channels/internal/snapshots/status"
        f"?run_id={RUN_READY}&thread_id={THREAD_ID}&platform=feishu"
    )
    r_ready = client.get(path_ready, headers=svc_headers)
    assert r_ready.status_code == 200
    fixtures["snapshots_status_ready.json"] = {
        "request": {"method": "GET", "path": path_ready},
        "response": {"status": r_ready.status_code, "body": r_ready.json()},
    }

    # 3. GET /internal/snapshots/status (failed)
    path_failed = (
        f"/api/v1/dalton/channels/internal/snapshots/status"
        f"?run_id={RUN_FAILED}&thread_id={THREAD_ID}&platform=feishu"
    )
    r_failed = client.get(path_failed, headers=svc_headers)
    assert r_failed.status_code == 200
    fixtures["snapshots_status_failed.json"] = {
        "request": {"method": "GET", "path": path_failed},
        "response": {"status": r_failed.status_code, "body": r_failed.json()},
    }

    # 4. GET /internal/snapshots/{snapshot_key} (200 DM PNG with delivery_key + delivery_attempt)
    path_dl_ok = (
        f"/api/v1/dalton/channels/internal/snapshots/{SAMPLE_SNAPSHOT_KEY}"
        f"?run_id={RUN_READY}&target_address=feishu%3A%40me"
        f"&delivery_key=charts%3A{RUN_READY}&delivery_attempt=1"
    )
    r_dl_ok = client.get(path_dl_ok, headers=svc_headers)
    assert r_dl_ok.status_code == 200
    assert r_dl_ok.content == SAMPLE_PNG
    fixtures["snapshots_download_dm_ok.json"] = {
        "request": {"method": "GET", "path": path_dl_ok},
        "response": {
            "status": r_dl_ok.status_code,
            "content_type": r_dl_ok.headers.get("content-type"),
            "body_base64": base64.b64encode(r_dl_ok.content).decode("ascii"),
            "sha256": hashlib.sha256(r_dl_ok.content).hexdigest(),
        },
    }

    # 5. GET /internal/snapshots/{snapshot_key} (403 group denied)
    path_dl_grp = (
        f"/api/v1/dalton/channels/internal/snapshots/{SAMPLE_SNAPSHOT_KEY}"
        f"?run_id={RUN_READY}&target_address=feishu%3Aoc_grp_1"
    )
    r_dl_grp = client.get(path_dl_grp, headers=svc_headers)
    assert r_dl_grp.status_code == 403
    fixtures["snapshots_download_group_denied_403.json"] = {
        "request": {"method": "GET", "path": path_dl_grp},
        "response": {"status": r_dl_grp.status_code, "body": r_dl_grp.json()},
    }

    # 6. GET /internal/snapshots/{snapshot_key} (400 invalid key / traversal)
    bad_key = f"channel-snapshots/{THREAD_ID}/%252e%252e/chart_layer-nvda-1.png"
    path_dl_bad = (
        f"/api/v1/dalton/channels/internal/snapshots/{bad_key}"
        f"?run_id={RUN_READY}&target_address=feishu%3A%40me"
    )
    r_dl_bad = client.get(path_dl_bad, headers=svc_headers)
    assert r_dl_bad.status_code == 400
    fixtures["snapshots_download_invalid_key_400.json"] = {
        "request": {"method": "GET", "path": path_dl_bad},
        "response": {"status": r_dl_bad.status_code, "body": r_dl_bad.json()},
    }

    # 7. SSE run_end + /messages/replay replay_done with chart_snapshots
    recorded_frames: list[bytes] = []

    class _FakeRedisPipe:
        def expire(self, *_a: Any, **_kw: Any) -> None:
            pass

        def xadd(self, _k: str, fields: dict[bytes, bytes], **_kw: Any) -> None:
            recorded_frames.append(fields[b"event"])

        async def execute(self) -> list[Any]:
            return []

        async def __aenter__(self) -> _FakeRedisPipe:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

    class _FakeRedisClient:
        def pipeline(self, transaction: bool = False) -> _FakeRedisPipe:
            _ = transaction
            return _FakeRedisPipe()

        async def set(self, *_a: Any, **_kw: Any) -> bool:
            return True

    class _FakeCache:
        enabled = True
        client = _FakeRedisClient()

    monkeypatch.setattr(stream_writer, "get_cache_client", lambda: _FakeCache())
    ready_snaps = cur.responses[RUN_READY]["metadata"]["chart_snapshots"]
    report_layers = [
        {
            "workspace_id": "ws-contract-1",
            "chart_id": "NVDA:1day",
            "symbol": "NVDA",
            "timeframe": "1day",
            "layer_id": "layer-nvda-1",
            "title": "NVDA 日线支撑位",
        }
    ]
    asyncio.run(
        stream_writer.append_run_end_event(
            THREAD_ID,
            RUN_READY,
            "completed",
            chart_snapshots=ready_snaps,
            chart_layers=report_layers,
            outbound_blocked=False,
            redis_event_ttl=3600,
            max_stored_messages=100,
        )
    )
    run_end_events = _parse_sse_events(b"".join(recorded_frames).decode("utf-8"))
    run_end_data = dict(run_end_events[-1][1])

    # Replay route
    from src.dalton.colleague_outbound_guard import OUTBOUND_BLOCKED_METADATA_KEY
    from src.server.dependencies.principal import ReadUserId
    from src.server.services.history import replay as history_replay
    from src.server.services.history import snapshot as history_snapshot

    fake_rows = ThreadRows(
        thread_id=THREAD_ID,
        owner_id="user-alice",
        thread={
            "conversation_thread_id": THREAD_ID,
            "workspace_id": "ws-contract-1",
            "latest_run_id": RUN_READY,
        },
        queries=[],
        responses_by_turn={
            1: {
                "conversation_response_id": RUN_READY,
                "turn_index": 1,
                "status": "completed",
                "metadata": {"chart_snapshots": ready_snaps},
            },
            2: {
                "conversation_response_id": "run-snap-blocked-1",
                "turn_index": 2,
                "status": "completed",
                "metadata": {OUTBOUND_BLOCKED_METADATA_KEY: True},
            },
        },
        usages=[],
        provenance=[],
        run_facts=[],
        open_runs={},
    )

    monkeypatch.setattr(
        "src.dalton.settings.is_dalton_multiuser_auth_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        threads_messaging,
        "get_replay_thread_data",
        create_autospec(threads_messaging.get_replay_thread_data, return_value=fake_rows),
    )
    monkeypatch.setattr(
        history_replay,
        "read_replay_page",
        create_autospec(
            history_replay.read_replay_page,
            return_value=(ReplayPage(lines=[], first_turn_index=1, has_more=False), "cold"),
        ),
    )
    monkeypatch.setattr(
        history_replay,
        "finish_lines",
        create_autospec(history_replay.finish_lines, return_value=None),
    )
    monkeypatch.setattr(
        history_snapshot,
        "build_thread_snapshot",
        create_autospec(history_snapshot.build_thread_snapshot, return_value=None),
    )
    app.dependency_overrides[ReadUserId.__metadata__[0].dependency] = lambda: "user-alice"
    r_replay = client.get(
        f"/api/v1/threads/{THREAD_ID}/messages/replay",
        headers={"X-User-Id": "user-alice"},
    )
    assert r_replay.status_code == 200
    replay_events = _parse_sse_events(r_replay.text)
    replay_done_data = dict(
        next(data for ev, data in replay_events if ev == "replay_done")
    )
    fixtures["snapshots_replay_sse_events.json"] = {
        "run_end": run_end_data,
        "replay_done": replay_done_data,
    }

    return fixtures


def test_batch2_channel_contract_fixtures_match_real_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assert committed Batch 2 snapshot JSON fixtures match real FastAPI routes byte-for-byte."""
    generated = generate_batch2_contract_fixtures(monkeypatch)
    assert len(generated) == 7
    for filename, payload in generated.items():
        fixture_path = FIXTURES_DIR / filename
        assert fixture_path.is_file(), f"Missing contract fixture: {fixture_path}"
        expected_text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        actual_text = fixture_path.read_text(encoding="utf-8")
        assert actual_text == expected_text, (
            f"Contract fixture {filename} drifted from real backend route output"
        )
