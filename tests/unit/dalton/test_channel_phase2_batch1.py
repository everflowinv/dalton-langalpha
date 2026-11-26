"""Unit tests for Channel Phase 2 Batch 1:
- Group bot-owned topics reservation / confirmation & I-1 vs I-7 race (Feishu & Discord)
- First-turn verified root_message_id & parent address normalization
- DM deliverable recorder (exact written-bytes SHA-256 + FileStamp match + race overwrite)
- Verified deliverable download & DM recipient re-verification (unbind after download before send)
- External link rewriting, visible-length 320-char summary budget, automation reports & plans
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import HTTPException

from src.dalton import channel_deliverables as cd
from src.dalton.capabilities import CapabilityId
from src.dalton.database import automation_notifications as auto_notify_db
from src.dalton.database import automation_reports as auto_reports_db
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database import data_task_notifications as dtn_db
from src.dalton.database import weixin_multi_account as wx_db
from src.dalton.monitors.models import MonitorRecord
from src.dalton.notify import dispatch as notify_dispatch
from src.ptc_agent.core.sandbox.file_version import FileStamp
from src.server.services.workspace_file_containment import ContainedFile


# ---------------------------------------------------------------------------
# Minimal in-memory DB harness for bot-topic & delivery DB helpers
# ---------------------------------------------------------------------------


class _FakeTopicCursor:
    """In-memory cursor modeling ``dalton_channel_bot_topics``, ``dalton_channel_deliveries``,
    ``dalton_channel_identities``, ``conversation_responses``, and ``workspaces``."""

    def __init__(self) -> None:
        self.topics: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.deliveries: dict[str, dict[str, Any]] = {}
        self.identities: dict[tuple[str, str], dict[str, Any]] = {}
        self.responses: dict[str, dict[str, Any]] = {}
        self.workspaces: dict[str, dict[str, Any]] = {}
        self._result_one: dict[str, Any] | None = None
        self._result_all: list[dict[str, Any]] = []

    async def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> None:
        norm = " ".join(sql.split()).upper()
        self._result_one = None
        self._result_all = []

        if "PG_ADVISORY_XACT_LOCK" in norm:
            return

        if "INSERT INTO DALTON_CHANNEL_BOT_TOPICS" in norm:
            if "'PENDING'" in norm:
                plat, addr, scope, root_id, init_uid, init_puid = params
                key = (plat, addr, scope)
                if key not in self.topics:
                    self.topics[key] = {
                        "platform": plat,
                        "chat_address": addr,
                        "canonical_scope_key": scope,
                        "external_topic_id": None,
                        "root_message_id": root_id,
                        "first_reply_message_id": None,
                        "initiator_dalton_user_id": init_uid,
                        "initiator_platform_user_id": init_puid,
                        "status": "pending",
                    }
                    self._result_one = dict(self.topics[key])
                return

            if "'CONFIRMED'" in norm:
                plat, addr, scope, ext_id, root_id, first_reply_id, init_uid, init_puid = params
                key = (plat, addr, scope)
                existing = self.topics.get(key)
                if existing is None:
                    self.topics[key] = {
                        "platform": plat,
                        "chat_address": addr,
                        "canonical_scope_key": scope,
                        "external_topic_id": ext_id,
                        "root_message_id": root_id,
                        "first_reply_message_id": first_reply_id,
                        "initiator_dalton_user_id": init_uid,
                        "initiator_platform_user_id": init_puid,
                        "status": "confirmed",
                    }
                else:
                    existing["external_topic_id"] = existing["external_topic_id"] or ext_id
                    existing["root_message_id"] = existing["root_message_id"] or root_id
                    existing["first_reply_message_id"] = (
                        existing["first_reply_message_id"] or first_reply_id
                    )
                    existing["status"] = "confirmed"
                self._result_one = dict(self.topics[key])
                return

        if "UPDATE DALTON_CHANNEL_BOT_TOPICS" in norm:
            if "SET EXTERNAL_TOPIC_ID = COALESCE" in norm:
                ext_id, root_id, first_reply_id, plat, addr, scope = params
                row = self.topics.get((plat, addr, scope))
                if row:
                    row["external_topic_id"] = row["external_topic_id"] or ext_id
                    row["root_message_id"] = row["root_message_id"] or root_id
                    row["first_reply_message_id"] = row["first_reply_message_id"] or first_reply_id
                    row["status"] = "confirmed"
                    self._result_one = dict(row)
                return
            root_id, first_reply_id, plat, addr, scope = params
            row = self.topics.get((plat, addr, scope))
            if row:
                row["root_message_id"] = row["root_message_id"] or root_id
                row["first_reply_message_id"] = row["first_reply_message_id"] or first_reply_id
                row["status"] = "confirmed"
                self._result_one = dict(row)
            return

        if "FROM DALTON_CHANNEL_BOT_TOPICS" in norm:
            where_part = norm.split("ORDER BY", 1)[0]
            require_confirmed = "STATUS = 'CONFIRMED'" in where_part
            require_ext = "EXTERNAL_TOPIC_ID IS NOT NULL" in where_part

            def _matches_status(row: dict[str, Any]) -> bool:
                if require_confirmed and row.get("status") != "confirmed":
                    return False
                if require_ext and not row.get("external_topic_id"):
                    return False
                return True

            if "(CANONICAL_SCOPE_KEY = %S OR ROOT_MESSAGE_ID = %S)" in norm:
                plat, addr, scope, root_id = params[:4]
                matches = [
                    r
                    for r in self.topics.values()
                    if r["platform"] == plat
                    and r["chat_address"] == addr
                    and (r["canonical_scope_key"] == scope or r["root_message_id"] == root_id)
                    and _matches_status(r)
                ]
                matches.sort(
                    key=lambda r: (
                        0 if r["status"] == "confirmed" else 1,
                        0 if r["canonical_scope_key"] == scope else 1,
                    )
                )
                if matches:
                    self._result_one = dict(matches[0])
                return

            if "EXTERNAL_TOPIC_ID = %S" in norm:
                plat, addr, ext_id = params
                for row in self.topics.values():
                    if (
                        row["platform"] == plat
                        and row["chat_address"] == addr
                        and row["external_topic_id"] == ext_id
                        and _matches_status(row)
                    ):
                        self._result_one = dict(row)
                        return
                return

            if "ROOT_MESSAGE_ID = %S" in norm:
                plat, addr, root_id = params[:3]
                for row in self.topics.values():
                    if (
                        row["platform"] == plat
                        and row["chat_address"] == addr
                        and row["root_message_id"] == root_id
                        and _matches_status(row)
                    ):
                        self._result_one = dict(row)
                        return
                return

            if "CANONICAL_SCOPE_KEY = %S" in norm:
                plat, addr, scope = params
                row = self.topics.get((plat, addr, scope))
                if row and _matches_status(row):
                    self._result_one = dict(row)
                return

        if "FROM DALTON_CHANNEL_DELIVERIES" in norm and "WHERE DELIVERY_KEY = %S" in norm:
            dkey = params[0]
            row = self.deliveries.get(dkey)
            if row:
                self._result_one = dict(row)
            return

        if "FROM DALTON_CHANNEL_IDENTITIES" in norm:
            uid, plat = params
            row = self.identities.get((uid, plat))
            if row:
                self._result_one = dict(row)
            return

        if "FROM CONVERSATION_RESPONSES" in norm:
            run_id = params[0]
            row = self.responses.get(run_id)
            if row:
                self._result_one = dict(row)
            return

        if "FROM WORKSPACES" in norm:
            ws_id = params[0]
            row = self.workspaces.get(ws_id)
            if row:
                self._result_one = dict(row)
            return

    async def fetchone(self) -> dict[str, Any] | None:
        return self._result_one

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._result_all)


class _FakeConn:
    def __init__(self, cur: _FakeTopicCursor) -> None:
        self._cur = cur

    @asynccontextmanager
    async def transaction(self):
        yield

    @asynccontextmanager
    async def cursor(self, **_kwargs: Any):
        yield self._cur


# ---------------------------------------------------------------------------
# 1. Bot-topic reservation & I-1 before I-7 race (Feishu & Discord — Decision #1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "chat_address", "root_msg_id", "external_topic_id", "alice_puid", "bob_puid"),
    [
        (
            "feishu",
            "feishu:oc_grp_1",
            "om_root_1",
            "omt_topic_1",
            "ou_alice",
            "ou_bob",
        ),
        (
            "discord",
            "discord:111111/222222",
            "999000111",
            "999000111",
            "100001",
            "200002",
        ),
    ],
)
async def test_bot_topic_other_member_i1_before_i7_preserves_reserved_initiator(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    chat_address: str,
    root_msg_id: str,
    external_topic_id: str,
    alice_puid: str,
    bob_puid: str,
) -> None:
    """Decision #1: Reserving pending topic before creation prevents another member's I-1
    from hijacking initiator attribution when I-1 arrives before I-7."""
    req_cap_mock = create_autospec(cg_db.require_channel_platform_capability, return_value=None)
    monkeypatch.setattr(cg_db, "require_channel_platform_capability", req_cap_mock)

    cur = _FakeTopicCursor()
    canonical_scope = (
        f"chat:oc_grp_1:topic:{root_msg_id}"
        if platform == "feishu"
        else f"chat:111111/222222:topic:{root_msg_id}"
    )

    # 1) I-6 pre-reserves Alice as initiator with status='pending' before topic creation.
    reserved = await cg_db._reserve_bot_topic_in_tx(
        cur,
        platform=platform,
        chat_address=chat_address,
        canonical_scope_key=canonical_scope,
        root_message_id=root_msg_id,
        initiator_platform_user_id=alice_puid,
        initiator_dalton_user_id="user-alice",
    )
    assert reserved is not None
    assert reserved["status"] == "pending"
    assert reserved["initiator_platform_user_id"] == alice_puid

    # 2) Before I-7 confirms, inbound_followup lookup does not expose pending row,
    #    and a concurrent reservation attempt by Bob on the same root_message_id
    #    returns Alice's pending row without hijacking initiator attribution.
    conn = _FakeConn(cur)
    pending_lookup = await cg_db.lookup_bot_topic(
        platform=platform,
        chat_address=chat_address,
        match_mode="inbound_followup",
        external_topic_id=external_topic_id,
        conn=conn,
    )
    assert pending_lookup["found"] is False

    bob_reserve = await cg_db._reserve_bot_topic_in_tx(
        cur,
        platform=platform,
        chat_address=chat_address,
        canonical_scope_key=f"chat:hijack:thread:{external_topic_id}",
        root_message_id=root_msg_id,
        initiator_platform_user_id=bob_puid,
        initiator_dalton_user_id="user-bob",
    )
    assert bob_reserve is not None
    assert bob_reserve["initiator_platform_user_id"] == alice_puid
    assert bob_reserve["canonical_scope_key"] == canonical_scope

    # 3) Alice's I-7 confirms the pending row with external_topic_id without overwriting initiator.
    confirmed = await cg_db._confirm_bot_topic_in_tx(
        cur,
        platform=platform,
        chat_address=chat_address,
        canonical_scope_key=canonical_scope,
        external_topic_id=external_topic_id,
        root_message_id=root_msg_id,
        first_reply_message_id="reply_first_1",
        initiator_platform_user_id=bob_puid,  # must NOT overwrite Alice's reserved initiator!
        initiator_dalton_user_id="user-bob",
    )
    assert confirmed is not None
    assert confirmed["status"] == "confirmed"
    assert confirmed["external_topic_id"] == external_topic_id
    assert confirmed["initiator_platform_user_id"] == alice_puid
    assert confirmed["canonical_scope_key"] == canonical_scope

    # 4) Follow-up lookup by external_topic_id and outbound lookup by canonical_scope_key return Alice.
    by_ext = await cg_db.lookup_bot_topic(
        platform=platform,
        chat_address=chat_address,
        match_mode="inbound_followup",
        external_topic_id=external_topic_id,
        conn=conn,
    )
    assert by_ext["found"] is True
    assert by_ext["initiator_platform_user_id"] == alice_puid
    assert by_ext["canonical_scope_key"] == canonical_scope

    by_scope = await cg_db.lookup_bot_topic(
        platform=platform,
        chat_address=chat_address,
        match_mode="outbound_delivery",
        canonical_scope_key=canonical_scope,
        conn=conn,
    )
    assert by_scope["found"] is True
    assert by_scope["external_topic_id"] == external_topic_id
    assert by_scope["first_reply_message_id"] == "reply_first_1"


def test_discord_parent_address_normalization_for_thread_targets() -> None:
    """Discord `:thread:` addresses normalize to parent guild/channel for topic & binding lookups."""
    assert (
        cg_db._extract_discord_parent_address("discord:111/222:thread:333")
        == "discord:111/222"
    )
    assert cg_db._extract_discord_parent_address("discord:111/222") == "discord:111/222"
    assert (
        cg_db._normalize_topic_chat_address("discord", "discord:111/222:thread:333")
        == "discord:111/222"
    )
    assert (
        cg_db._parse_topic_root_from_scope_key("chat:oc_grp_1:topic:om_root_99")
        == "om_root_99"
    )
    assert cg_db._parse_topic_root_from_scope_key("chat:oc_grp_1:thread:omt_1") is None


# ---------------------------------------------------------------------------
# 2. DM Recipient Re-Verification (Decision #6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_dm_delivery_recipient_rejects_unbind_and_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision #6: Unbinding or rebinding after download and before send fails verification."""
    req_cap_mock = create_autospec(cg_db.require_channel_platform_capability, return_value=None)
    epoch_mock = create_autospec(wx_db.verify_gateway_epoch_in_tx, return_value=None)
    roster_mock = create_autospec(cg_db.check_team_member_status_live, return_value="active")
    monkeypatch.setattr(cg_db, "require_channel_platform_capability", req_cap_mock)
    monkeypatch.setattr(wx_db, "verify_gateway_epoch_in_tx", epoch_mock)
    monkeypatch.setattr(cg_db, "check_team_member_status_live", roster_mock)

    cur = _FakeTopicCursor()
    cur.deliveries["deliverables:run-1"] = {
        "delivery_key": "deliverables:run-1",
        "kind": "fallback",
        "platform": "feishu",
        "user_id": "user-alice",
        "thread_id": "tid-1",
        "run_id": "run-1",
        "is_current": True,
        "target_address": "feishu:@me",
        "status": "in_flight",
        "delivery_attempt": 1,
        "dm_recipient_platform_user_id": "ou_alice_orig",
    }
    cur.identities[("user-alice", "feishu")] = {"platform_user_id": "ou_alice_orig"}
    conn = _FakeConn(cur)

    # Matching binding succeeds
    ok = await cg_db.verify_dm_delivery_recipient(
        delivery_key="deliverables:run-1",
        delivery_attempt=1,
        gateway_epoch=1,
        conn=conn,
    )
    assert ok["authorized"] is True
    assert ok["recipient_id"] == "ou_alice_orig"

    # Rebind to a different Feishu account -> rejected with 403
    cur.identities[("user-alice", "feishu")] = {"platform_user_id": "ou_alice_new"}
    with pytest.raises(HTTPException) as exc_rebound:
        await cg_db.verify_dm_delivery_recipient(
            delivery_key="deliverables:run-1",
            delivery_attempt=1,
            gateway_epoch=1,
            conn=conn,
        )
    assert exc_rebound.value.status_code == 403
    assert exc_rebound.value.detail["code"] == "deliverable_recipient_denied"

    # Unbind completely -> rejected with 403
    del cur.identities[("user-alice", "feishu")]
    with pytest.raises(HTTPException) as exc_unbound:
        await cg_db.verify_dm_delivery_recipient(
            delivery_key="deliverables:run-1",
            delivery_attempt=1,
            gateway_epoch=1,
            conn=conn,
        )
    assert exc_unbound.value.status_code == 403
    assert exc_unbound.value.detail["code"] == "deliverable_recipient_denied"

    # Background run missing pinned dm_recipient_platform_user_id -> rejected with 403
    cur.deliveries["deliverables:run-bg"] = {
        "delivery_key": "deliverables:run-bg",
        "kind": "fallback",
        "platform": "feishu",
        "user_id": "user-alice",
        "thread_id": "tid-1",
        "run_id": "run-bg",
        "is_current": True,
        "target_address": "feishu:@me",
        "status": "in_flight",
        "delivery_attempt": 1,
        "dm_recipient_platform_user_id": None,
    }
    with pytest.raises(HTTPException) as exc_missing:
        await cg_db.verify_dm_delivery_recipient(
            delivery_key="deliverables:run-bg",
            delivery_attempt=1,
            gateway_epoch=1,
            conn=conn,
        )
    assert exc_missing.value.status_code == 403
    assert exc_missing.value.detail["code"] == "deliverable_recipient_denied"


