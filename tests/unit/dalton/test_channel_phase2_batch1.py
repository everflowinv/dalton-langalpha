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
                plat, addr, root_id = params
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
