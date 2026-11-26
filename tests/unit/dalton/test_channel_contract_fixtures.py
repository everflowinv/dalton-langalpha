"""Shared contract tests between backend FastAPI routes and Dalton Channel Gateway.

Calls real FastAPI routes (`channels_router.py` and `src/server/app/threads/messaging.py`)
via `fastapi.testclient.TestClient`, serializes the exact request/response contracts into
`tests/fixtures/dalton/channel_contract/*.json`, and asserts repository fixtures match
byte-for-byte. The gateway vitest suite (`integrations/dalton-channel-gateway/test/channel-contract.test.ts`)
loads the exact same JSON fixture files as `fetch` responses.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from src.dalton import channel_deliverables as cd
from src.dalton.app import channels_router as ch_router_mod
from src.dalton.capabilities import CapabilityId
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database import weixin_multi_account as wx_db
from src.ptc_agent.core.sandbox.file_version import FileStamp
from src.server.app.threads import messaging as threads_messaging
from src.server.database.conversation.replay_rows import ThreadRows
from src.server.services.history.replay import ReplayPage
from src.server.services.workspace_file_containment import ContainedFile

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "dalton" / "channel_contract"
SERVICE_TOKEN = "contract-test-service-token-32bytes-ok"
GATEWAY_EPOCH = 7


class _ContractCursor:
    """In-memory cursor supporting the SQL executed by real `channels_router` endpoints."""

    def __init__(self) -> None:
        self.topics: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.deliveries: dict[str, dict[str, Any]] = {}
        self.identities: dict[tuple[str, str], dict[str, Any]] = {}
        self.inbound_events: dict[tuple[str, str], dict[str, Any]] = {}
        self.responses: dict[str, dict[str, Any]] = {}
        self.workspaces: dict[str, dict[str, Any]] = {}
        self._one: dict[str, Any] | None = None
        self._all: list[dict[str, Any]] = []

    async def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> None:
        norm = " ".join(sql.split()).upper()
        self._one = None
        self._all = []

        if "PG_ADVISORY_XACT_LOCK" in norm:
            return

        if "SELECT NOW() AS DB_NOW" in norm:
            from datetime import datetime, timezone

            self._one = {"db_now": datetime.now(timezone.utc)}
            return

        if "SELECT EXISTS (" in norm and "FROM DALTON_CHANNEL_DELIVERIES" in norm:
            run_id = str(params[0])
            has_unk = any(
                d.get("run_id") == run_id
                and bool(d.get("is_current"))
                and bool(d.get("has_non_empty_text"))
                and str(d.get("status")) == "unknown"
                for d in self.deliveries.values()
            )
            self._one = {"has_unknown_text": has_unk}
            return

        if "FROM DALTON_CHANNEL_BOT_TOPICS" in norm:
            where_part = norm.split("ORDER BY", 1)[0]
            require_confirmed = "STATUS = 'CONFIRMED'" in where_part
            if "EXTERNAL_TOPIC_ID = %S" in norm:
                plat, addr, ext_id = params
                for row in self.topics.values():
                    if (
                        row["platform"] == plat
                        and row["chat_address"] == addr
                        and row.get("external_topic_id") == ext_id
                        and (not require_confirmed or row.get("status") == "confirmed")
                    ):
                        self._one = dict(row)
                        return
                return
            if "CANONICAL_SCOPE_KEY = %S" in norm:
                plat, addr, scope = params
                row = self.topics.get((plat, addr, scope))
                if row and (not require_confirmed or row.get("status") == "confirmed"):
                    self._one = dict(row)
                return
            return

        if "FROM DALTON_CHANNEL_DELIVERIES" in norm and "WHERE DELIVERY_KEY = %S" in norm:
            key = str(params[0])
            row = self.deliveries.get(key)
            if row:
                self._one = dict(row)
            return

        if "FROM DALTON_CHANNEL_DELIVERIES" in norm and "WHERE RUN_ID = %S" in norm:
            run_id, exclude_key = str(params[0]), str(params[1])
            self._all = [
                {
                    "delivery_key": d["delivery_key"],
                    "status": d["status"],
                    "files_status": d.get("files_status") or [],
                }
                for d in self.deliveries.values()
                if d.get("run_id") == run_id
                and bool(d.get("is_current"))
                and d["delivery_key"] != exclude_key
            ]
            return

        if "INSERT INTO DALTON_CHANNEL_DELIVERIES" in norm:
            (
                key,
                kind,
                uid,
                tid,
                rid,
                exec_id,
                is_cur,
                has_text,
                plat,
                addr,
                total_chunks,
                src_scope,
                src_gen,
                act_origin,
                wx_aid,
                wx_bid,
                wx_gen,
                wx_recip,
                pinned_dm,
                _replay_rec,
            ) = params
            prev = self.deliveries.get(key)
            attempt = (int(prev.get("delivery_attempt") or 1) + 1) if prev else 1
            self.deliveries[key] = {
                "delivery_key": key,
                "kind": kind,
                "user_id": uid,
                "thread_id": tid,
                "run_id": rid,
                "execution_id": exec_id,
                "is_current": is_cur,
                "has_non_empty_text": has_text,
                "platform": plat,
                "target_address": addr,
                "status": "in_flight",
                "total_chunks": total_chunks,
                "sent_chunks": 0,
                "platform_message_ids": [],
                "files_status": [],
                "source_scope_key": src_scope,
                "source_generation": src_gen,
                "activity_origin": act_origin,
                "delivery_attempt": attempt,
                "weixin_account_id": wx_aid,
                "weixin_bot_id": wx_bid,
                "weixin_bot_generation": wx_gen,
                "weixin_recipient_id": wx_recip,
                "dm_recipient_platform_user_id": pinned_dm,
                "error_code": None,
                "error_message": None,
                "confirmed_thread_address": None,
                "discord_body_phase": None,
                "first_response_json": None,
            }
            return

        if "FROM DALTON_CHANNEL_IDENTITIES" in norm:
            uid, plat = str(params[0]), str(params[1])
            row = self.identities.get((uid, plat))
            if row:
                self._one = dict(row)
            return

        if "FROM DALTON_CHANNEL_INBOUND_EVENTS" in norm:
            for ev in self.inbound_events.values():
                if params and (
                    ev.get("run_id") == params[0]
                    or ev.get("thread_id") == params[0]
                    or (len(params) > 1 and ev.get("message_id") == params[1])
                ):
                    self._one = dict(ev)
                    return
            return

        if "FROM CONVERSATION_RESPONSES" in norm:
            rid = str(params[0])
            row = self.responses.get(rid)
            if row:
                self._one = dict(row)
            return

        if "FROM WORKSPACES" in norm:
            ws_id = str(params[0])
            row = self.workspaces.get(ws_id)
            if row:
                self._one = dict(row)
            return

    async def fetchone(self) -> dict[str, Any] | None:
        return self._one

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._all)


class _ContractConn:
    def __init__(self, cur: _ContractCursor) -> None:
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


def _seed_contract_cursor() -> _ContractCursor:
    cur = _ContractCursor()
    cur.topics[("feishu", "feishu:oc_grp_1", "chat:oc_grp_1:topic:om_root_1")] = {
        "platform": "feishu",
        "chat_address": "feishu:oc_grp_1",
        "canonical_scope_key": "chat:oc_grp_1:topic:om_root_1",
        "external_topic_id": "omt_topic_1",
        "root_message_id": "om_root_1",
        "first_reply_message_id": "om_first_reply_1",
        "initiator_platform_user_id": "ou_alice",
        "initiator_dalton_user_id": "user-alice",
        "status": "confirmed",
    }
    cur.topics[("discord", "discord:111111/222222", "chat:111111/222222:topic:999000111")] = {
        "platform": "discord",
        "chat_address": "discord:111111/222222",
        "canonical_scope_key": "chat:111111/222222:topic:999000111",
        "external_topic_id": "999000111",
        "root_message_id": "999000111",
        "first_reply_message_id": "999000222",
        "initiator_platform_user_id": "100001",
        "initiator_dalton_user_id": "user-alice",
        "status": "confirmed",
    }
    cur.deliveries["deliverables:run-contract-1"] = {
        "delivery_key": "deliverables:run-contract-1",
        "kind": "fallback",
        "user_id": "user-alice",
        "thread_id": "11111111-1111-4111-8111-111111111111",
        "run_id": "run-contract-1",
        "is_current": True,
        "has_non_empty_text": False,
        "platform": "feishu",
        "target_address": "feishu:@me",
        "status": "in_flight",
        "sent_chunks": 0,
        "delivery_attempt": 1,
        "dm_recipient_platform_user_id": "ou_alice",
        "files_status": [],
    }
    cur.deliveries["deliverables:run-wx-denied"] = {
        "delivery_key": "deliverables:run-wx-denied",
        "kind": "fallback",
        "user_id": "user-alice",
        "thread_id": "11111111-1111-4111-8111-111111111111",
        "run_id": "run-wx-denied",
        "is_current": True,
        "has_non_empty_text": False,
        "platform": "weixin",
        "target_address": "weixin:@me",
        "status": "in_flight",
        "sent_chunks": 0,
        "delivery_attempt": 1,
        "dm_recipient_platform_user_id": "wx_user_alice",
        "weixin_account_id": "22222222-2222-4222-8222-222222222222",
        "weixin_bot_generation": 1,
        "weixin_recipient_id": "wx_user_alice",
        "files_status": [],
    }
    cur.deliveries["deliverables:run-crash-1"] = {
        "delivery_key": "deliverables:run-crash-1",
        "kind": "fallback",
        "user_id": "user-alice",
        "thread_id": "11111111-1111-4111-8111-111111111111",
        "run_id": "run-crash-1",
        "is_current": True,
        "has_non_empty_text": False,
        "platform": "feishu",
        "target_address": "feishu:@me",
        "status": "unknown",
        "error_code": "gateway_restarted",
        "error_message": "Gateway restarted before settlement",
        "sent_chunks": 0,
        "delivery_attempt": 1,
        "source_scope_key": "dm:ou_alice",
        "source_generation": 1,
        "activity_origin": "conversation",
        "dm_recipient_platform_user_id": "ou_alice",
        "confirmed_thread_address": None,
        "discord_body_phase": None,
        "first_response_json": None,
        "files_status": [
            {
                "workspace_id": "ws-contract-1",
                "path": "results/report.pdf",
                "status": "pending",
                "reason": None,
            }
        ],
    }
    cur.identities[("user-alice", "feishu")] = {
        "user_id": "user-alice",
        "platform": "feishu",
        "platform_user_id": "ou_alice",
        "weixin_context_token": None,
    }
    cur.inbound_events[("feishu", "om_crash_in_1")] = {
        "platform": "feishu",
        "message_id": "om_crash_in_1",
        "dalton_user_id": "user-alice",
        "chat_type": "dm",
        "chat_address": "feishu:@me",
        "target_address": "feishu:@me",
        "thread_scope_key": "dm:ou_alice",
        "thread_id": "11111111-1111-4111-8111-111111111111",
        "run_id": "run-crash-1",
        "workspace_id": "ws-contract-1",
        "session_generation": 1,
        "raw_payload": {"platform_user_id": "ou_alice", "generation": 1},
        "_source_table": "inbound_events",
    }
    cur.workspaces["ws-contract-1"] = {
        "workspace_id": "ws-contract-1",
        "user_id": "user-alice",
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


def generate_all_contract_fixtures(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, dict[str, Any]]:
    """Hit real FastAPI routes via TestClient and return all shared contract fixture dicts."""
    import asyncio

    cur = _seed_contract_cursor()
    conn = _ContractConn(cur)

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

    async def _fake_find_source(
        _cur: Any,
        *,
        user_id: str | None = None,
        run_id: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any] | None:
        _ = (user_id, thread_id)
        for ev in cur.inbound_events.values():
            if run_id and ev.get("run_id") == run_id:
                return dict(ev)
        return None

    async def _fake_wx_preflight_denied(
        *,
        delivery_key: str,
        gateway_epoch: int | str | None,
        delivery_attempt: int | None = None,
        chunk_index: int = 0,
        conn: Any = None,
    ) -> dict[str, Any]:
        _ = (delivery_key, gateway_epoch, delivery_attempt, chunk_index, conn)
        return {
            "authorized": False,
            "decision": "deny",
            "retryable": False,
            "deny_code": "weixin_account_unbound",
        }

    # Patch environment & capability/DB dependencies with signature-preserving specs
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
        cg_db,
        "require_channel_platform_capability",
        create_autospec(cg_db.require_channel_platform_capability, return_value=None),
    )
    monkeypatch.setattr(
        cd,
        "require_channel_platform_capability",
        create_autospec(cd.require_channel_platform_capability, return_value=None),
    )
    monkeypatch.setattr(
        cg_db,
        "get_db_connection",
        create_autospec(cg_db.get_db_connection, side_effect=_fake_get_db_connection),
    )
    monkeypatch.setattr(
        cd,
        "get_db_connection",
        create_autospec(cd.get_db_connection, side_effect=_fake_get_db_connection),
    )
    monkeypatch.setattr(
        wx_db,
        "verify_gateway_epoch_in_tx",
        create_autospec(wx_db.verify_gateway_epoch_in_tx, side_effect=_fake_verify_epoch),
    )
    monkeypatch.setattr(
        cg_db,
        "check_team_member_status_live",
        create_autospec(cg_db.check_team_member_status_live, return_value="active"),
    )
    monkeypatch.setattr(
        cg_db,
        "_find_current_conversation_source",
        create_autospec(cg_db._find_current_conversation_source, side_effect=_fake_find_source),
    )
    monkeypatch.setattr(
        cg_db,
        "_verify_workspace_write_permission",
        create_autospec(
            cg_db._verify_workspace_write_permission,
            return_value=(True, None, {"workspace_id": "ws-contract-1", "user_id": "user-alice"}),
        ),
    )
    monkeypatch.setattr(
        cg_db,
        "get_home_workspace_id",
        create_autospec(cg_db.get_home_workspace_id, return_value="ws-home-alice"),
    )
    monkeypatch.setattr(
        wx_db,
        "preflight_weixin_send",
        create_autospec(wx_db.preflight_weixin_send, side_effect=_fake_wx_preflight_denied),
    )

    # Record a real deliverable via TurnDeliverableRecorder so the fixture uses the exact backend schema
    pdf_bytes = b"%PDF-1.4\ncontract deliverable report\n"
    pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

    async def _fake_versioned_read(
        _sb: Any,
        path: str,
        *,
        work_dir: str,
        **_kwargs: Any,
    ) -> ContainedFile:
        _ = work_dir
        return ContainedFile(
            canonical=path,
            size=len(pdf_bytes),
            content=pdf_bytes,
            stamp=FileStamp(
                size=len(pdf_bytes),
                mtime="1700000000.0",
                ctime="1700000000.0",
                inode="101",
            ),
            settled=True,
        )

    recorder = cd.TurnDeliverableRecorder(
        thread_id="11111111-1111-4111-8111-111111111111",
        run_id="run-contract-1",
        workspace_id="ws-contract-1",
        sandbox=object(),
        work_dir="/home/workspace/Research",
        versioned_reader=_fake_versioned_read,
    )
    asyncio.run(
        recorder.on_file_operation(
            {
                "operation": "write_file",
                "file_path": "/home/workspace/Research/results/report.pdf",
                "written_sha256": pdf_sha,
                "written_size": len(pdf_bytes),
            }
        )
    )
    recorded_deliverables = recorder.snapshot()

    # Patch replay route dependencies to capture real `replay_done` SSE JSON from `messaging.py`
    replay_rows = ThreadRows(
        thread_id="11111111-1111-4111-8111-111111111111",
        owner_id="user-alice",
        thread={
            "conversation_thread_id": "11111111-1111-4111-8111-111111111111",
            "workspace_id": "ws-contract-1",
            "latest_run_id": "run-contract-1",
        },
        queries=[],
        responses_by_turn={
            1: {
                "conversation_response_id": "run-contract-1",
                "turn_index": 1,
                "status": "completed",
                "metadata": {"turn_deliverables": recorded_deliverables},
            }
        },
        usages=[],
        provenance=[],
        run_facts=[],
        open_runs={},
    )
    from src.server.services.history import replay as history_replay
    from src.server.services.history import snapshot as history_snapshot

    monkeypatch.setattr(
        "src.dalton.settings.is_dalton_multiuser_auth_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        threads_messaging,
        "get_replay_thread_data",
        create_autospec(threads_messaging.get_replay_thread_data, return_value=replay_rows),
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

    app = _build_contract_app()
    from src.server.dependencies.principal import ReadUserId

    app.dependency_overrides[ReadUserId.__metadata__[0].dependency] = (
        lambda: "user-alice"
    )
    client = TestClient(app)
    svc_headers = {"X-Service-Token": SERVICE_TOKEN}
    epoch_headers = {**svc_headers, "X-Gateway-Epoch": str(GATEWAY_EPOCH)}

    fixtures: dict[str, dict[str, Any]] = {}

    # 1. Topics lookup: Feishu hit
    feishu_lookup_req = {
        "platform": "feishu",
        "chat_address": "feishu:oc_grp_1",
        "match_mode": "inbound_followup",
        "external_topic_id": "omt_topic_1",
    }
    r1 = client.post(
        "/api/v1/dalton/channels/internal/topics/lookup",
        json=feishu_lookup_req,
        headers=svc_headers,
    )
    assert r1.status_code == 200
    fixtures["topics_lookup_feishu_hit.json"] = {
        "request": {
            "method": "POST",
            "path": "/api/v1/dalton/channels/internal/topics/lookup",
            "body": feishu_lookup_req,
        },
        "response": {"status": r1.status_code, "body": r1.json()},
    }

    # 2. Topics lookup: Discord hit
    discord_lookup_req = {
        "platform": "discord",
        "chat_address": "discord:111111/222222",
        "match_mode": "inbound_followup",
        "external_topic_id": "999000111",
    }
    r2 = client.post(
        "/api/v1/dalton/channels/internal/topics/lookup",
        json=discord_lookup_req,
        headers=svc_headers,
    )
    assert r2.status_code == 200
    fixtures["topics_lookup_discord_hit.json"] = {
        "request": {
            "method": "POST",
            "path": "/api/v1/dalton/channels/internal/topics/lookup",
            "body": discord_lookup_req,
        },
        "response": {"status": r2.status_code, "body": r2.json()},
    }

    # 3. Topics lookup: Miss
    miss_lookup_req = {
        "platform": "feishu",
        "chat_address": "feishu:oc_grp_1",
        "match_mode": "inbound_followup",
        "external_topic_id": "omt_unknown",
    }
    r3 = client.post(
        "/api/v1/dalton/channels/internal/topics/lookup",
        json=miss_lookup_req,
        headers=svc_headers,
    )
    assert r3.status_code == 200
    fixtures["topics_lookup_miss.json"] = {
        "request": {
            "method": "POST",
            "path": "/api/v1/dalton/channels/internal/topics/lookup",
            "body": miss_lookup_req,
        },
        "response": {"status": r3.status_code, "body": r3.json()},
    }

    # 4. Verify DM recipient: Authorized (200)
    verify_path = (
        "/api/v1/dalton/channels/internal/deliveries/"
        "deliverables%3Arun-contract-1/verify-dm-recipient"
    )
    r4 = client.post(
        verify_path,
        json={"delivery_attempt": 1},
        headers=epoch_headers,
    )
    assert r4.status_code == 200
    fixtures["verify_dm_recipient_authorized.json"] = {
        "request": {
            "method": "POST",
            "path": verify_path,
            "headers": {"X-Gateway-Epoch": str(GATEWAY_EPOCH)},
            "body": {"delivery_attempt": 1},
        },
        "response": {"status": r4.status_code, "body": r4.json()},
    }

    # 5. Verify DM recipient: Missing X-Gateway-Epoch -> 409 stale_gateway_epoch
    r5 = client.post(
        verify_path,
        json={"delivery_attempt": 1},
        headers=svc_headers,
    )
    assert r5.status_code == 409
    fixtures["verify_dm_recipient_stale_epoch_409.json"] = {
        "request": {
            "method": "POST",
            "path": verify_path,
            "headers": {},
            "body": {"delivery_attempt": 1},
        },
        "response": {"status": r5.status_code, "body": r5.json()},
    }

    # 6. Verify DM recipient: Unbound -> 403 deliverable_recipient_denied
    saved_identity = cur.identities.pop(("user-alice", "feishu"))
    r6 = client.post(
        verify_path,
        json={"delivery_attempt": 1},
        headers=epoch_headers,
    )
    assert r6.status_code == 403
    fixtures["verify_dm_recipient_unbound_403.json"] = {
        "request": {
            "method": "POST",
            "path": verify_path,
            "headers": {"X-Gateway-Epoch": str(GATEWAY_EPOCH)},
            "body": {"delivery_attempt": 1},
        },
        "response": {"status": r6.status_code, "body": r6.json()},
    }
    cur.identities[("user-alice", "feishu")] = saved_identity

    # 7. Deliverables download: Weixin preflight denied -> 403 before any read
    wx_dl_path = (
        "/api/v1/dalton/channels/internal/deliverables/download"
        "?delivery_key=deliverables:run-wx-denied&delivery_attempt=1"
        "&workspace_id=ws-contract-1&path=results/report.pdf"
    )
    r7 = client.get(wx_dl_path, headers=epoch_headers)
    assert r7.status_code == 403
    fixtures["deliverables_download_weixin_denied_403.json"] = {
        "request": {
            "method": "GET",
            "path": wx_dl_path,
            "headers": {"X-Gateway-Epoch": str(GATEWAY_EPOCH)},
        },
        "response": {"status": r7.status_code, "body": r7.json()},
    }

    # 8. Real SSE `run_end` + real `/api/v1/threads/{id}/messages/replay` `replay_done`
    r8 = client.get(
        "/api/v1/threads/11111111-1111-4111-8111-111111111111/messages/replay",
        headers={"X-User-Id": "user-alice"},
    )
    assert r8.status_code == 200
    sse_events = _parse_sse_events(r8.text)
    replay_done_payloads = [data for ev, data in sse_events if ev == "replay_done"]
    assert len(replay_done_payloads) == 1
    replay_done_payload = replay_done_payloads[0]
    assert "turn_deliverables_by_run" in replay_done_payload

    run_end_payload = {
        "thread_id": "11111111-1111-4111-8111-111111111111",
        "run_id": "run-contract-1",
        "outcome": "completed",
        "turn_deliverables": recorded_deliverables,
    }
    fixtures["deliverables_sse_events.json"] = {
        "run_end": run_end_payload,
        "replay_done": replay_done_payload,
    }

    # 9. I-6 claim when `deliverables:run-crash-1` is `unknown` + recovery claim for `deliverables-links:run-crash-1`
    claim_unknown_req = {
        "delivery_key": "deliverables:run-crash-1",
        "kind": "fallback",
        "user_id": "user-alice",
        "thread_id": "11111111-1111-4111-8111-111111111111",
        "run_id": "run-crash-1",
        "platform": "feishu",
        "target": "feishu:@me",
        "has_non_empty_text": False,
        "total_chunks": 0,
        "source_scope_key": "dm:ou_alice",
        "source_generation": 1,
        "activity_origin": "replay",
    }
    r9a = client.post(
        "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
        json=claim_unknown_req,
        headers=epoch_headers,
    )
    assert r9a.status_code == 200

    claim_links_req = {
        **claim_unknown_req,
        "delivery_key": "deliverables-links:run-crash-1",
    }
    r9b = client.post(
        "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
        json=claim_links_req,
        headers=epoch_headers,
    )
    assert r9b.status_code == 200
    fixtures["deliveries_claim_unknown_deliverables.json"] = {
        "unknown_claim": {
            "request": {
                "method": "POST",
                "path": "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
                "body": claim_unknown_req,
            },
            "response": {"status": r9a.status_code, "body": r9a.json()},
        },
        "links_recovery_claim": {
            "request": {
                "method": "POST",
                "path": "/api/v1/dalton/channels/internal/deliveries/authorize-and-claim",
                "body": claim_links_req,
            },
            "response": {"status": r9b.status_code, "body": r9b.json()},
        },
    }

    return fixtures


def test_channel_contract_fixtures_match_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regenerate every contract fixture from real FastAPI routes and assert exact match with repo."""
    generated = generate_all_contract_fixtures(monkeypatch)
    update = os.getenv("UPDATE_CHANNEL_CONTRACT_FIXTURES") == "1"
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for filename, payload in sorted(generated.items()):
        serialized = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        path = FIXTURES_DIR / filename
        if update:
            path.write_text(serialized, encoding="utf-8")
        assert path.exists(), f"Missing contract fixture: {path}"
        assert path.read_text(encoding="utf-8") == serialized, (
            f"Contract fixture drift detected in {filename}. "
            "Regenerate with UPDATE_CHANNEL_CONTRACT_FIXTURES=1 and update gateway tests."
        )


def test_topics_lookup_rejects_legacy_purpose_field_without_match_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract regression guard: sending `purpose` without `match_mode` MUST fail validation with 422."""
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setattr(
        "src.dalton.capabilities.has_capability",
        lambda cap: cap == CapabilityId.CHANNEL_GATEWAY,
    )
    app = FastAPI()
    app.include_router(ch_router_mod.channels_router, prefix="/api/v1/dalton")
    client = TestClient(app)
    resp = client.post(
        "/api/v1/dalton/channels/internal/topics/lookup",
        json={
            "platform": "feishu",
            "chat_address": "feishu:oc_grp_1",
            "external_topic_id": "omt_topic_1",
            "purpose": "inbound_followup",
        },
        headers={"X-Service-Token": SERVICE_TOKEN},
    )
    assert resp.status_code == 422