# ---------------------------------------------------------------------------
# 3. TurnDeliverableRecorder (Decision #5: exact written bytes + race guard)
# ---------------------------------------------------------------------------


class _FakeSandboxForRecorder:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files
        self.working_directory = "/home/workspace"


@pytest.mark.asyncio
async def test_turn_deliverable_recorder_exact_hash_and_race_overwrite_rejected() -> None:
    """Decision #5: Register only when post-write FileStamp bytes match exact written SHA-256;
    if overwritten between write return and callback read, refuse registration."""
    good_bytes = b"%PDF-1.4\nhello report\n"
    good_sha = hashlib.sha256(good_bytes).hexdigest()
    tampered_bytes = b"%PDF-1.4\ntampered by concurrent command\n"

    sandbox = _FakeSandboxForRecorder(
        {
            "/home/workspace/results/q3.pdf": good_bytes,
            "/home/workspace/results/raced.pdf": tampered_bytes,
        }
    )

    async def _fake_versioned_read(
        _sb: Any,
        path: str,
        *,
        work_dir: str,
        **_kwargs: Any,
    ) -> ContainedFile | None:
        _ = work_dir
        raw = sandbox._files.get(path)
        if raw is None:
            return None
        return ContainedFile(
            canonical=path,
            size=len(raw),
            content=raw,
            stamp=FileStamp(size=len(raw), mtime="1700000000.0", ctime="1700000000.0", inode="42"),
            settled=True,
        )

    recorder = cd.TurnDeliverableRecorder(
        thread_id="tid-1",
        run_id="run-1",
        workspace_id="ws-1",
        sandbox=sandbox,
        work_dir="/home/workspace",
        versioned_reader=_fake_versioned_read,
    )

    # 1) Normal Write where read-back matches written_sha256 -> registered
    await recorder.on_file_operation(
        {
            "operation": "write_file",
            "file_path": "/home/workspace/results/q3.pdf",
            "written_sha256": good_sha,
            "written_size": len(good_bytes),
        }
    )
    # 2) Write where file was overwritten BEFORE callback read -> MUST NOT register!
    await recorder.on_file_operation(
        {
            "operation": "write_file",
            "file_path": "/home/workspace/results/raced.pdf",
            "written_sha256": good_sha,
            "written_size": len(good_bytes),
        }
    )
    # 3) Excluded directory (.agents/) and excluded extension (.py) -> NOT registered
    await recorder.on_file_operation(
        {
            "operation": "write_file",
            "file_path": "/home/workspace/.agents/notes.md",
            "written_sha256": good_sha,
            "written_size": len(good_bytes),
        }
    )
    await recorder.on_file_operation(
        {
            "operation": "write_file",
            "file_path": "/home/workspace/scripts/run.py",
            "written_sha256": good_sha,
            "written_size": len(good_bytes),
        }
    )

    items = recorder.snapshot()
    assert len(items) == 1
    assert items[0]["path"] == "results/q3.pdf"
    assert items[0]["sha256"] == good_sha
    assert items[0]["size"] == len(good_bytes)
    assert items[0]["workspace_id"] == "ws-1"


def test_deliverable_path_allowlist_and_magic_bytes() -> None:
    """§4.3 path rules & magic-byte verification reject scripts, HTML, hidden dirs, and spoofed PDFs."""
    assert cd.is_deliverable_path_allowed("results/report.md")[0] is True
    assert cd.is_deliverable_path_allowed("charts/fig.png")[0] is True
    assert cd.is_deliverable_path_allowed("report.html")[0] is False
    assert cd.is_deliverable_path_allowed(".agents/scratch.md")[0] is False
    assert cd.is_deliverable_path_allowed("node_modules/pkg/README.md")[0] is False
    assert cd.is_deliverable_path_allowed("src/app.py")[0] is False
    assert cd.is_deliverable_path_allowed("../escape/report.md")[0] is False

    # Valid PDF magic vs spoofed PDF magic
    assert cd.validate_deliverable_magic_bytes("report.pdf", b"%PDF-1.7\nbody") is True
    assert cd.validate_deliverable_magic_bytes("report.pdf", b"<html>not pdf</html>") is False
    # Valid PNG magic vs spoofed PNG
    assert (
        cd.validate_deliverable_magic_bytes("chart.png", b"\x89PNG\r\n\x1a\nrest")
        is True
    )
    assert cd.validate_deliverable_magic_bytes("chart.png", b"plain text") is False


# ---------------------------------------------------------------------------
# 4. Verified Deliverable Download (Decisions #5 & #6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_verified_deliverable_mtime_preserving_overwrite_returns_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision #5: Even if `cp -p` / `touch -r` preserves mtime & size, SHA-256 mismatch returns 409."""
    orig_bytes = b"Revenue grew 20% YoY."
    same_len_tampered = b"Revenue fell 99% YoY."
    assert len(orig_bytes) == len(same_len_tampered)
    orig_sha = hashlib.sha256(orig_bytes).hexdigest()
    stamp = FileStamp(size=len(orig_bytes), mtime="1700000000.0", ctime="1700000000.0", inode="7")

    req_cap_cd = create_autospec(cd.require_channel_platform_capability, return_value=None)
    req_cap_cg = create_autospec(cg_db.require_channel_platform_capability, return_value=None)
    epoch_mock = create_autospec(wx_db.verify_gateway_epoch_in_tx, return_value=None)
    roster_mock = create_autospec(cg_db.check_team_member_status_live, return_value="active")
    monkeypatch.setattr(cd, "require_channel_platform_capability", req_cap_cd)
    monkeypatch.setattr(cg_db, "require_channel_platform_capability", req_cap_cg)
    monkeypatch.setattr(wx_db, "verify_gateway_epoch_in_tx", epoch_mock)
    monkeypatch.setattr(cg_db, "check_team_member_status_live", roster_mock)

    cur = _FakeTopicCursor()
    cur.deliveries["deliverables:run-1"] = {
        "delivery_key": "deliverables:run-1",
        "kind": "fallback",
        "platform": "feishu",
        "user_id": "user-alice",
        "thread_id": "tid-1",
        "run_id": "run-1",
        "is_current": True,
        "target_address": "feishu:@me",
        "status": "in_flight",
        "delivery_attempt": 1,
        "dm_recipient_platform_user_id": "ou_alice",
    }
    cur.identities[("user-alice", "feishu")] = {"platform_user_id": "ou_alice"}
    cur.responses["run-1"] = {
        "conversation_response_id": "run-1",
        "thread_id": "tid-1",
        "metadata": {
            "turn_deliverables": [
                {
                    "workspace_id": "ws-1",
                    "path": "results/summary.md",
                    "size": len(orig_bytes),
                    "sha256": orig_sha,
                    "stamp": {
                        "size": stamp.size,
                        "mtime": stamp.mtime,
                        "ctime": stamp.ctime,
                        "inode": stamp.inode,
                    },
                }
            ]
        },
    }
    cur.workspaces["ws-1"] = {"workspace_id": "ws-1", "user_id": "user-alice"}
    conn = _FakeConn(cur)

    fake_sb = _FakeSandboxForRecorder({"/home/workspace/results/summary.md": same_len_tampered})

    async def _fake_live_sb(_ws_id: str) -> tuple[Any, str]:
        return fake_sb, "/home/workspace"

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
            size=len(same_len_tampered),
            content=same_len_tampered,
            stamp=stamp,
            settled=True,
        )

    with pytest.raises(HTTPException) as exc:
        await cd.download_verified_deliverable(
            delivery_key="deliverables:run-1",
            delivery_attempt=1,
            workspace_id="ws-1",
            path="results/summary.md",
            gateway_epoch=1,
            conn=conn,
            get_live_sandbox_fn=_fake_live_sb,
            versioned_reader_fn=_fake_versioned_read,
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_download_verified_deliverable_rejects_group_and_unbound_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision #6 & #7: Group delivery_key or unbound DM recipient returns 403."""
    req_cap_cd = create_autospec(cd.require_channel_platform_capability, return_value=None)
    req_cap_cg = create_autospec(cg_db.require_channel_platform_capability, return_value=None)
    epoch_mock = create_autospec(wx_db.verify_gateway_epoch_in_tx, return_value=None)
    roster_mock = create_autospec(cg_db.check_team_member_status_live, return_value="active")
    monkeypatch.setattr(cd, "require_channel_platform_capability", req_cap_cd)
    monkeypatch.setattr(cg_db, "require_channel_platform_capability", req_cap_cg)
    monkeypatch.setattr(wx_db, "verify_gateway_epoch_in_tx", epoch_mock)
    monkeypatch.setattr(cg_db, "check_team_member_status_live", roster_mock)

    cur = _FakeTopicCursor()
    cur.deliveries["deliverables:run-grp"] = {
        "delivery_key": "deliverables:run-grp",
        "kind": "fallback",
        "platform": "feishu",
        "user_id": "user-alice",
        "thread_id": "tid-grp",
        "run_id": "run-grp",
        "is_current": True,
        "target_address": "feishu:oc_grp_1",
        "status": "in_flight",
        "delivery_attempt": 1,
        "dm_recipient_platform_user_id": None,
    }
    conn = _FakeConn(cur)

    with pytest.raises(HTTPException) as exc:
        await cd.download_verified_deliverable(
            delivery_key="deliverables:run-grp",
            delivery_attempt=1,
            workspace_id="ws-1",
            path="results/summary.md",
            gateway_epoch=1,
            conn=conn,
        )
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# 5. Link Closure, Visible-Length 320-Char Budget & Notification Plans
# ---------------------------------------------------------------------------


def test_rewrite_workspace_links_for_external_reference_and_workspace_fallback() -> None:
    """§4.2: Reference links `[ref]`, `[ref][]`, `[text][ref]` expand inline and remove definitions;
    workspace_id fallback works when thread_id is absent; loopback degrades to plain hint."""
    text = (
        "见 [完整报告][r1]、[r2][]、[r3] 与 [跨区](__wsref__/ws-other/charts/a.png)。\n\n"
        "[r1]: /home/workspace/results/report.md\n"
        "[r2]: results/table.csv\n"
        "[r3]: /home/daytona/notes/memo.md\n"
    )
    out = notify_dispatch.rewrite_workspace_links_for_external(
        text,
        public_base="https://dalton.example.com",
        thread_id="tid-123",
    )
    assert "[r1]:" not in out
    assert "[r2]:" not in out
    assert "[r3]:" not in out
    assert (
        "[完整报告](https://dalton.example.com/chat/t/tid-123?file=%2Fhome%2Fworkspace%2Fresults%2Freport.md)"
        in out
    )
    assert (
        "[r2](https://dalton.example.com/chat/t/tid-123?file=results%2Ftable.csv)"
        in out
    )
    assert (
        "[r3](https://dalton.example.com/chat/t/tid-123?file=%2Fhome%2Fdaytona%2Fnotes%2Fmemo.md)"
        in out
    )
    assert (
        "[跨区](https://dalton.example.com/chat/ws-other?file=charts%2Fa.png)"
        in out
    )

    # Without thread_id, falls back to workspace_id route
    ws_out = notify_dispatch.rewrite_workspace_links_for_external(
        "见 [完整报告](results/report.md)。",
        public_base="https://dalton.example.com",
        thread_id=None,
        workspace_id="ws-abc",
    )
    assert (
        ws_out
        == "见 [完整报告](https://dalton.example.com/chat/ws-abc?file=results%2Freport.md)。"
    )

    # Loopback public_base degrades cleanly to plain hint
    local_out = notify_dispatch.rewrite_workspace_links_for_external(
        "见 [完整报告](results/report.md)。",
        public_base="http://127.0.0.1:5173",
        thread_id="tid-123",
    )
    assert "http://" not in local_out
    assert "完整报告（在网页工作区查看：results/report.md）" in local_out


def test_extract_summary_lines_counts_visible_link_label_against_320_budget() -> None:
    """Codex r6 #7: Long deep link URLs must NOT eat into the 320-char visible summary budget."""
    long_path = "results/" + ("very_long_segment_" * 14) + "report.md"
    src = f"第一句包含 [季度报告]({long_path})。\n第二句确认目标价上调至 200 元。"
    lines = notify_dispatch.extract_summary_lines(
        src,
        max_chars=320,
        thread_id="tid-budget-1",
        public_base="https://dalton.example.com",
    )
    assert len(lines) == 2
    assert "https://dalton.example.com/chat/t/tid-budget-1?file=" in lines[0]
    assert "第二句确认目标价上调至 200 元。" in lines[1]


def test_automation_report_rewrites_workspace_links_before_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.2: Workspace links in automation report cards/text are rewritten before `_validate_card`
    (and neutralized cleanly when public URL is local/empty so cards are not rejected)."""
    card = {
        "schema": "2.0",
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "详见 [研究报告](/home/workspace/results/report.md) 与 ![图](charts/c.png)。",
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "打开报告"},
                    "behaviors": [
                        {"type": "open_url", "default_url": "results/report.md"}
                    ],
                },
            ]
        },
    }

    # Public URL configured -> rewritten to https deep links and passes validation
    monkeypatch.setattr(
        "src.dalton.settings.get_dalton_public_app_url",
        lambda: "https://dalton.example.com",
    )
    rw_cards, _, _ = auto_reports_db._rewrite_automation_report_inputs(
        cards=[card],
        failure_text=None,
        text=None,
        thread_id="tid-rep-1",
        workspace_id="ws-rep-1",
    )
    prepared, err = auto_reports_db.prepare_automation_report_content(
        profile="market-digest-am",
        cards=rw_cards,
        failure_text=None,
        text=None,
    )
    assert err is None
    assert prepared is not None
    md_content = prepared["cards"][0]["body"]["elements"][0]["content"]
    assert "https://dalton.example.com/chat/t/tid-rep-1?file=" in md_content
    btn_url = prepared["cards"][0]["body"]["elements"][1]["behaviors"][0]["default_url"]
    assert btn_url.startswith("https://dalton.example.com/chat/t/tid-rep-1?file=")

    # Local/loopback URL -> button degrades to plain markdown & passes validation cleanly
    monkeypatch.setattr(
        "src.dalton.settings.get_dalton_public_app_url",
        lambda: "http://localhost:5173",
    )
    rw_local_cards, _, _ = auto_reports_db._rewrite_automation_report_inputs(
        cards=[card],
        failure_text=None,
        text=None,
        thread_id="tid-rep-1",
        workspace_id="ws-rep-1",
    )
    prepared_local, err_local = auto_reports_db.prepare_automation_report_content(
        profile="market-digest-am",
        cards=rw_local_cards,
        failure_text=None,
        text=None,
    )
    assert err_local is None
    assert prepared_local is not None


@pytest.mark.asyncio
async def test_data_task_monitor_url_respects_capabilities_and_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.2: `monitor_url` is returned only when CHANNEL_GATEWAY + DATA_JOBS are enabled,
    job_key is registered, public URL is reachable, and visibility allows the target audience."""
    now = datetime.now(timezone.utc)
    company_monitor = MonitorRecord(
        monitor_id="mon-1",
        slug="cn-intraday",
        artifact_id="art-1",
        owner_user_id="system",
        title="A股盘中监测",
        category="cn",
        sort_order=10,
        visibility="company",
        published_seq=1,
        keep_updated=True,
        source_kind="data_job",
        source_ref="cn-intraday",
        acl_revision=1,
        created_at=now,
        updated_at=now,
        workspace_id="ws-sys",
        member_user_ids=(),
    )
    own_monitor = MonitorRecord(
        **{**company_monitor.__dict__, "visibility": "own", "owner_user_id": "user-alice"}
    )

    from src.dalton.monitors import db as monitor_db
    from src.dalton.monitors import permissions as monitor_perms

    def _cap_all(cap: CapabilityId) -> bool:
        return cap in (CapabilityId.CHANNEL_GATEWAY, CapabilityId.DATA_JOBS)

    monkeypatch.setattr(
        "src.dalton.settings.get_dalton_public_app_url",
        lambda: "https://dalton.example.com",
    )
    monkeypatch.setattr("src.dalton.capabilities.has_capability", _cap_all)
    monkeypatch.setattr(monitor_perms, "has_capability", _cap_all)

    real_get_monitor_by_data_job = monitor_db.get_monitor_by_data_job
    get_mon_mock = create_autospec(
        real_get_monitor_by_data_job,
        return_value=company_monitor,
    )
    can_view_mock = create_autospec(
        monitor_perms.can_view_monitor,
        return_value=True,
    )
    monkeypatch.setattr(monitor_db, "get_monitor_by_data_job", get_mon_mock)
    monkeypatch.setattr(monitor_perms, "can_view_monitor", can_view_mock)

    group_auth = dtn_db.AuthorizedDataTaskTarget(
        allowed=True,
        run_id="run-1",
        job_key="cn-intraday",
        owner_user_id="user-alice",
        platform="feishu",
        target_kind="group",
    )
    url = await dtn_db._resolve_data_task_monitor_url(group_auth)
    assert url == "https://dalton.example.com/monitors/cn-intraday"

    # Private (`own`) monitor must NOT be exposed to group chats
    get_own_mock = create_autospec(
        real_get_monitor_by_data_job,
        return_value=own_monitor,
    )
    monkeypatch.setattr(monitor_db, "get_monitor_by_data_job", get_own_mock)
    assert await dtn_db._resolve_data_task_monitor_url(group_auth) is None

    # Standalone core without DATA_JOBS -> returns None
    def _cap_no_data_jobs(cap: CapabilityId) -> bool:
        return cap == CapabilityId.CHANNEL_GATEWAY

    monkeypatch.setattr("src.dalton.capabilities.has_capability", _cap_no_data_jobs)
    monkeypatch.setattr(monitor_perms, "has_capability", _cap_no_data_jobs)
    assert await dtn_db._resolve_data_task_monitor_url(group_auth) is None


@pytest.mark.asyncio
async def test_automation_notification_plan_returns_nullable_thread_and_workspace_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.2: `decide_automation_notification_plan` always returns top-level `thread_id` & `workspace_id`."""
    cur = _FakeTopicCursor()
    row = {
        "delivery_id": "11111111-1111-4111-8111-111111111111",
        "execution_id": "22222222-2222-4222-8222-222222222222",
        "automation_id": "33333333-3333-4333-8333-333333333333",
        "user_id": "user-alice",
        "channel": "discord",
        "target": "discord:user:100001",
        "status": "pending",
        "conversation_thread_id": "tid-from-exec",
        "workspace_id": "ws-from-auto",
        "payload_json": {
            "transport": "gateway",
            "source": "automation_completion",
            "text": "完成摘要",
        },
    }
    monkeypatch.setattr(
        auto_notify_db,
        "_parent_is_deleted",
        create_autospec(auto_notify_db._parent_is_deleted, return_value=False),
    )
    monkeypatch.setattr(
        auto_notify_db,
        "_eligibility",
        create_autospec(auto_notify_db._eligibility, return_value=(True, None)),
    )
    monkeypatch.setattr(
        auto_notify_db,
        "_existing_channel_status",
        create_autospec(auto_notify_db._existing_channel_status, return_value=None),
    )

    async def _resolve_enabled(_uid: str | None) -> bool:
        return True

    async def _resolve_overrides(_uid: str | None) -> dict[str, bool]:
        return {}

    async def _resolve_targets(_uid: str | None) -> list[str]:
        return [row["target"]]

    async def _resolve_routes(_uid: str | None) -> dict[str, list[str]]:
        return {}

    async def _is_admin(_uid: str | None) -> bool:
        return False

    plan = await auto_notify_db.decide_automation_notification_plan(
        cur,
        row,
        delivery_id=row["delivery_id"],
        resolve_enabled_fn=_resolve_enabled,
        resolve_overrides_fn=_resolve_overrides,
        resolve_targets_fn=_resolve_targets,
        resolve_routes_fn=_resolve_routes,
        is_admin_fn=_is_admin,
    )
    assert plan["authorized"] is True
    assert plan["thread_id"] == "tid-from-exec"
    assert plan["workspace_id"] == "ws-from-auto"


# ---------------------------------------------------------------------------
# 6. Review r6 Fixes: Workspace Root, Online/Offline Download, Weixin Preflight,
#    I-1 vs I-7 Lock Order, Discord Parent Channel Binding, Link Hygiene
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_deliverable_recorder_uses_workspace_dir_not_computer_root() -> None:
    """Item 3: Write recorder uses actual workspace dir (`sandbox.workspace(project).workspace`),
    stripping the workspace folder prefix and ignoring sibling workspaces."""
    good_bytes = b"# Quarterly Analysis\n"
    good_sha = hashlib.sha256(good_bytes).hexdigest()
    ws_dir = "/home/workspace/Research"

    async def _fake_versioned_read(
        _sb: Any,
        path: str,
        *,
        work_dir: str,
        **_kwargs: Any,
    ) -> ContainedFile | None:
        assert work_dir == ws_dir
        if path != f"{ws_dir}/results/report.md":
            return None
        return ContainedFile(
            canonical=path,
            size=len(good_bytes),
            content=good_bytes,
            stamp=FileStamp(size=len(good_bytes), mtime="1700000001.0", ctime="1700000001.0", inode="88"),
            settled=True,
        )

    recorder = cd.TurnDeliverableRecorder(
        thread_id="tid-ws-1",
        run_id="run-ws-1",
        workspace_id="ws-research",
        sandbox=object(),
        work_dir=ws_dir,
        versioned_reader=_fake_versioned_read,
    )

    # Write inside this workspace folder -> stored relative to workspace root (`results/report.md`), NOT `Research/results/report.md`
    await recorder.on_file_operation(
        {
            "operation": "write_file",
            "file_path": "/home/workspace/Research/results/report.md",
            "written_sha256": good_sha,
            "written_size": len(good_bytes),
        }
    )
    # Write inside a sibling workspace folder -> ignored
    await recorder.on_file_operation(
        {
            "operation": "write_file",
            "file_path": "/home/workspace/OtherWS/results/report.md",
            "written_sha256": good_sha,
            "written_size": len(good_bytes),
        }
    )

    items = recorder.snapshot()
    assert len(items) == 1
    assert items[0]["path"] == "results/report.md"
    assert items[0]["size"] == len(good_bytes)
    assert items[0]["workspace_id"] == "ws-research"


@pytest.mark.asyncio
async def test_default_get_live_sandbox_uses_get_session_if_running_and_refuses_backup_when_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 3: Download resolves live sandbox via `get_session_if_running` + `layout_for` across workers,
    reads backup ONLY when confirmed stopped, and raises 503 (never stale backup) if running sandbox is unreachable."""
    from types import SimpleNamespace
    from ptc_agent.core.paths import SandboxLayout
    from src.server.database import workspace as ws_db
    from src.server.services import workspace_files as ws_files
    from src.server.services.workspace_manager import WorkspaceManager

    ws_row_running = {
        "workspace_id": "ws-research",
        "user_id": "user-alice",
        "status": "running",
        "dir_name": "Research",
        "computer_root_dir": "/home/workspace",
    }
    ws_row_stopped = {
        **ws_row_running,
        "status": "stopped",
    }

    fake_sandbox = SimpleNamespace(tag="worker-b-sandbox")
    fake_session = SimpleNamespace(sandbox=fake_sandbox)
    session_calls: list[tuple[str, str | None]] = []

    class _FakeWsMgr:
        def __init__(self, mode: str) -> None:
            self.mode = mode

        async def get_session_if_running(
            self, workspace_id: str, user_id: str | None = None
        ) -> Any:
            session_calls.append((workspace_id, user_id))
            if self.mode == "running":
                return fake_session
            if self.mode == "stopped":
                return None
            raise RuntimeError("SSH tunnel temporarily unreachable")

    monkeypatch.setattr(
        ws_files,
        "layout_for",
        create_autospec(
            ws_files.layout_for,
            return_value=SandboxLayout.for_root("/home/workspace").for_workspace("Research"),
        ),
    )

    real_get_workspace = ws_db.get_workspace

    # 1) Running on another worker -> attaches via get_session_if_running and resolves /home/workspace/Research
    monkeypatch.setattr(
        ws_db, "get_workspace", create_autospec(real_get_workspace, return_value=ws_row_running)
    )
    monkeypatch.setattr(
        WorkspaceManager,
        "get_instance",
        classmethod(lambda _cls: _FakeWsMgr("running")),
    )
    sb, work_dir = await cd._default_get_live_sandbox("ws-research", user_id="user-alice")
    assert sb is fake_sandbox
    assert work_dir == "/home/workspace/Research"
    assert session_calls == [("ws-research", "user-alice")]

    # 2) Running but unreachable -> raises 503 (never falls back to offline backup!)
    monkeypatch.setattr(
        WorkspaceManager,
        "get_instance",
        classmethod(lambda _cls: _FakeWsMgr("unreachable")),
    )
    with pytest.raises(HTTPException) as exc_unreach:
        await cd._default_get_live_sandbox("ws-research", user_id="user-alice")
    assert exc_unreach.value.status_code == 503
    assert exc_unreach.value.detail["code"] == "deliverable_sandbox_unavailable"

    # 3) Confirmed stopped via workspace status -> returns (None, None) without dialing sandbox
    session_calls.clear()
    monkeypatch.setattr(
        ws_db, "get_workspace", create_autospec(real_get_workspace, return_value=ws_row_stopped)
    )
    sb_stopped, dir_stopped = await cd._default_get_live_sandbox("ws-research", user_id="user-alice")
    assert sb_stopped is None
    assert dir_stopped is None
    assert session_calls == []

    # 3b) Transitioning (`starting` / `stopping`) -> raises 503 immediately without reading backup
    for trans_status in ("starting", "stopping"):
        session_calls.clear()
        monkeypatch.setattr(
            ws_db,
            "get_workspace",
            create_autospec(
                real_get_workspace, return_value={**ws_row_running, "status": trans_status}
            ),
        )
        with pytest.raises(HTTPException) as exc_trans:
            await cd._default_get_live_sandbox("ws-research", user_id="user-alice")
        assert exc_trans.value.status_code == 503
        assert exc_trans.value.detail["code"] == "deliverable_sandbox_unavailable"
        assert session_calls == []

    # 4) `get_session_if_running` returns None:
    #    - If re-check confirms `stopped`, allow offline backup `(None, None)`.
    #    - If re-check sees `stopping` / `running`, refuse stale backup with 503.
    ws_sequence_stopped = iter([ws_row_running, ws_row_stopped])
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(real_get_workspace, side_effect=lambda *_a, **_kw: next(ws_sequence_stopped)),
    )
    monkeypatch.setattr(
        WorkspaceManager,
        "get_instance",
        classmethod(lambda _cls: _FakeWsMgr("stopped")),
    )
    sb_none, dir_none = await cd._default_get_live_sandbox("ws-research", user_id="user-alice")
    assert sb_none is None
    assert dir_none is None

    ws_sequence_stopping = iter([ws_row_running, {**ws_row_running, "status": "stopping"}])
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(real_get_workspace, side_effect=lambda *_a, **_kw: next(ws_sequence_stopping)),
    )
    with pytest.raises(HTTPException) as exc_race:
        await cd._default_get_live_sandbox("ws-research", user_id="user-alice")
    assert exc_race.value.status_code == 503
    assert exc_race.value.detail["code"] == "deliverable_sandbox_unavailable"


@pytest.mark.asyncio
async def test_download_verified_deliverable_stopping_refuses_stale_backup_when_online_updated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8 #3: When DB status has entered `stopping` (online file already overwritten, stop backup
    not yet flushed so offline mirror still holds the old turn-matching bytes), download MUST
    return 503 `deliverable_sandbox_unavailable` and NEVER read or serve the stale backup."""
    from src.server.database import workspace as ws_db

    old_bytes = b"%PDF-1.4\nv1-recorded-in-turn\n"
    old_sha = hashlib.sha256(old_bytes).hexdigest()

    monkeypatch.setattr(
        cd,
        "require_channel_platform_capability",
        create_autospec(cd.require_channel_platform_capability, return_value=None),
    )
    monkeypatch.setattr(
        cg_db,
        "require_channel_platform_capability",
        create_autospec(cg_db.require_channel_platform_capability, return_value=None),
    )
    monkeypatch.setattr(
        wx_db,
        "verify_gateway_epoch_in_tx",
        create_autospec(wx_db.verify_gateway_epoch_in_tx, return_value=None),
    )
    monkeypatch.setattr(
        cg_db,
        "check_team_member_status_live",
        create_autospec(cg_db.check_team_member_status_live, return_value="active"),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={
                "workspace_id": "ws-stopping-1",
                "user_id": "user-alice",
                "status": "stopping",
                "dir_name": "Research",
                "computer_root_dir": "/home/workspace",
            },
        ),
    )

    cur = _FakeTopicCursor()
    cur.identities[("user-alice", "feishu")] = {"platform_user_id": "ou_alice"}
    cur.workspaces["ws-stopping-1"] = {
        "workspace_id": "ws-stopping-1",
        "user_id": "user-alice",
    }
    cur.deliveries["deliverables:run-stopping-1"] = {
        "delivery_key": "deliverables:run-stopping-1",
        "kind": "fallback",
        "platform": "feishu",
        "user_id": "user-alice",
        "thread_id": "tid-stopping-1",
        "run_id": "run-stopping-1",
        "is_current": True,
        "target_address": "feishu:@me",
        "status": "in_flight",
        "delivery_attempt": 1,
        "dm_recipient_platform_user_id": "ou_alice",
    }
    cur.responses["run-stopping-1"] = {
        "conversation_response_id": "run-stopping-1",
        "thread_id": "tid-stopping-1",
        "metadata": {
            "turn_deliverables": [
                {
                    "workspace_id": "ws-stopping-1",
                    "path": "results/report.pdf",
                    "sha256": old_sha,
                    "size": len(old_bytes),
                    "stamp": {
                        "size": len(old_bytes),
                        "mtime": "1700000001.0",
                        "ctime": "1700000001.0",
                        "inode": "99",
                    },
                }
            ]
        },
    }
    conn = _FakeConn(cur)

    backup_read_calls = 0

    async def _stale_backup_reader(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal backup_read_calls
        backup_read_calls += 1
        # Offline backup still has old matching bytes even though online sandbox was overwritten!
        return {"content": old_bytes, "size": len(old_bytes)}

    with pytest.raises(HTTPException) as exc:
        await cd.download_verified_deliverable(
            delivery_key="deliverables:run-stopping-1",
            delivery_attempt=1,
            workspace_id="ws-stopping-1",
            path="results/report.pdf",
            gateway_epoch=7,
            conn=conn,
            offline_backup_reader_fn=_stale_backup_reader,
        )
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "deliverable_sandbox_unavailable"
    assert backup_read_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preflight_resp", "expected_status", "expected_code"),
    [
        (
            {
                "authorized": False,
                "decision": "deny",
                "retryable": False,
                "deny_code": "weixin_account_unbound",
            },
            403,
            "weixin_account_unbound",
        ),
        (
            {
                "authorized": False,
                "decision": "deny",
                "retryable": False,
                "deny_code": "delivery_attempt_mismatch",
            },
            409,
            "delivery_attempt_mismatch",
        ),
        (
            {
                "authorized": False,
                "decision": "defer",
                "retryable": True,
                "deny_code": "roster_lookup_error",
            },
            503,
            "roster_lookup_error",
        ),
    ],
)
async def test_download_verified_deliverable_weixin_preflight_denied_or_deferred_does_zero_reads(
    monkeypatch: pytest.MonkeyPatch,
    preflight_resp: dict[str, Any],
    expected_status: int,
    expected_code: str,
) -> None:
    """Item 4: Weixin deliverable download requires `preflight_weixin_send` -> `authorized is True`,
    returning 403/409/503 on deny/defer with ZERO sandbox reads and ZERO backup reads."""
    monkeypatch.setattr(
        cd,
        "require_channel_platform_capability",
        create_autospec(cd.require_channel_platform_capability, return_value=None),
    )
    monkeypatch.setattr(
        wx_db,
        "preflight_weixin_send",
        create_autospec(wx_db.preflight_weixin_send, return_value=preflight_resp),
    )

    cur = _FakeTopicCursor()
    cur.deliveries["deliverables:run-wx-1"] = {
        "delivery_key": "deliverables:run-wx-1",
        "kind": "fallback",
        "platform": "weixin",
        "user_id": "user-alice",
        "thread_id": "tid-wx-1",
        "run_id": "run-wx-1",
        "is_current": True,
        "target_address": "weixin:@me",
        "status": "in_flight",
        "delivery_attempt": 1,
        "dm_recipient_platform_user_id": "wx_alice",
    }
    conn = _FakeConn(cur)

    sandbox_reads = 0
    backup_reads = 0

    async def _fail_live_sb(_ws_id: str) -> tuple[Any, str | None]:
        nonlocal sandbox_reads
        sandbox_reads += 1
        return object(), "/home/workspace/Research"

    async def _fail_versioned_read(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal sandbox_reads
        sandbox_reads += 1
        raise AssertionError("Must not read live sandbox when preflight is denied")

    async def _fail_backup_read(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal backup_reads
        backup_reads += 1
        raise AssertionError("Must not read offline backup when preflight is denied")

    with pytest.raises(HTTPException) as exc:
        await cd.download_verified_deliverable(
            delivery_key="deliverables:run-wx-1",
            delivery_attempt=1,
            workspace_id="ws-1",
            path="results/report.pdf",
            gateway_epoch=7,
            conn=conn,
            get_live_sandbox_fn=_fail_live_sb,
            versioned_reader_fn=_fail_versioned_read,
            offline_backup_reader_fn=_fail_backup_read,
        )
    assert exc.value.status_code == expected_status
    assert exc.value.detail["code"] == expected_code
    assert sandbox_reads == 0
    assert backup_reads == 0


class _LockOrderTrackingCursor:
    """In-memory cursor recording lock acquisition order for real I-1, I-6, and I-7 entrypoints."""

    def __init__(self) -> None:
        self.lock_events: list[str] = []
        self.topics: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.identities: dict[tuple[str, str], dict[str, Any]] = {}
        self.inbound_events: dict[tuple[str, str], dict[str, Any]] = {}
        self.session_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.deliveries: dict[str, dict[str, Any]] = {}
        self.chat_bindings: dict[tuple[str, str], dict[str, Any]] = {}
        self.user_settings: dict[str, dict[str, Any]] = {}
        self._one: dict[str, Any] | None = None
        self._all: list[dict[str, Any]] = []

    async def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> None:
        norm = " ".join(sql.split()).upper()
        self._one = None
        self._all = []

        if "PG_ADVISORY_XACT_LOCK" in norm:
            if params:
                self.lock_events.append(f"advisory:{params[0]}")
            return

        if "SELECT NOW() AS DB_NOW" in norm:
            self._one = {"db_now": datetime.now(timezone.utc)}
            return

        if "FROM DALTON_CHANNEL_SESSION_GENERATIONS" in norm and "FOR UPDATE" in norm:
            self.lock_events.append("for_update:dalton_channel_session_generations")
            plat, scope, uid = str(params[0]), str(params[1]), str(params[2])
            row = self.session_rows.get((plat, scope, uid))
            if row:
                self._one = dict(row)
            return

        if "INSERT INTO DALTON_CHANNEL_SESSION_GENERATIONS" in norm:
            plat, scope, uid = str(params[0]), str(params[1]), str(params[2])
            self.session_rows.setdefault(
                (plat, scope, uid),
                {
                    "platform": plat,
                    "scope_key": scope,
                    "dalton_user_id": uid,
                    "generation": 1,
                    "override_workspace_id": None,
                    "pending_interrupt": None,
                    "selection_initialized_at": datetime.now(timezone.utc),
                    "last_conversation_activity_at": datetime.now(timezone.utc),
                },
            )
            return

        if "UPDATE DALTON_CHANNEL_SESSION_GENERATIONS" in norm:
            return

        if "SELECT COALESCE(MAX(SENT_TO_CURRENT_COUNT), 0) AS CNT" in norm:
            rid = str(params[0])
            cnt = max(
                (int(r.get("sent_to_current_count") or 0) for r in self.inbound_events.values() if r.get("run_id") == rid),
                default=0,
            )
            self._one = {"cnt": cnt}
            return

        if "FROM DALTON_CHANNEL_INBOUND_EVENTS" in norm:
            if "WHERE PLATFORM = %S AND MESSAGE_ID = %S" in norm:
                plat, msg_id = str(params[0]), str(params[1])
                row = self.inbound_events.get((plat, msg_id))
                if row and (
                    "AND CHAT_ADDRESS = %S" not in norm
                    or row.get("chat_address") == str(params[2])
                ):
                    self._one = dict(row)
                return
            return

        if "INSERT INTO DALTON_CHANNEL_INBOUND_EVENTS" in norm:
            plat, msg_id = str(params[0]), str(params[1])
            key = (plat, msg_id)
            if key not in self.inbound_events:
                raw_p = params[6]
                raw_dict = dict(raw_p.obj) if hasattr(raw_p, "obj") else (dict(raw_p) if isinstance(raw_p, dict) else {})
                self.inbound_events[key] = {
                    "platform": plat,
                    "message_id": msg_id,
                    "request_key": params[2],
                    "chat_type": params[3],
                    "chat_address": params[4],
                    "thread_scope_key": params[5],
                    "raw_payload": raw_dict,
                    "status": "pending",
                    "dalton_user_id": None,
                    "workspace_id": None,
                    "external_thread_id": None,
                    "session_generation": 1,
                    "fallback_pending": False,
                    "sent_to_current_count": 0,
                    "uploaded_attachments": [],
                }
                self._one = dict(self.inbound_events[key])
            return

        if "UPDATE DALTON_CHANNEL_INBOUND_EVENTS" in norm:
            if "SET SENT_TO_CURRENT_COUNT = SENT_TO_CURRENT_COUNT + 1" in norm:
                rid = str(params[0])
                for row in self.inbound_events.values():
                    if row.get("run_id") == rid:
                        row["sent_to_current_count"] = int(row.get("sent_to_current_count") or 0) + 1
                        self._one = {"sent_to_current_count": row["sent_to_current_count"]}
                        return
                return
            if "WHERE PLATFORM = %S AND MESSAGE_ID = %S" in norm:
                plat, msg_id = str(params[-2]), str(params[-1])
                row = self.inbound_events.get((plat, msg_id))
                if row:
                    if "SET DALTON_USER_ID = %S, WORKSPACE_ID = %S" in norm:
                        row["dalton_user_id"] = params[0]
                        row["workspace_id"] = params[1]
                        row["external_thread_id"] = params[2]
                        row["session_generation"] = params[3]
                        raw_p = params[5]
                        row["raw_payload"] = dict(raw_p.obj) if hasattr(raw_p, "obj") else (dict(raw_p) if isinstance(raw_p, dict) else {})
                        row["thread_scope_key"] = params[6]
                    self._one = dict(row)
            return

        if "FROM DALTON_CHANNEL_IDENTITIES" in norm:
            if "WHERE PLATFORM = %S AND PLATFORM_USER_ID = %S" in norm:
                plat, puid = str(params[0]), str(params[1])
                for (u, p), row in self.identities.items():
                    if p == plat and row.get("platform_user_id") == puid:
                        self._one = {"user_id": u, **row}
                        return
                return
            uid, plat = str(params[0]), str(params[1])
            row = self.identities.get((uid, plat))
            if row:
                self._one = dict(row)
            return

        if "FROM DALTON_CHANNEL_BOT_TOPICS" in norm:
            where_part = norm.split("ORDER BY", 1)[0]
            require_confirmed = "STATUS = 'CONFIRMED'" in where_part
            if "(CANONICAL_SCOPE_KEY = %S OR ROOT_MESSAGE_ID = %S)" in norm:
                plat, addr, scope, root_id = params[:4]
                for r in self.topics.values():
                    if (
                        r["platform"] == plat
                        and r["chat_address"] == addr
                        and (r["canonical_scope_key"] == scope or r["root_message_id"] == root_id)
                        and (not require_confirmed or r.get("status") == "confirmed")
                    ):
                        self._one = dict(r)
                        return
                return
            if "EXTERNAL_TOPIC_ID = %S" in norm:
                plat, addr, ext_id = params
                for r in self.topics.values():
                    if (
                        r["platform"] == plat
                        and r["chat_address"] == addr
                        and r.get("external_topic_id") == ext_id
                        and (not require_confirmed or r.get("status") == "confirmed")
                    ):
                        self._one = dict(r)
                        return
                return
            if "ROOT_MESSAGE_ID = %S" in norm:
                plat, addr, root_id = params
                for r in self.topics.values():
                    if (
                        r["platform"] == plat
                        and r["chat_address"] == addr
                        and r.get("root_message_id") == root_id
                        and (not require_confirmed or r.get("status") == "confirmed")
                    ):
                        self._one = dict(r)
                        return
                return
            if "CANONICAL_SCOPE_KEY = %S" in norm:
                plat, addr, scope = params
                r = self.topics.get((plat, addr, scope))
                if r and (not require_confirmed or r.get("status") == "confirmed"):
                    self._one = dict(r)
                return
            return

        if "UPDATE DALTON_CHANNEL_BOT_TOPICS" in norm:
            if "SET EXTERNAL_TOPIC_ID = COALESCE" in norm:
                ext_id, root_id, first_rep, plat, addr, scope = params
                r = self.topics.get((plat, addr, scope))
                if r:
                    r["external_topic_id"] = r.get("external_topic_id") or ext_id
                    r["root_message_id"] = r.get("root_message_id") or root_id
                    r["first_reply_message_id"] = r.get("first_reply_message_id") or first_rep
                    r["status"] = "confirmed"
                    self._one = dict(r)
            return

        if "SELECT EXISTS (" in norm and "FROM DALTON_CHANNEL_DELIVERIES" in norm:
            run_id = str(params[0])
            exclude_deliv = "NOT LIKE 'DELIVERABLES:" in norm
            want_status = "in_flight" if "STATUS = 'IN_FLIGHT'" in norm else "unknown"
            matched = any(
                d.get("run_id") == run_id
                and bool(d.get("is_current"))
                and bool(d.get("has_non_empty_text"))
                and str(d.get("status")) == want_status
                and (
                    not exclude_deliv
                    or not str(d.get("delivery_key") or "").startswith(
                        ("deliverables:", "deliverables-links:")
                    )
                )
                for d in self.deliveries.values()
            )
            if "AS IN_FLIGHT" in norm:
                self._one = {"in_flight": matched}
            else:
                self._one = {"has_unknown_text": matched}
            return

        if "INSERT INTO DALTON_CHANNEL_DELIVERIES" in norm:
            dkey = str(params[0])
            self.deliveries[dkey] = {
                "delivery_key": dkey,
                "kind": params[1],
                "user_id": params[2],
                "thread_id": params[3],
                "run_id": params[4],
                "execution_id": params[5],
                "is_current": bool(params[6]),
                "has_non_empty_text": bool(params[7]),
                "platform": params[8],
                "target_address": params[9],
                "status": "in_flight",
                "total_chunks": params[10],
                "sent_chunks": 0,
                "platform_message_ids": [],
                "files_status": [],
                "source_scope_key": params[11],
                "source_generation": params[12],
                "activity_origin": params[13],
                "delivery_attempt": 1,
            }
            return

        if "FROM DALTON_CHANNEL_DELIVERIES" in norm and "WHERE DELIVERY_KEY = %S" in norm:
            if "FOR UPDATE" in norm:
                self.lock_events.append("for_update:dalton_channel_deliveries")
            row = self.deliveries.get(str(params[0]))
            if row:
                self._one = dict(row)
            return

        if "UPDATE DALTON_CHANNEL_DELIVERIES" in norm:
            if params:
                dkey = str(params[-1])
                row = self.deliveries.get(dkey)
                if row and "SET STATUS = %S" in norm:
                    row["status"] = params[0]
            return

        if "FROM DALTON_CHANNEL_CHAT_BINDINGS" in norm:
            plat, addr = str(params[0]), str(params[1])
            row = self.chat_bindings.get((plat, addr))
            if row:
                self._one = dict(row)
            return

        if "FROM DALTON_CHANNEL_USER_SETTINGS" in norm:
            uid = str(params[0])
            row = self.user_settings.get(uid)
            if row:
                self._one = dict(row)
            return

        if "FROM WORKSPACES" in norm:
            self._one = {"workspace_id": str(params[0]), "user_id": "user-alice", "status": "running"}
            return

    async def fetchone(self) -> dict[str, Any] | None:
        return self._one

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._all)


@pytest.mark.asyncio
async def test_i1_and_i7_lock_order_and_real_i1_inbound_before_i7(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 7: Real I-1 (`register_and_resolve_inbound`) and real I-7 (`_update_delivery_progress_once`)
    both acquire `dalton_bot_topic` advisory lock strictly BEFORE `dalton_channel_session_generations`
    `FOR UPDATE`, and real I-1 handles both other-member @bot and initiator concurrent follow-up
    before I-7 confirms."""
    monkeypatch.setattr(
        cg_db,
        "require_channel_platform_capability",
        create_autospec(cg_db.require_channel_platform_capability, return_value=None),
    )
    monkeypatch.setattr(
        wx_db,
        "verify_gateway_epoch_in_tx",
        create_autospec(wx_db.verify_gateway_epoch_in_tx, return_value=None),
    )
    monkeypatch.setattr(
        cg_db,
        "get_user_tier",
        create_autospec(cg_db.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        cg_db,
        "get_home_workspace_id",
        create_autospec(cg_db.get_home_workspace_id, return_value="ws-home"),
    )

    cur = _LockOrderTrackingCursor()
    cur.identities[("user-alice", "feishu")] = {"platform_user_id": "ou_alice"}
    cur.identities[("user-bob", "feishu")] = {"platform_user_id": "ou_bob"}
    # Pre-reserved pending topic from I-6 (Alice is initiator)
    cur.topics[("feishu", "feishu:oc_grp_1", "chat:oc_grp_1:topic:om_root_1")] = {
        "platform": "feishu",
        "chat_address": "feishu:oc_grp_1",
        "canonical_scope_key": "chat:oc_grp_1:topic:om_root_1",
        "external_topic_id": None,
        "root_message_id": "om_root_1",
        "first_reply_message_id": None,
        "initiator_dalton_user_id": "user-alice",
        "initiator_platform_user_id": "ou_alice",
        "status": "pending",
    }
    cur.session_rows[("feishu", "chat:oc_grp_1:topic:om_root_1", "user-alice")] = {
        "platform": "feishu",
        "scope_key": "chat:oc_grp_1:topic:om_root_1",
        "dalton_user_id": "user-alice",
        "generation": 1,
        "override_workspace_id": None,
        "pending_interrupt": None,
        "selection_initialized_at": datetime.now(timezone.utc),
        "last_conversation_activity_at": datetime.now(timezone.utc),
    }
    cur.deliveries["fallback:run-topic-1"] = {
        "delivery_key": "fallback:run-topic-1",
        "kind": "fallback",
        "user_id": "user-alice",
        "platform": "feishu",
        "target_address": "feishu:oc_grp_1",
        "source_scope_key": "chat:oc_grp_1:topic:om_root_1",
        "source_generation": 1,
        "activity_origin": "conversation",
        "status": "in_flight",
        "sent_chunks": 0,
        "total_chunks": 1,
        "platform_message_ids": [],
        "files_status": [],
        "delivery_attempt": 1,
        "is_current": True,
        "has_non_empty_text": True,
        "run_id": "run-topic-1",
    }
    conn = _FakeConn(cur)

    # 1) Real I-1: Bob `@bot` arrives inside the topic BEFORE I-7 confirms
    cur.lock_events.clear()
    bob_res = await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id="ou_bob",
        chat_type="group",
        chat_address="feishu:oc_grp_1",
        thread_scope_key="chat:oc_grp_1:thread:omt_topic_1",
        message_id="om_bob_in_1",
        request_key="rk-bob-1",
        raw_payload={"platform_user_id": "ou_bob"},
        external_topic_id="omt_topic_1",
        root_message_id="om_root_1",
        conn=conn,
    )
    assert bob_res["allowed"] is True
    assert bob_res["resolved_scope_key"] == "chat:oc_grp_1:thread:omt_topic_1"
    assert bob_res["topic_initiator_platform_user_id"] == "ou_alice"
    i1_bob_topic_idx = cur.lock_events.index("advisory:dalton_bot_topic:feishu:feishu:oc_grp_1")
    i1_bob_sess_idx = cur.lock_events.index("for_update:dalton_channel_session_generations")
    assert i1_bob_topic_idx < i1_bob_sess_idx

    # 2) Real I-1: Alice (initiator) immediately `@bot` follows up before I-7 confirms
    cur.lock_events.clear()
    alice_res = await cg_db.register_and_resolve_inbound(
        platform="feishu",
        platform_user_id="ou_alice",
        chat_type="group",
        chat_address="feishu:oc_grp_1",
        thread_scope_key="chat:oc_grp_1:thread:omt_topic_1",
        message_id="om_alice_in_2",
        request_key="rk-alice-2",
        raw_payload={"platform_user_id": "ou_alice"},
        external_topic_id="omt_topic_1",
        root_message_id="om_root_1",
        conn=conn,
    )
    assert alice_res["allowed"] is True
    assert alice_res["resolved_scope_key"] == "chat:oc_grp_1:topic:om_root_1"
    i1_alice_topic_idx = cur.lock_events.index("advisory:dalton_bot_topic:feishu:feishu:oc_grp_1")
    i1_alice_sess_idx = cur.lock_events.index("for_update:dalton_channel_session_generations")
    assert i1_alice_topic_idx < i1_alice_sess_idx

    # 3) Real I-7: confirms bot_topic_registration with identical lock order (`bot_topic` -> `session_generations`)
    cur.lock_events.clear()
    i7_res = await cg_db._update_delivery_progress_once(
        delivery_key="fallback:run-topic-1",
        sent_chunks=1,
        total_chunks=1,
        platform_message_ids=["om_reply_1"],
        status="sent",
        delivery_attempt=1,
        bot_topic_registration={
            "platform": "feishu",
            "chat_address": "feishu:oc_grp_1",
            "canonical_scope_key": "chat:oc_grp_1:topic:om_root_1",
            "external_topic_id": "omt_topic_1",
            "root_message_id": "om_root_1",
            "first_reply_message_id": "om_reply_1",
            "initiator_platform_user_id": "ou_alice",
            "initiator_dalton_user_id": "user-alice",
        },
        conn=conn,
    )
    assert i7_res["ok"] is True
    assert i7_res["bot_topic_registered"] is True
    i7_topic_idx = cur.lock_events.index("advisory:dalton_bot_topic:feishu:feishu:oc_grp_1")
    i7_sess_idx = cur.lock_events.index("for_update:dalton_channel_session_generations")
    assert i7_topic_idx < i7_sess_idx

    # 4) Real I-1 + I-6 + I-7 on Discord (pre-confirmation Bob @bot + initiator follow-up + I-7 confirm)
    cur.identities[("user-alice", "discord")] = {"platform_user_id": "100001"}
    cur.identities[("user-bob", "discord")] = {"platform_user_id": "200002"}
    cur.chat_bindings[("discord", "discord:111111/222222")] = {
        "chat_name": "research",
        "workspace_id": None,
    }
    cur.user_settings["user-bob"] = {
        "apps": {"discord": {"chats": {"discord:111111/222222": {"name": "research"}}}}
    }
    cur.topics[("discord", "discord:111111/222222", "chat:111111/222222:topic:999000111")] = {
        "platform": "discord",
        "chat_address": "discord:111111/222222",
        "canonical_scope_key": "chat:111111/222222:topic:999000111",
        "external_topic_id": None,
        "root_message_id": "999000111",
        "first_reply_message_id": None,
        "initiator_dalton_user_id": "user-alice",
        "initiator_platform_user_id": "100001",
        "status": "pending",
    }
    cur.session_rows[("discord", "chat:111111/222222:topic:999000111", "user-alice")] = {
        "platform": "discord",
        "scope_key": "chat:111111/222222:topic:999000111",
        "dalton_user_id": "user-alice",
        "generation": 1,
        "override_workspace_id": None,
        "pending_interrupt": None,
        "selection_initialized_at": datetime.now(timezone.utc),
        "last_conversation_activity_at": datetime.now(timezone.utc),
    }
    cur.deliveries["fallback:run-disc-topic-1"] = {
        "delivery_key": "fallback:run-disc-topic-1",
        "kind": "fallback",
        "user_id": "user-alice",
        "platform": "discord",
        "target_address": "discord:111111/222222",
        "source_scope_key": "chat:111111/222222:topic:999000111",
        "source_generation": 1,
        "activity_origin": "conversation",
        "status": "in_flight",
        "sent_chunks": 0,
        "total_chunks": 1,
        "platform_message_ids": [],
        "files_status": [],
        "delivery_attempt": 1,
        "is_current": True,
        "has_non_empty_text": True,
        "run_id": "run-disc-topic-1",
    }

    cur.lock_events.clear()
    disc_bob = await cg_db.register_and_resolve_inbound(
        platform="discord",
        platform_user_id="200002",
        chat_type="group",
        chat_address="discord:111111/222222",
        thread_scope_key="chat:111111/222222:thread:999000111",
        message_id="disc_bob_in_1",
        request_key="rk-disc-bob-1",
        raw_payload={"platform_user_id": "200002"},
        external_topic_id="999000111",
        root_message_id="999000111",
        conn=conn,
    )
    assert disc_bob["allowed"] is True
    assert disc_bob["resolved_scope_key"] == "chat:111111/222222:thread:999000111"
    assert disc_bob["topic_initiator_platform_user_id"] == "100001"
    assert cur.lock_events.index("advisory:dalton_bot_topic:discord:discord:111111/222222") < cur.lock_events.index(
        "for_update:dalton_channel_session_generations"
    )

    # Bob's Feishu & Discord pre-confirmation I-6 claims MUST both return the verified inbound external_topic_id
    real_find_src = cg_db._find_current_conversation_source
    bob_feishu_row = {
        **cur.inbound_events[("feishu", "om_bob_in_1")],
        "_source_table": "inbound_events",
        "target_address": "feishu:oc_grp_1",
        "thread_id": "44444444-4444-4444-8444-444444444444",
        "run_id": "run-bob-pre-i7-fs",
    }
    monkeypatch.setattr(
        cg_db,
        "_find_current_conversation_source",
        create_autospec(real_find_src, return_value=bob_feishu_row),
    )
    # Reset Feishu topic to pending to verify pre-confirmation I-6 fallback to verified inbound topic id
    cur.topics[("feishu", "feishu:oc_grp_1", "chat:oc_grp_1:topic:om_root_1")]["status"] = "pending"
    cur.topics[("feishu", "feishu:oc_grp_1", "chat:oc_grp_1:topic:om_root_1")]["external_topic_id"] = None
    bob_fs_claim = await cg_db.authorize_and_claim_delivery(
        delivery_key="fallback:run-bob-pre-i7-fs",
        kind="fallback",
        user_id="user-bob",
        thread_id="44444444-4444-4444-8444-444444444444",
        run_id="run-bob-pre-i7-fs",
        platform="feishu",
        target=None,
        has_non_empty_text=True,
        total_chunks=1,
        source_scope_key="chat:oc_grp_1:thread:omt_topic_1",
        source_generation=1,
        activity_origin="conversation",
        conn=conn,
    )
    assert bob_fs_claim["claim_status"] == "claimed"
    assert bob_fs_claim["external_topic_id"] == "omt_topic_1"
    assert bob_fs_claim["inbound_message_id"] == "om_bob_in_1"
    assert bob_fs_claim["root_message_id"] == "om_root_1"

    bob_disc_row = {
        **cur.inbound_events[("discord", "disc_bob_in_1")],
        "_source_table": "inbound_events",
        "target_address": "discord:111111/222222",
        "thread_id": "55555555-5555-4555-8555-555555555555",
        "run_id": "run-bob-pre-i7-dc",
    }
    monkeypatch.setattr(
        cg_db,
        "_find_current_conversation_source",
        create_autospec(real_find_src, return_value=bob_disc_row),
    )
    bob_dc_claim = await cg_db.authorize_and_claim_delivery(
        delivery_key="fallback:run-bob-pre-i7-dc",
        kind="fallback",
        user_id="user-bob",
        thread_id="55555555-5555-4555-8555-555555555555",
        run_id="run-bob-pre-i7-dc",
        platform="discord",
        target=None,
        has_non_empty_text=True,
        total_chunks=1,
        source_scope_key="chat:111111/222222:thread:999000111",
        source_generation=1,
        activity_origin="conversation",
        conn=conn,
    )
    assert bob_dc_claim["claim_status"] == "claimed"
    assert bob_dc_claim["external_topic_id"] == "999000111"

    # Real Discord I-7 confirmation preserves lock order
    cur.lock_events.clear()
    disc_i7 = await cg_db._update_delivery_progress_once(
        delivery_key="fallback:run-disc-topic-1",
        sent_chunks=1,
        total_chunks=1,
        platform_message_ids=["disc_reply_1"],
        status="sent",
        delivery_attempt=1,
        bot_topic_registration={
            "platform": "discord",
            "chat_address": "discord:111111/222222",
            "canonical_scope_key": "chat:111111/222222:topic:999000111",
            "external_topic_id": "999000111",
            "root_message_id": "999000111",
            "first_reply_message_id": "disc_reply_1",
            "initiator_platform_user_id": "100001",
            "initiator_dalton_user_id": "user-alice",
        },
        conn=conn,
    )
    assert disc_i7["ok"] is True
    assert disc_i7["bot_topic_registered"] is True
    assert cur.lock_events.index("advisory:dalton_bot_topic:discord:discord:111111/222222") < cur.lock_events.index(
        "for_update:dalton_channel_session_generations"
    )


@pytest.mark.asyncio
async def test_discord_current_session_registered_thread_denies_after_parent_binding_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 8a: Current-session Discord delivery to a registered thread (both replay and background
    report-back) calls `_verify_group_target_deliverable` on the parent channel and denies after
    parent binding or user channel settings are removed."""
    monkeypatch.setattr(
        cg_db,
        "require_channel_platform_capability",
        create_autospec(cg_db.require_channel_platform_capability, return_value=None),
    )
    monkeypatch.setattr(
        wx_db,
        "verify_gateway_epoch_in_tx",
        create_autospec(wx_db.verify_gateway_epoch_in_tx, return_value=None),
    )
    monkeypatch.setattr(
        cg_db,
        "get_user_tier",
        create_autospec(cg_db.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        cg_db,
        "get_home_workspace_id",
        create_autospec(cg_db.get_home_workspace_id, return_value="ws-home"),
    )
    monkeypatch.setattr(
        cg_db,
        "_verify_workspace_write_permission",
        create_autospec(
            cg_db._verify_workspace_write_permission,
            return_value=(True, None, {"workspace_id": "ws-home", "user_id": "user-alice"}),
        ),
    )

    cur = _LockOrderTrackingCursor()
    cur.identities[("user-alice", "discord")] = {
        "platform_user_id": "100001",
        "weixin_context_token": None,
    }
    cur.topics[("discord", "discord:111111/222222", "chat:111111/222222:topic:999000111")] = {
        "platform": "discord",
        "chat_address": "discord:111111/222222",
        "canonical_scope_key": "chat:111111/222222:topic:999000111",
        "external_topic_id": "999000111",
        "root_message_id": "999000111",
        "first_reply_message_id": "999000222",
        "initiator_dalton_user_id": "user-alice",
        "initiator_platform_user_id": "100001",
        "status": "confirmed",
    }
    cur.chat_bindings[("discord", "discord:111111/222222")] = {
        "chat_name": "research",
        "workspace_id": None,
    }
    cur.user_settings["user-alice"] = {
        "apps": {"discord": {"chats": {"discord:111111/222222": {"name": "research"}}}}
    }
    conn = _FakeConn(cur)

    real_find_current_source = cg_db._find_current_conversation_source

    for source_kind, source_table in [("replay", "inbound_events"), ("bg", "background_runs")]:
        src_row = {
            "_source_table": source_table,
            "platform": "discord",
            "dalton_user_id": "user-alice",
            "target_address": "discord:111111/222222",
            "thread_scope_key": "chat:111111/222222:topic:999000111",
            "thread_id": "33333333-3333-4333-8333-333333333333",
            "run_id": f"run-disc-{source_kind}",
            "workspace_id": "ws-home",
            "session_generation": 1,
            "message_id": "999000111" if source_table == "inbound_events" else None,
        }
        monkeypatch.setattr(
            cg_db,
            "_find_current_conversation_source",
            create_autospec(real_find_current_source, return_value=src_row),
        )

        # Bound parent channel -> allowed and returns external_topic_id
        allowed = await cg_db.authorize_and_claim_delivery(
            delivery_key=f"fallback:ok-{source_kind}",
            kind="fallback",
            user_id="user-alice",
            thread_id="33333333-3333-4333-8333-333333333333",
            run_id=f"run-disc-{source_kind}",
            platform="discord",
            target=None,
            has_non_empty_text=True,
            total_chunks=1,
            source_scope_key="chat:111111/222222:topic:999000111",
            source_generation=1,
            activity_origin="replay" if source_kind == "replay" else "notification",
            conn=conn,
        )
        assert allowed["claim_status"] == "claimed"
        assert allowed["external_topic_id"] == "999000111"

        # Remove parent channel binding -> denied with not_allowed
        saved_binding = cur.chat_bindings.pop(("discord", "discord:111111/222222"))
        denied_unbind = await cg_db.authorize_and_claim_delivery(
            delivery_key=f"fallback:unbind-{source_kind}",
            kind="fallback",
            user_id="user-alice",
            thread_id="33333333-3333-4333-8333-333333333333",
            run_id=f"run-disc-{source_kind}",
            platform="discord",
            target=None,
            has_non_empty_text=True,
            total_chunks=1,
            source_scope_key="chat:111111/222222:topic:999000111",
            source_generation=1,
            activity_origin="replay" if source_kind == "replay" else "notification",
            conn=conn,
        )
        assert denied_unbind["claim_status"] == "denied"
        assert denied_unbind["code"] == "not_allowed"
        cur.chat_bindings[("discord", "discord:111111/222222")] = saved_binding

        # Remove user channel setting -> denied with not_allowed
        cur.user_settings["user-alice"] = {"apps": {"discord": {"chats": {}}}}
        denied_setting = await cg_db.authorize_and_claim_delivery(
            delivery_key=f"fallback:setting-{source_kind}",
            kind="fallback",
            user_id="user-alice",
            thread_id="33333333-3333-4333-8333-333333333333",
            run_id=f"run-disc-{source_kind}",
            platform="discord",
            target=None,
            has_non_empty_text=True,
            total_chunks=1,
            source_scope_key="chat:111111/222222:topic:999000111",
            source_generation=1,
            activity_origin="replay" if source_kind == "replay" else "notification",
            conn=conn,
        )
        assert denied_setting["claim_status"] == "denied"
        assert denied_setting["code"] == "not_allowed"
        cur.user_settings["user-alice"] = {
            "apps": {"discord": {"chats": {"discord:111111/222222": {"name": "research"}}}}
        }


def test_rewrite_preserves_loopback_http_and_rejects_host_absolute_paths_in_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 8d: Non-workspace HTTP(S) links (including loopback URLs) are preserved, and
    arbitrary host absolute paths like `/etc/ssl/openssl.cnf` are NOT rewritten into workspace
    deep links before automation report validation so `_scan_string` still rejects them."""
    text = (
        "参考 [本地文档](http://127.0.0.1:8080/health) 与 "
        "[开发页](http://localhost:3000/docs) 及 [研究报告](/home/workspace/results/report.md)。"
    )
    rewritten = notify_dispatch.rewrite_workspace_links_for_external(
        text,
        public_base="https://dalton.example.com",
        thread_id="tid-link-1",
    )
    assert "[本地文档](http://127.0.0.1:8080/health)" in rewritten
    assert "[开发页](http://localhost:3000/docs)" in rewritten
    assert (
        "[研究报告](https://dalton.example.com/chat/t/tid-link-1?file=%2Fhome%2Fworkspace%2Fresults%2Freport.md)"
        in rewritten
    )

    # Host absolute path MUST NOT be classified as workspace file
    assert (
        notify_dispatch.classify_external_workspace_destination("/etc/ssl/openssl.cnf")[
            "kind"
        ]
        == "keep"
    )
    assert (
        notify_dispatch.classify_external_workspace_destination("file:///etc/ssl/openssl.cnf")[
            "kind"
        ]
        == "keep"
    )

    # Automation report containing `/etc/ssl/openssl.cnf` MUST still be rejected by validation
    monkeypatch.setattr(
        "src.dalton.settings.get_dalton_public_app_url",
        lambda: "https://dalton.example.com",
    )
    bad_card = {
        "schema": "2.0",
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "读取 [系统配置](/etc/ssl/openssl.cnf)",
                }
            ]
        },
    }
    rw_cards, _, _ = auto_reports_db._rewrite_automation_report_inputs(
        cards=[bad_card],
        failure_text=None,
        text=None,
        thread_id="tid-rep-2",
        workspace_id="ws-rep-2",
    )
    assert "/etc/ssl/openssl.cnf" in rw_cards[0]["body"]["elements"][0]["content"]
    prepared, err = auto_reports_db.prepare_automation_report_content(
        profile="market-digest-am",
        cards=rw_cards,
        failure_text=None,
        text=None,
    )
    assert prepared is None
    assert err == "invalid_card"

    # R8 #9: Percent-encoded host absolute path (`%2Fetc%2Fssl%2Fopenssl.cnf`) MUST be decoded
    # BEFORE checking leading `/` so report button URLs cannot bypass validation.
    assert (
        notify_dispatch.classify_external_workspace_destination("%2Fetc%2Fssl%2Fopenssl.cnf")[
            "kind"
        ]
        == "keep"
    )
    assert (
        notify_dispatch.classify_external_workspace_destination("%2fetc%2fssl%2fopenssl.cnf")[
            "kind"
        ]
        == "keep"
    )
    encoded_button_card = {
        "schema": "2.0",
        "body": {
            "elements": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "打开配置"},
                    "behaviors": [
                        {
                            "type": "open_url",
                            "default_url": "%2Fetc%2Fssl%2Fopenssl.cnf",
                        }
                    ],
                }
            ]
        },
    }
    rw_btn_cards, _, _ = auto_reports_db._rewrite_automation_report_inputs(
        cards=[encoded_button_card],
        failure_text=None,
        text=None,
        thread_id="tid-rep-3",
        workspace_id="ws-rep-3",
    )
    assert (
        rw_btn_cards[0]["body"]["elements"][0]["behaviors"][0]["default_url"]
        == "%2Fetc%2Fssl%2Fopenssl.cnf"
    )
    btn_prepared, btn_err = auto_reports_db.prepare_automation_report_content(
        profile="market-digest-am",
        cards=rw_btn_cards,
        failure_text=None,
        text=None,
    )
    assert btn_prepared is None
    assert btn_err == "invalid_card"


@pytest.mark.asyncio
async def test_discord_parent_recheck_covers_legacy_thread_and_unconfirmed_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8 #2: Discord parent channel binding & user channel settings are re-verified for ALL
    current-session subthread deliveries, including:
    1. Pre-upgrade `:thread:` sessions with NO `dalton_channel_bot_topics` row (`inbound_events` and `background_runs`).
    2. Pre-confirmation `:thread:` / `:topic:` sessions where the topic row is still `pending`.
    """
    monkeypatch.setattr(
        cg_db,
        "require_channel_platform_capability",
        create_autospec(cg_db.require_channel_platform_capability, return_value=None),
    )
    monkeypatch.setattr(
        wx_db,
        "verify_gateway_epoch_in_tx",
        create_autospec(wx_db.verify_gateway_epoch_in_tx, return_value=None),
    )
    monkeypatch.setattr(
        cg_db,
        "get_user_tier",
        create_autospec(cg_db.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        cg_db,
        "get_home_workspace_id",
        create_autospec(cg_db.get_home_workspace_id, return_value="ws-home"),
    )
    monkeypatch.setattr(
        cg_db,
        "_verify_workspace_write_permission",
        create_autospec(
            cg_db._verify_workspace_write_permission,
            return_value=(True, None, {"workspace_id": "ws-home", "user_id": "user-alice"}),
        ),
    )

    real_find_src = cg_db._find_current_conversation_source

    scenarios = [
        (
            "legacy_thread_bg_no_topic_row",
            "background_runs",
            "chat:111111/222222:thread:555666777",
            None,
        ),
        (
            "legacy_thread_inbound_no_topic_row",
            "inbound_events",
            "chat:111111/222222:thread:555666777",
            None,
        ),
        (
            "pending_topic_before_confirmation",
            "inbound_events",
            "chat:111111/222222:topic:999000888",
            "pending",
        ),
        (
            "pre_confirmation_other_member_thread",
            "inbound_events",
            "chat:111111/222222:thread:999000888",
            "pending",
        ),
    ]

    for label, source_table, scope_key, topic_status in scenarios:
        cur = _LockOrderTrackingCursor()
        cur.identities[("user-alice", "discord")] = {
            "platform_user_id": "100001",
            "weixin_context_token": None,
        }
        if topic_status is not None:
            cur.topics[("discord", "discord:111111/222222", "chat:111111/222222:topic:999000888")] = {
                "platform": "discord",
                "chat_address": "discord:111111/222222",
                "canonical_scope_key": "chat:111111/222222:topic:999000888",
                "external_topic_id": None,
                "root_message_id": "999000888",
                "first_reply_message_id": None,
                "initiator_dalton_user_id": "user-alice",
                "initiator_platform_user_id": "100001",
                "status": topic_status,
            }
        cur.chat_bindings[("discord", "discord:111111/222222")] = {
            "chat_name": "research",
            "workspace_id": None,
        }
        cur.user_settings["user-alice"] = {
            "apps": {"discord": {"chats": {"discord:111111/222222": {"name": "research"}}}}
        }
        conn = _FakeConn(cur)

        src_row = {
            "_source_table": source_table,
            "platform": "discord",
            "dalton_user_id": "user-alice",
            "target_address": "discord:111111/222222",
            "thread_scope_key": scope_key,
            "thread_id": "66666666-6666-4666-8666-666666666666",
            "run_id": f"run-{label}",
            "workspace_id": "ws-home",
            "session_generation": 1,
            "message_id": "msg-in-1" if source_table == "inbound_events" else None,
            "raw_payload": {"platform_user_id": "100001"},
        }
        monkeypatch.setattr(
            cg_db,
            "_find_current_conversation_source",
            create_autospec(real_find_src, return_value=src_row),
        )

        # 1) Bound parent channel -> allowed
        ok_res = await cg_db.authorize_and_claim_delivery(
            delivery_key=f"fallback:ok-{label}",
            kind="fallback",
            user_id="user-alice",
            thread_id="66666666-6666-4666-8666-666666666666",
            run_id=f"run-{label}",
            platform="discord",
            target=None,
            has_non_empty_text=True,
            total_chunks=1,
            source_scope_key=scope_key,
            source_generation=1,
            activity_origin="replay" if source_table == "inbound_events" else "notification",
            conn=conn,
        )
        assert ok_res["claim_status"] == "claimed", label

        # 2) Revoke parent channel binding -> denied
        saved_binding = cur.chat_bindings.pop(("discord", "discord:111111/222222"))
        denied_unbind = await cg_db.authorize_and_claim_delivery(
            delivery_key=f"fallback:unbind-{label}",
            kind="fallback",
            user_id="user-alice",
            thread_id="66666666-6666-4666-8666-666666666666",
            run_id=f"run-{label}",
            platform="discord",
            target=None,
            has_non_empty_text=True,
            total_chunks=1,
            source_scope_key=scope_key,
            source_generation=1,
            activity_origin="replay" if source_table == "inbound_events" else "notification",
            conn=conn,
        )
        assert denied_unbind["claim_status"] == "denied", label
        assert denied_unbind["code"] == "not_allowed", label
        cur.chat_bindings[("discord", "discord:111111/222222")] = saved_binding

        # 3) Revoke user channel setting -> denied
        cur.user_settings["user-alice"] = {"apps": {"discord": {"chats": {}}}}
        denied_setting = await cg_db.authorize_and_claim_delivery(
            delivery_key=f"fallback:setting-{label}",
            kind="fallback",
            user_id="user-alice",
            thread_id="66666666-6666-4666-8666-666666666666",
            run_id=f"run-{label}",
            platform="discord",
            target=None,
            has_non_empty_text=True,
            total_chunks=1,
            source_scope_key=scope_key,
            source_generation=1,
            activity_origin="replay" if source_table == "inbound_events" else "notification",
            conn=conn,
        )
        assert denied_setting["claim_status"] == "denied", label
        assert denied_setting["code"] == "not_allowed", label


@pytest.mark.asyncio
async def test_deliverables_links_excluded_from_body_dedupe_and_sent_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8 #5: `deliverables:<run_id>` and `deliverables-links:<run_id>` carrying overflow link
    lines are excluded from body-text dedupe (`has_non_empty_text`, `has_in_flight_or_unknown_current_text`,
    fallback guard, and `sent_to_current_count`)."""
    monkeypatch.setattr(
        cg_db,
        "require_channel_platform_capability",
        create_autospec(cg_db.require_channel_platform_capability, return_value=None),
    )
    monkeypatch.setattr(
        wx_db,
        "verify_gateway_epoch_in_tx",
        create_autospec(wx_db.verify_gateway_epoch_in_tx, return_value=None),
    )
    monkeypatch.setattr(
        cg_db,
        "get_user_tier",
        create_autospec(cg_db.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        cg_db,
        "get_home_workspace_id",
        create_autospec(cg_db.get_home_workspace_id, return_value="ws-home"),
    )

    cur = _LockOrderTrackingCursor()
    cur.identities[("user-alice", "feishu")] = {
        "platform_user_id": "ou_alice",
        "weixin_context_token": None,
    }
    cur.inbound_events[("feishu", "om_dm_6files")] = {
        "platform": "feishu",
        "message_id": "om_dm_6files",
        "dalton_user_id": "user-alice",
        "chat_type": "dm",
        "chat_address": "feishu:@me",
        "thread_scope_key": "dm:ou_alice",
        "thread_id": "77777777-7777-4777-8777-777777777777",
        "run_id": "run-6files",
        "workspace_id": "ws-home",
        "session_generation": 1,
        "raw_payload": {"platform_user_id": "ou_alice"},
        "sent_to_current_count": 0,
    }
    conn = _FakeConn(cur)

    real_find_src = cg_db._find_current_conversation_source
    src_row = {
        **cur.inbound_events[("feishu", "om_dm_6files")],
        "_source_table": "inbound_events",
        "target_address": "feishu:@me",
    }
    monkeypatch.setattr(
        cg_db,
        "_find_current_conversation_source",
        create_autospec(real_find_src, return_value=src_row),
    )

    # 1) Claim `deliverables:run-6files` even if caller sent has_non_empty_text=True -> stored FALSE!
    deliv_claim = await cg_db.authorize_and_claim_delivery(
        delivery_key="deliverables:run-6files",
        kind="fallback",
        user_id="user-alice",
        thread_id="77777777-7777-4777-8777-777777777777",
        run_id="run-6files",
        platform="feishu",
        target="feishu:@me",
        has_non_empty_text=True,
        total_chunks=1,
        source_scope_key="dm:ou_alice",
        source_generation=1,
        activity_origin="conversation",
        conn=conn,
    )
    assert deliv_claim["claim_status"] == "claimed"
    assert cur.deliveries["deliverables:run-6files"]["has_non_empty_text"] is False

    # 2) Simulate legacy/crashed `deliverables:run-6files` row left in `unknown` even with has_non_empty_text=True
    cur.deliveries["deliverables:run-6files"]["status"] = "unknown"
    cur.deliveries["deliverables:run-6files"]["has_non_empty_text"] = True

    prog = await cg_db.update_inbound_progress(
        platform="feishu",
        message_id="om_dm_6files",
        run_id="run-6files",
        conn=conn,
    )
    assert prog["has_unknown_current_text"] is False
    assert prog["has_in_flight_or_unknown_current_text"] is False

    # 3) Body `fallback:run-6files` is NOT blocked by unknown `deliverables:run-6files`
    fb_claim = await cg_db.authorize_and_claim_delivery(
        delivery_key="fallback:run-6files",
        kind="fallback",
        user_id="user-alice",
        thread_id="77777777-7777-4777-8777-777777777777",
        run_id="run-6files",
        platform="feishu",
        target="feishu:@me",
        has_non_empty_text=True,
        total_chunks=1,
        source_scope_key="dm:ou_alice",
        source_generation=1,
        activity_origin="replay",
        conn=conn,
    )
    assert fb_claim["claim_status"] == "claimed"

    # 4) Settling `deliverables-links:run-6files` as `sent` MUST NOT increment `sent_to_current_count`
    cur.deliveries["deliverables-links:run-6files"] = {
        "delivery_key": "deliverables-links:run-6files",
        "kind": "fallback",
        "user_id": "user-alice",
        "platform": "feishu",
        "target_address": "feishu:@me",
        "source_scope_key": "dm:ou_alice",
        "source_generation": 1,
        "activity_origin": "replay",
        "status": "in_flight",
        "sent_chunks": 0,
        "total_chunks": 1,
        "platform_message_ids": [],
        "files_status": [],
        "delivery_attempt": 1,
        "is_current": True,
        "has_non_empty_text": True,
        "run_id": "run-6files",
    }
    i7_links = await cg_db._update_delivery_progress_once(
        delivery_key="deliverables-links:run-6files",
        sent_chunks=1,
        total_chunks=1,
        platform_message_ids=["om_links_1"],
        status="sent",
        delivery_attempt=1,
        conn=conn,
    )
    assert i7_links["ok"] is True
    assert cur.inbound_events[("feishu", "om_dm_6files")]["sent_to_current_count"] == 0


@pytest.mark.asyncio
async def test_r10_fallback_replay_on_in_flight_or_unknown_stays_denied_while_deliverables_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PG regression #1: non-deliverable `fallback:*` rows in `in_flight` or `unknown`
    MUST return `denied` (`not_allowed`) when claimed with `activity_origin='replay'`,
    while `deliverables:*` in `unknown` still returns `in_flight_or_unknown` for link recovery.
    """
    monkeypatch.setattr(
        cg_db,
        "require_channel_platform_capability",
        create_autospec(cg_db.require_channel_platform_capability, return_value=None),
    )

    cur = _LockOrderTrackingCursor()
    conn = _FakeConn(cur)
    for key, st in (
        ("fallback:run-r10:flight", "in_flight"),
        ("fallback:run-r10:unknown", "unknown"),
        ("deliverables:run-r10", "unknown"),
    ):
        cur.deliveries[key] = {
            "delivery_key": key,
            "kind": "fallback",
            "user_id": "user-alice",
            "thread_id": "77777777-7777-4777-8777-777777777777",
            "run_id": "run-r10",
            "is_current": True,
            "has_non_empty_text": not key.startswith("deliverables:"),
            "platform": "feishu",
            "target_address": "feishu:@me",
            "status": st,
            "sent_chunks": 0,
            "total_chunks": 1,
            "platform_message_ids": [],
            "files_status": [],
            "source_scope_key": "dm:ou_alice",
            "source_generation": 2,
            "activity_origin": "conversation",
            "delivery_attempt": 1,
            "error_code": "gateway_restarted" if st == "unknown" else None,
        }

    flight_replay = await cg_db.authorize_and_claim_delivery(
        delivery_key="fallback:run-r10:flight",
        kind="fallback",
        user_id="user-alice",
        thread_id="77777777-7777-4777-8777-777777777777",
        run_id="run-r10",
        platform="feishu",
        target="feishu:@me",
        has_non_empty_text=True,
        total_chunks=1,
        source_scope_key="dm:ou_alice",
        source_generation=2,
        activity_origin="replay",
        conn=conn,
    )
    assert flight_replay["claim_status"] == "denied"
    assert flight_replay["code"] == "not_allowed"

    unknown_replay = await cg_db.authorize_and_claim_delivery(
        delivery_key="fallback:run-r10:unknown",
        kind="fallback",
        user_id="user-alice",
        thread_id="77777777-7777-4777-8777-777777777777",
        run_id="run-r10",
        platform="feishu",
        target="feishu:@me",
        has_non_empty_text=True,
        total_chunks=1,
        source_scope_key="dm:ou_alice",
        source_generation=2,
        activity_origin="replay",
        conn=conn,
    )
    assert unknown_replay["claim_status"] == "denied"
    assert unknown_replay["code"] == "not_allowed"

    deliv_replay = await cg_db.authorize_and_claim_delivery(
        delivery_key="deliverables:run-r10",
        kind="fallback",
        user_id="user-alice",
        thread_id="77777777-7777-4777-8777-777777777777",
        run_id="run-r10",
        platform="feishu",
        target="feishu:@me",
        has_non_empty_text=False,
        total_chunks=0,
        source_scope_key="dm:ou_alice",
        source_generation=2,
        activity_origin="replay",
        conn=conn,
    )
    assert deliv_replay["claim_status"] == "in_flight_or_unknown"
    assert deliv_replay["existing_status"] == "unknown"


@pytest.mark.asyncio
async def test_r10_feishu_single_chunk_pending_reply_persists_and_confirms_on_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex r3 High: persisting `first_reply_message_id` on a pending Feishu topic
    allows `lookup_bot_topic` to return `pending_candidates` and I-1 to promote the topic
    to `confirmed` once the initiator's mention-free follow-up arrives with `external_topic_id`.
    """
    monkeypatch.setattr(
        cg_db,
        "require_channel_platform_capability",
        create_autospec(cg_db.require_channel_platform_capability, return_value=None),
    )

    topics: dict[tuple[str, str, str], dict[str, Any]] = {
        ("feishu", "feishu:oc_grp_single", "chat:oc_grp_single:topic:om_root_s1"): {
            "platform": "feishu",
            "chat_address": "feishu:oc_grp_single",
            "canonical_scope_key": "chat:oc_grp_single:topic:om_root_s1",
            "external_topic_id": None,
            "root_message_id": "om_root_s1",
            "first_reply_message_id": None,
            "initiator_dalton_user_id": "user-alice",
            "initiator_platform_user_id": "ou_alice",
            "status": "pending",
        }
    }

    class _PendingTopicCur:
        def __init__(self) -> None:
            self._one: dict[str, Any] | None = None
            self._all: list[dict[str, Any]] = []

        async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
            norm = " ".join(sql.split()).upper()
            self._one = None
            self._all = []
            if "PG_ADVISORY_XACT_LOCK" in norm:
                return
            if norm.startswith("SELECT") and "FROM DALTON_CHANNEL_BOT_TOPICS" in norm:
                if "STATUS = 'PENDING'" in norm and "FIRST_REPLY_MESSAGE_ID IS NOT NULL" in norm:
                    plat, addr = params[0], params[1]
                    self._all = [
                        dict(r)
                        for r in topics.values()
                        if r["platform"] == plat
                        and r["chat_address"] == addr
                        and r["status"] == "pending"
                        and r.get("first_reply_message_id")
                    ]
                    return
                if "STATUS = 'CONFIRMED'" in norm and "EXTERNAL_TOPIC_ID = %S" in norm:
                    plat, addr, ext = params[0], params[1], params[2]
                    for r in topics.values():
                        if (
                            r["platform"] == plat
                            and r["chat_address"] == addr
                            and r.get("external_topic_id") == ext
                            and r["status"] == "confirmed"
                        ):
                            self._one = dict(r)
                            return
                    return
                if "CANONICAL_SCOPE_KEY = %S OR ROOT_MESSAGE_ID = %S" in norm:
                    plat, addr, scope, root_mid = params[0], params[1], params[2], params[3]
                    for r in topics.values():
                        if r["platform"] == plat and r["chat_address"] == addr and (
                            r["canonical_scope_key"] == scope or r.get("root_message_id") == root_mid
                        ):
                            self._one = dict(r)
                            return
                    return
            if norm.startswith("UPDATE DALTON_CHANNEL_BOT_TOPICS") and "FIRST_REPLY_MESSAGE_ID = COALESCE" in norm:
                root_mid, first_rep, plat, addr, scope = params
                row = topics[(plat, addr, scope)]
                row["root_message_id"] = row["root_message_id"] or root_mid
                row["first_reply_message_id"] = row["first_reply_message_id"] or first_rep
                self._one = dict(row)
                return

        async def fetchone(self) -> dict[str, Any] | None:
            return self._one

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._all)

    class _PendingTopicConn:
        def __init__(self, cur: _PendingTopicCur) -> None:
            self._cur = cur

        @asynccontextmanager
        async def cursor(self, **_kwargs: Any):
            yield self._cur

    cur = _PendingTopicCur()
    conn = _PendingTopicConn(cur)

    recorded = await cg_db._record_pending_bot_topic_reply_in_tx(
        cur,
        platform="feishu",
        chat_address="feishu:oc_grp_single",
        canonical_scope_key="chat:oc_grp_single:topic:om_root_s1",
        first_reply_message_id="om_single_reply_1",
        initiator_dalton_user_id="user-alice",
        initiator_platform_user_id="ou_alice",
        root_message_id="om_root_s1",
    )
    assert recorded is not None
    assert recorded["status"] == "pending"
    assert recorded["first_reply_message_id"] == "om_single_reply_1"

    lookup = await cg_db.lookup_bot_topic(
        platform="feishu",
        chat_address="feishu:oc_grp_single",
        match_mode="inbound_followup",
        external_topic_id="omt_delayed_s1",
        conn=conn,
    )
    assert lookup["found"] is False
    assert lookup["pending_candidates"] == [
        {
            "canonical_scope_key": "chat:oc_grp_single:topic:om_root_s1",
            "root_message_id": "om_root_s1",
            "first_reply_message_id": "om_single_reply_1",
            "initiator_platform_user_id": "ou_alice",
            "initiator_dalton_user_id": "user-alice",
        }
    ]


def test_r10_automation_notifications_load_delivery_row_guards_uuid_casts_with_case() -> None:
    """PG regression #2: `_load_delivery_row` MUST guard `::uuid` casts with short-circuiting
    `CASE WHEN ... THEN ...::uuid ELSE NULL END` so Postgres never evaluates `::uuid` on
    non-UUID test/synthetic `execution_id` or `automation_id` strings.
    """
    import inspect

    from src.dalton.database import automation_notifications as auto_db

    src = inspect.getsource(auto_db._load_delivery_row)
    assert "CASE" in src and "THEN d.execution_id::uuid" in src
    assert "THEN d.automation_id::uuid" in src
    assert "AND ae.automation_execution_id = d.execution_id::uuid" not in src
    assert "AND a.automation_id = d.automation_id::uuid" not in src


@pytest.mark.asyncio
async def test_bt1_feishu_pending_topics_initiator_first_cap_warning_and_pagination(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BT1: pending Feishu topic lookup must not silently drop topics past 5;
    must prioritize initiator's pending topics before group-wide pending topics,
    support pagination, and warn when total pending topics exceed the configured cap.
    """
    import logging

    monkeypatch.setattr(
        cg_db,
        "require_channel_platform_capability",
        create_autospec(cg_db.require_channel_platform_capability, return_value=None),
    )

    # Seed 7 pending topics (topic 1 is oldest, belonging to ou_oldest; topics 2..7 are newer by other users).
    pending_rows: list[dict[str, Any]] = []
    for idx in range(1, 8):
        pending_rows.append(
            {
                "platform": "feishu",
                "chat_address": "feishu:oc_grp_bt1",
                "canonical_scope_key": f"chat:oc_grp_bt1:topic:om_root_{idx}",
                "external_topic_id": None,
                "root_message_id": f"om_root_{idx}",
                "first_reply_message_id": f"om_reply_{idx}",
                "initiator_dalton_user_id": f"user-{idx}",
                "initiator_platform_user_id": "ou_oldest" if idx == 1 else f"ou_other_{idx}",
                "status": "pending",
                "last_active_at": idx,  # higher = newer
            }
        )

    class _Bt1Cur:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self._rows = rows
            self._one: dict[str, Any] | None = None
            self._all: list[dict[str, Any]] = []

        async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
            norm = " ".join(sql.split()).upper()
            self._one = None
            self._all = []
            if "STATUS = 'CONFIRMED'" in norm:
                return
            if "STATUS = 'PENDING'" in norm and "FIRST_REPLY_MESSAGE_ID IS NOT NULL" in norm:
                plat, addr = params[0], params[1]
                matched = [
                    dict(r)
                    for r in self._rows
                    if r["platform"] == plat
                    and r["chat_address"] == addr
                    and r["status"] == "pending"
                    and r.get("first_reply_message_id")
                ]
                # Emulate ORDER BY root_mid match, initiator_puid match (if present), last_active_at DESC
                root_mid = params[2] if len(params) >= 4 else None
                init_puid = params[4] if "INITIATOR_PLATFORM_USER_ID = %S" in norm and len(params) >= 6 else None
                matched.sort(
                    key=lambda r: (
                        0 if (root_mid is not None and r.get("root_message_id") == root_mid) else 1,
                        0 if (init_puid is not None and r.get("initiator_platform_user_id") == init_puid) else 1,
                        -int(r["last_active_at"]),
                        str(r["canonical_scope_key"]),
                    )
                )
                # Respect LIMIT / OFFSET in SQL
                import re

                limit_val: int | None = None
                offset_val = 0
                lit_limit = re.search(r"\bLIMIT\s+(\d+)\b", norm)
                if lit_limit:
                    limit_val = int(lit_limit.group(1))
                elif "LIMIT %S" in norm:
                    if "OFFSET %S" in norm:
                        limit_val = int(params[-2])
                        offset_val = int(params[-1])
                    else:
                        limit_val = int(params[-1])
                sliced = matched[offset_val:]
                if limit_val is not None:
                    sliced = sliced[:limit_val]
                self._all = sliced

        async def fetchone(self) -> dict[str, Any] | None:
            return self._one

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._all)

    class _Bt1Conn:
        def __init__(self, cur: _Bt1Cur) -> None:
            self._cur = cur

        @asynccontextmanager
        async def cursor(self, **_kwargs: Any):
            yield self._cur

    conn = _Bt1Conn(_Bt1Cur(pending_rows))

    # 1. Without initiator filter, all 7 pending topics are returned (including oldest #1).
    res_all = await cg_db.lookup_bot_topic(
        platform="feishu",
        chat_address="feishu:oc_grp_bt1",
        match_mode="inbound_followup",
        external_topic_id="omt_bt1_7",
        conn=conn,
    )
    assert res_all["found"] is False
    scopes_all = [c["canonical_scope_key"] for c in res_all.get("pending_candidates", [])]
    assert len(scopes_all) == 7
    assert "chat:oc_grp_bt1:topic:om_root_1" in scopes_all

    # 2. With initiator_platform_user_id='ou_oldest', the oldest topic owned by ou_oldest is prioritized first.
    res_init = await cg_db.lookup_bot_topic(
        platform="feishu",
        chat_address="feishu:oc_grp_bt1",
        match_mode="inbound_followup",
        external_topic_id="omt_bt1_7",
        initiator_platform_user_id="ou_oldest",
        conn=conn,
    )
    scopes_init = [c["canonical_scope_key"] for c in res_init.get("pending_candidates", [])]
    assert scopes_init[0] == "chat:oc_grp_bt1:topic:om_root_1"
    assert len(scopes_init) == 7

    # 3. Pagination traverses all 7 pending topics without dropping any.
    page1 = await cg_db.lookup_bot_topic(
        platform="feishu",
        chat_address="feishu:oc_grp_bt1",
        match_mode="inbound_followup",
        external_topic_id="omt_bt1_7",
        pending_limit=3,
        pending_offset=0,
        conn=conn,
    )
    assert len(page1["pending_candidates"]) == 3
    assert page1.get("pending_candidates_has_more") is True

    page2 = await cg_db.lookup_bot_topic(
        platform="feishu",
        chat_address="feishu:oc_grp_bt1",
        match_mode="inbound_followup",
        external_topic_id="omt_bt1_7",
        pending_limit=3,
        pending_offset=3,
        conn=conn,
    )
    assert len(page2["pending_candidates"]) == 3
    assert page2.get("pending_candidates_has_more") is True

    page3 = await cg_db.lookup_bot_topic(
        platform="feishu",
        chat_address="feishu:oc_grp_bt1",
        match_mode="inbound_followup",
        external_topic_id="omt_bt1_7",
        pending_limit=3,
        pending_offset=6,
        conn=conn,
    )
    assert len(page3["pending_candidates"]) == 1
    assert page3.get("pending_candidates_has_more") is not True
    combined_pages = [
        c["canonical_scope_key"]
        for p in (page1, page2, page3)
        for c in p["pending_candidates"]
    ]
    assert combined_pages == scopes_all

    # 4. Exceeding MAX_PENDING_BOT_TOPIC_CANDIDATES logs a warning instead of silently dropping.
    cap = cg_db.MAX_PENDING_BOT_TOPIC_CANDIDATES
    assert isinstance(cap, int) and cap >= 7
    overflow_rows = [
        {
            "platform": "feishu",
            "chat_address": "feishu:oc_grp_overflow",
            "canonical_scope_key": f"chat:oc_grp_overflow:topic:om_root_{idx}",
            "external_topic_id": None,
            "root_message_id": f"om_root_{idx}",
            "first_reply_message_id": f"om_reply_{idx}",
            "initiator_dalton_user_id": f"user-{idx}",
            "initiator_platform_user_id": f"ou_user_{idx}",
            "status": "pending",
            "last_active_at": idx,
        }
        for idx in range(1, cap + 3)
    ]
    overflow_conn = _Bt1Conn(_Bt1Cur(overflow_rows))
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=cg_db.__name__):
        res_overflow = await cg_db.lookup_bot_topic(
            platform="feishu",
            chat_address="feishu:oc_grp_overflow",
            match_mode="inbound_followup",
            external_topic_id="omt_overflow",
            conn=overflow_conn,
        )
    assert len(res_overflow["pending_candidates"]) == cap
    assert res_overflow.get("pending_candidates_truncated") is True
    assert res_overflow.get("pending_candidates_has_more") is True
    assert any("pending" in rec.message.lower() and "oc_grp_overflow" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_bt2_deliverables_links_unknown_subkey_reclaim_guards_and_parent_i7_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BT2:
    1. `deliverables-links:<runId>:item:<digest>` in `unknown` / `gateway_restarted` with
       uncertain `files_status` (`dispatching` / `unknown`) MUST NOT `ON CONFLICT` reclaim
       in I-6; it returns `in_flight_or_unknown` with `files_status` and `platform_message_ids`.
    2. A provably-unsent `unknown` / `gateway_restarted` `:item:` row (0 chunks, 0 receipts,
       empty or `queued`/`failed` `files_status`) or one reclaimed with `reclaim_verified_unsent=True`
       (and zero confirmed receipts) IS reclaimed via `ON CONFLICT` incrementing `delivery_attempt`.
    3. A confirmed `:item:` row (`linked` or `platform_message_ids`) is NEVER reclaimed even when
       `reclaim_verified_unsent=True`.
    4. I-7 `update_delivery_progress` allows attempt-fenced `unknown -> sent` settlement for
       `deliverables-links:` keys (clearing `error_code`), while rejecting stale attempts and
       non-`deliverables-links:` `unknown -> sent` with 409.
    """
    from fastapi import HTTPException

    monkeypatch.setattr(
        cg_db,
        "require_channel_platform_capability",
        create_autospec(cg_db.require_channel_platform_capability, return_value=None),
    )

    # Pure transition-validator assertions (covers Discord + Feishu + stale attempt + non-link keys)
    assert (
        cg_db._validate_delivery_status_transition(
            prev_status="unknown",
            status="sent",
            outcome=None,
            platform="discord",
            kind="fallback",
            error_code="gateway_restarted",
            discord_body_phase=None,
            current_attempt=1,
            requested_attempt=1,
            delivery_key="deliverables-links:run-bt2-val",
        )
        == "sent"
    )
    with pytest.raises(HTTPException) as exc_stale:
        cg_db._validate_delivery_status_transition(
            prev_status="unknown",
            status="sent",
            outcome=None,
            platform="feishu",
            kind="fallback",
            error_code="gateway_restarted",
            discord_body_phase=None,
            current_attempt=2,
            requested_attempt=1,
            delivery_key="deliverables-links:run-bt2-val",
        )
    assert exc_stale.value.status_code == 409

    with pytest.raises(HTTPException) as exc_non_links:
        cg_db._validate_delivery_status_transition(
            prev_status="unknown",
            status="sent",
            outcome=None,
            platform="feishu",
            kind="fallback",
            error_code="gateway_restarted",
            discord_body_phase=None,
            current_attempt=1,
            requested_attempt=1,
            delivery_key="fallback:run-bt2-val",
        )
    assert exc_non_links.value.status_code == 409

    # Exercise `authorize_and_claim_delivery` against `_LockOrderTrackingCursor`
    cur = _LockOrderTrackingCursor()
    conn = _FakeConn(cur)
    run_id = "run-bt2-py"
    tid = "77777777-7777-4777-8777-777777777777"
    base_row = {
        "kind": "fallback",
        "user_id": "user-alice",
        "thread_id": tid,
        "run_id": run_id,
        "is_current": True,
        "has_non_empty_text": False,
        "platform": "feishu",
        "target_address": "feishu:@me",
        "status": "unknown",
        "sent_chunks": 0,
        "total_chunks": 1,
        "platform_message_ids": [],
        "source_scope_key": "dm:ou_alice",
        "source_generation": 2,
        "activity_origin": "conversation",
        "delivery_attempt": 1,
        "error_code": "gateway_restarted",
    }

    # Case 1: `unknown` + `gateway_restarted` with `dispatching` in files_status -> MUST NOT reclaim
    k_disp = f"deliverables-links:{run_id}:item:dispatching01"
    cur.deliveries[k_disp] = {
        **base_row,
        "delivery_key": k_disp,
        "files_status": [
            {"workspace_id": "ws-a", "path": "results/a.pdf", "status": "dispatching"}
        ],
    }
    res_disp = await cg_db.authorize_and_claim_delivery(
        delivery_key=k_disp,
        kind="fallback",
        user_id="user-alice",
        thread_id=tid,
        run_id=run_id,
        platform="feishu",
        target="feishu:@me",
        has_non_empty_text=False,
        total_chunks=1,
        source_scope_key="dm:ou_alice",
        source_generation=2,
        activity_origin="replay",
        conn=conn,
    )
    assert res_disp["claim_status"] == "in_flight_or_unknown"
    assert res_disp["existing_status"] == "unknown"
    assert res_disp["error_code"] == "gateway_restarted"
    assert res_disp["platform_message_ids"] == []
    assert res_disp["files_status"][0]["status"] == "dispatching"

    # Case 2: Confirmed `:item:` (`linked` + `platform_message_ids`) MUST NOT reclaim even with `reclaim_verified_unsent=True`
    k_linked = f"deliverables-links:{run_id}:item:linked02"
    cur.deliveries[k_linked] = {
        **base_row,
        "delivery_key": k_linked,
        "sent_chunks": 1,
        "platform_message_ids": ["om_confirmed_02"],
        "files_status": [
            {
                "workspace_id": "ws-a",
                "path": "results/b.pdf",
                "status": "linked",
                "platform_message_id": "om_confirmed_02",
            }
        ],
    }
    res_linked = await cg_db.authorize_and_claim_delivery(
        delivery_key=k_linked,
        kind="fallback",
        user_id="user-alice",
        thread_id=tid,
        run_id=run_id,
        platform="feishu",
        target="feishu:@me",
        has_non_empty_text=False,
        total_chunks=1,
        source_scope_key="dm:ou_alice",
        source_generation=2,
        activity_origin="replay",
        reclaim_verified_unsent=True,
        conn=conn,
    )
    assert res_linked["claim_status"] == "in_flight_or_unknown"
    assert res_linked["platform_message_ids"] == ["om_confirmed_02"]
    assert res_linked["files_status"][0]["status"] == "linked"

    # Case 3: Provably-unsent `gateway_restarted` `:item:` (`files_status: []`, 0 chunks, 0 receipts)
    # and `reclaim_verified_unsent=True` on uncertain-but-unconfirmed `:item:` both pass the guard.
    assert cg_db._is_provably_unsent_deliverable_link_item(
        {"sent_chunks": 0, "platform_message_ids": [], "files_status": []}
    ) is True
    assert cg_db._is_provably_unsent_deliverable_link_item(
        {
            "sent_chunks": 0,
            "platform_message_ids": [],
            "files_status": [{"status": "queued"}],
        }
    ) is True
    assert cg_db._is_provably_unsent_deliverable_link_item(cur.deliveries[k_disp]) is False
    assert cg_db._has_no_confirmed_deliverable_link_receipt(cur.deliveries[k_disp]) is True
    assert cg_db._has_no_confirmed_deliverable_link_receipt(cur.deliveries[k_linked]) is False
