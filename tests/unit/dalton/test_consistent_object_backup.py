"""Unit tests for Batch 1 Step 3: full-consumer reconciliation, consistent snapshot backup,
backup_lib preflight/verify-mount, post-restore inflight convergence, transcription resubmit,
Everflow prod backup/offsite orchestration, launchd templates, deploy_main compat, and
Standalone Core wrapper.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.dalton.ops import backup_lib
from scripts.dalton.ops.prod import local_backup_set, offsite_sync
from scripts.standalone import consistent_backup as standalone_cb
from src.dalton import consistent_backup, storage_refs
from src.dalton.app import transcriptions as transcriptions_app
from src.dalton.capabilities import CapabilityId, DALTON_ROUTE_CAPABILITY_REGISTRY
from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.database import transcriptions as tr_db
from src.dalton.services import transcriptions as tr_svc
from src.dalton.storage_keys import storage_user_id
from src.dalton.transcription_service.jobs import AsrJobManager, FakeAsrWorker

REPO_ROOT = Path(__file__).resolve().parents[3]
OPS_DIR = REPO_ROOT / "scripts" / "dalton" / "ops"
PROD_DIR = OPS_DIR / "prod"


# ---------------------------------------------------------------------------
# In-memory S3 versioned bucket double + async DB connection double
# ---------------------------------------------------------------------------


class _FakeS3Client:
    """In-memory versioned (or unversioned) S3 client for testing consistent backup/restore."""

    def __init__(self, *, versioning_by_bucket: dict[str, bool] | None = None) -> None:
        self.versioning_by_bucket: dict[str, bool] = dict(versioning_by_bucket or {})
        # bucket -> key -> list[{"version_id": str, "is_delete_marker": bool, "data": bytes, "content_type": str, "last_modified": dt}]
        self._store: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._seq = 0
        self.get_calls: list[dict[str, Any]] = []
        self.put_calls: list[dict[str, Any]] = []

    def _next_vid(self, bucket: str) -> str:
        self._seq += 1
        if not self.versioning_by_bucket.get(bucket, True):
            return "null"
        return f"v{self._seq:04d}"

    def seed_object(
        self,
        bucket: str,
        key: str,
        data: bytes,
        *,
        version_id: str | None = None,
        content_type: str = "application/octet-stream",
        last_modified: dt.datetime | None = None,
    ) -> str:
        vid = version_id if version_id is not None else self._next_vid(bucket)
        ts = last_modified or dt.datetime(2026, 11, 27, 3, 0, self._seq % 60, tzinfo=dt.timezone.utc)
        self._seq += 1
        entries = self._store.setdefault(bucket, {}).setdefault(key, [])
        entries.insert(
            0,
            {
                "version_id": vid,
                "is_delete_marker": False,
                "data": data,
                "content_type": content_type,
                "last_modified": ts,
            },
        )
        return vid

    def delete_object_marker(
        self,
        bucket: str,
        key: str,
        *,
        last_modified: dt.datetime | None = None,
    ) -> str:
        self._seq += 1
        vid = f"dm{self._seq:04d}"
        ts = last_modified or dt.datetime(2026, 11, 27, 3, 5, self._seq % 60, tzinfo=dt.timezone.utc)
        entries = self._store.setdefault(bucket, {}).setdefault(key, [])
        if self.versioning_by_bucket.get(bucket, True):
            entries.insert(
                0,
                {
                    "version_id": vid,
                    "is_delete_marker": True,
                    "data": b"",
                    "content_type": "",
                    "last_modified": ts,
                },
            )
        else:
            self._store.setdefault(bucket, {}).pop(key, None)
        return vid

    def get_bucket_versioning(self, *, Bucket: str) -> dict[str, Any]:
        if self.versioning_by_bucket.get(Bucket, True):
            return {"Status": "Enabled"}
        return {}

    def list_object_versions(self, *, Bucket: str, Prefix: str = "", **_kw: Any) -> dict[str, Any]:
        versions: list[dict[str, Any]] = []
        delete_markers: list[dict[str, Any]] = []
        for key, history in sorted(self._store.get(Bucket, {}).items()):
            if Prefix and not key.startswith(Prefix):
                continue
            for idx, rec in enumerate(history):
                is_latest = idx == 0
                if rec["is_delete_marker"]:
                    delete_markers.append(
                        {
                            "Key": key,
                            "VersionId": rec["version_id"],
                            "IsLatest": is_latest,
                            "LastModified": rec["last_modified"],
                        }
                    )
                else:
                    versions.append(
                        {
                            "Key": key,
                            "VersionId": rec["version_id"],
                            "IsLatest": is_latest,
                            "Size": len(rec["data"]),
                            "LastModified": rec["last_modified"],
                        }
                    )
        return {"Versions": versions, "DeleteMarkers": delete_markers, "IsTruncated": False}

    def list_objects_v2(self, *, Bucket: str, Prefix: str = "", **_kw: Any) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        for key, history in sorted(self._store.get(Bucket, {}).items()):
            if Prefix and not key.startswith(Prefix):
                continue
            if not history or history[0]["is_delete_marker"]:
                continue
            latest = history[0]
            contents.append(
                {
                    "Key": key,
                    "Size": len(latest["data"]),
                    "LastModified": latest["last_modified"],
                }
            )
        return {"Contents": contents, "IsTruncated": False}

    def get_object(self, *, Bucket: str, Key: str, VersionId: str | None = None) -> dict[str, Any]:
        self.get_calls.append({"Bucket": Bucket, "Key": Key, "VersionId": VersionId})
        history = self._store.get(Bucket, {}).get(Key, [])
        if VersionId is not None:
            for rec in history:
                if not rec["is_delete_marker"] and rec["version_id"] == VersionId:
                    return {
                        "Body": io.BytesIO(rec["data"]),
                        "ContentType": rec["content_type"],
                        "VersionId": rec["version_id"],
                    }
            raise KeyError(f"No version {VersionId} for {Bucket}/{Key}")
        if history and not history[0]["is_delete_marker"]:
            rec = history[0]
            return {
                "Body": io.BytesIO(rec["data"]),
                "ContentType": rec["content_type"],
            }
        raise KeyError(f"Missing object {Bucket}/{Key}")

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str = "application/octet-stream",
    ) -> dict[str, Any]:
        self.put_calls.append({"Bucket": Bucket, "Key": Key, "Size": len(Body)})
        vid = self.seed_object(Bucket, Key, Body, content_type=ContentType)
        return {"VersionId": vid}


class _FakeAsyncCursor:
    def __init__(self, conn: _FakeAsyncConn) -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount: int = 0

    async def __aenter__(self) -> _FakeAsyncCursor:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        self._conn.sql_log.append((sql.strip(), params))
        res = self._conn.handler(sql, params)
        if isinstance(res, int):
            self.rowcount = res
            self._rows = []
        elif isinstance(res, list):
            self._rows = res
            self.rowcount = len(res)
        elif res is None:
            self._rows = []
            self.rowcount = 0
        else:
            self._rows = [res]
            self.rowcount = 1

    async def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[Any]:
        return list(self._rows)


class _FakeAsyncTx:
    def __init__(self, conn: _FakeAsyncConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeAsyncTx:
        self._conn.in_tx = True
        self._conn.tx_events.append("BEGIN_TX")
        return self

    async def __aexit__(self, exc_type: Any, *args: Any) -> None:
        self._conn.in_tx = False
        self._conn.tx_events.append("ROLLBACK_TX" if exc_type else "COMMIT_TX")


class _FakeAsyncConn:
    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.sql_log: list[tuple[str, Any]] = []
        self.tx_events: list[str] = []
        self.in_tx: bool = False
        self.closed: bool = False

    def cursor(self) -> _FakeAsyncCursor:
        return _FakeAsyncCursor(self)

    def transaction(self) -> _FakeAsyncTx:
        return _FakeAsyncTx(self)

    async def close(self) -> None:
        self.closed = True
        return None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 1. storage_refs.py: full-consumer reconciliation & skill sha256: prefix
# ---------------------------------------------------------------------------


def test_parse_skill_content_hash_prefix_validation() -> None:
    good_hex = "a" * 64
    assert storage_refs.parse_skill_content_hash(f"sha256:{good_hex}") == good_hex

    for bad in (
        None,
        "",
        good_hex,  # missing sha256: prefix
        f"md5:{good_hex}",
        f"SHA256:{good_hex}",
        "sha256:short",
        f"sha256:{'A' * 64}",
    ):
        with pytest.raises(ValueError, match="sha256:"):
            storage_refs.parse_skill_content_hash(bad)


def test_storage_refs_full_consumer_reconciliation_and_edge_cases(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blob_bytes = b"hello-workspace-blob"
    blob_sha = _sha(blob_bytes)
    pack_bytes = b"PACKMEMBER1_PACKMEMBER2"
    pack_sha = _sha(pack_bytes)
    skill_bytes = b"PK\x03\x04canonical-skill-zip"
    skill_sha = _sha(skill_bytes)
    widget_bytes = b'{"series":[1,2,3]}'
    widget_sha = _sha(widget_bytes)
    chat_bytes = b"large-chat-upload-payload"
    chat_sha = _sha(chat_bytes)
    asr_audio = b"ID3-synthetic-audio"
    asr_sha = _sha(asr_audio)
    company_mapped = storage_user_id(COMPANY_SYSTEM_USER_ID)
    assert company_mapped == "sys_dalton-company"

    # Build asr-jobs.tar.gz with succeeded job (state.json, result.json with raw_response_file, raw_response.json)
    asr_dir = tmp_path / "asr_jobs"
    job_ok_dir = asr_dir / "job-ok-1"
    job_ok_dir.mkdir(parents=True)
    (job_ok_dir / "state.json").write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")
    (job_ok_dir / "result.json").write_text(
        json.dumps({"text": "ok", "raw_response_file": "raw_response.json"}),
        encoding="utf-8",
    )
    (job_ok_dir / "raw_response.json").write_text(
        json.dumps({"segments": [{"text": "ok"}]}), encoding="utf-8"
    )
    asr_tar = tmp_path / "asr-jobs.tar.gz"
    consistent_backup.archive_terminal_asr_jobs(asr_dir, ["job-ok-1"], asr_tar)

    inv = storage_refs.SnapshotRefInventory(
        references=[
            storage_refs.ObjectReference(
                consumer="workspace_file_blobs",
                bucket_role="main",
                object_key=f"blobs/{company_mapped}/{blob_sha}",
                row_id=f"{COMPANY_SYSTEM_USER_ID}:{blob_sha}",
                expected_size=len(blob_bytes),
                expected_sha256=blob_sha,
                check_mode="strict",
            ),
            storage_refs.ObjectReference(
                consumer="workspace_files_pack",
                bucket_role="main",
                object_key=f"blobs/u1/{pack_sha}",
                row_id=f"u1:{pack_sha}",
                expected_size=len(pack_bytes),
                expected_sha256=pack_sha,
                check_mode="strict",
            ),
            storage_refs.ObjectReference(
                consumer="provenance_result_bodies",
                bucket_role="main",
                object_key="provenance/prov1",
                row_id="prov1",
                expected_size=999,  # Different from manifest size -> WARNING only, NOT error!
                check_mode="exists_warn_size",
            ),
            storage_refs.ObjectReference(
                consumer="memo_binaries",
                bucket_role="main",
                object_key="memo/u1/doc.pdf",
                row_id="memo-1",
                check_mode="exists_only",
            ),
            storage_refs.ObjectReference(
                consumer="user_skills",
                bucket_role="main",
                object_key=f"user-skills/u1/{skill_sha}.zip",
                row_id="skill-1",
                expected_sha256=storage_refs.parse_skill_content_hash(f"sha256:{skill_sha}"),
                check_mode="strict",
            ),
            storage_refs.ObjectReference(
                consumer="widgets_turn_slices",
                bucket_role="main",
                object_key=f"private-media/widgets/t1/{widget_sha}.json",
                row_id="resp-1",
                expected_size=len(widget_bytes),
                expected_sha256=widget_sha,
                check_mode="strict",
            ),
            storage_refs.ObjectReference(
                consumer="users_avatars",
                bucket_role="main",
                object_key="avatars/u1.png",
                row_id="u1",
                check_mode="exists_only",
            ),
            storage_refs.ObjectReference(
                consumer="chat_attachments",
                bucket_role="main",
                object_key="attachments/a1/report.pdf",
                row_id="a1",
                expected_size=123,
                check_mode="strict",
            ),
            storage_refs.ObjectReference(
                consumer="dalton_chat_file_uploads",
                bucket_role="chat_upload",
                object_key="chat-uploads/ws1/up1/data.csv",
                row_id="up1",
                expected_size=len(chat_bytes),
                expected_sha256=chat_sha,
                check_mode="strict",
            ),
            storage_refs.ObjectReference(
                consumer="dalton_transcription_jobs",
                bucket_role="asr",
                object_key="transcriptions/ws1/job-ok-1/source/audio.mp3",
                row_id="job-ok-1",
                expected_size=len(asr_audio),
                expected_sha256=asr_sha,
                check_mode="strict",
            ),
            storage_refs.ObjectReference(
                consumer="channel_snapshots",
                bucket_role="chat_upload",
                object_key="channel-snapshots/00000000-0000-0000-0000-000000000001/0123456789abcdef/chart_l1.png",
                row_id="resp-1:l1",
                check_mode="exists_only",
            ),
        ],
        terminal_asr_job_ids=["job-ok-1"],
        succeeded_asr_job_ids=["job-ok-1"],
        skipped_pending={"chat_attachments": 1, "dalton_chat_file_uploads": 1},
    )

    manifest = [
        {"bucket": "dalton-main", "key": f"blobs/{company_mapped}/{blob_sha}", "size": len(blob_bytes), "archived_sha256": blob_sha},
        {"bucket": "dalton-main", "key": f"blobs/u1/{pack_sha}", "size": len(pack_bytes), "archived_sha256": pack_sha},
        {"bucket": "dalton-main", "key": "provenance/prov1", "size": 456, "archived_sha256": "b" * 64},
        {"bucket": "dalton-main", "key": "memo/u1/doc.pdf", "size": 10, "archived_sha256": "c" * 64},
        {"bucket": "dalton-main", "key": f"user-skills/u1/{skill_sha}.zip", "size": len(skill_bytes), "archived_sha256": skill_sha},
        {"bucket": "dalton-main", "key": f"private-media/widgets/t1/{widget_sha}.json", "size": len(widget_bytes), "archived_sha256": widget_sha},
        {"bucket": "dalton-main", "key": "avatars/u1.png", "size": 32, "archived_sha256": "d" * 64},
        {"bucket": "dalton-main", "key": "attachments/a1/report.pdf", "size": 123, "archived_sha256": "e" * 64},
        {"bucket": "dalton-asr", "key": "chat-uploads/ws1/up1/data.csv", "size": len(chat_bytes), "archived_sha256": chat_sha},
        {"bucket": "dalton-asr", "key": "transcriptions/ws1/job-ok-1/source/audio.mp3", "size": len(asr_audio), "archived_sha256": asr_sha},
        {"bucket": "dalton-asr", "key": "channel-snapshots/00000000-0000-0000-0000-000000000001/0123456789abcdef/chart_l1.png", "size": 55, "archived_sha256": "f" * 64},
    ]

    with caplog.at_level("WARNING"):
        report = storage_refs.reconcile_inventory_against_manifest(
            inv,
            manifest,
            bucket_targets={
                "main": ("dalton-main", ""),
                "asr": ("dalton-asr", ""),
                "chat_upload": ("dalton-asr", ""),
            },
            asr_jobs_archive=asr_tar,
        )

    assert report.ok is True, report.errors
    assert report.checked_refs == len(inv.references)
    assert report.checked_asr_jobs == 1
    assert len(report.warnings) == 1
    assert report.warnings[0].consumer == "provenance_result_bodies"
    assert report.warnings[0].code == "mutable_size_drift"

    # If raw_response.json is missing while result.json declares raw_response_file, reconciliation MUST fail.
    (job_ok_dir / "raw_response.json").unlink()
    asr_tar_missing_raw = tmp_path / "asr-jobs-missing-raw.tar.gz"
    consistent_backup.archive_terminal_asr_jobs(asr_dir, ["job-ok-1"], asr_tar_missing_raw)
    bad_report = storage_refs.reconcile_inventory_against_manifest(
        inv,
        manifest,
        bucket_targets={"main": ("dalton-main", ""), "asr": ("dalton-asr", ""), "chat_upload": ("dalton-asr", "")},
        asr_jobs_archive=asr_tar_missing_raw,
    )
    assert bad_report.ok is False
    assert any(e.code == "missing_or_invalid_raw_response_json" for e in bad_report.errors)


def test_storage_refs_enumerates_pack_bounds_inline_attachments_and_bad_skill_prefix() -> None:
    pack_sha = _sha(b"0123456789")
    good_skill_sha = _sha(b"zip")

    def _handler(sql: str, params: Any) -> Any:
        s = " ".join(sql.split())
        if s.startswith("SELECT to_regclass"):
            return (params[0],)
        if "FROM workspace_file_blobs WHERE condemned_at IS NULL" in s:
            return [("u1", pack_sha, 10)]
        if "FROM workspace_files f JOIN workspaces w" in s:
            return [
                ("wf-good", "u1", 5, None, pack_sha, 5),   # 5 + 5 <= 10 OK
                ("wf-oob", "u1", 6, None, pack_sha, 6),    # 6 + 6 > 10 OOB!
            ]
        if "FROM user_skills WHERE archive_key" in s:
            return [
                ("sk-ok", f"user-skills/u1/{good_skill_sha}.zip", f"sha256:{good_skill_sha}"),
                ("sk-bad", "user-skills/u1/bad.zip", good_skill_sha),  # Missing sha256: prefix!
            ]
        if "FROM chat_attachments WHERE cleaned_at IS NULL" in s:
            return [
                ("att-inline", "ready", None, 42),        # Pre-upgrade inline -> exempt!
                ("att-up", "uploading", "attachments/up/x", 10),  # skipped_pending
                ("att-ready", "ready", "attachments/r1/x.pdf", 77),
            ]
        if "FROM drive_uploads" in s:
            return [(2,)]
        if "FROM pg_attribute" in s:
            return []
        return []

    conn = _FakeAsyncConn(_handler)
    inv = asyncio.run(storage_refs.enumerate_snapshot_refs(conn, include_aux=False))
    assert inv.skipped_pending["chat_attachments"] == 1
    assert inv.skipped_pending["drive_uploads"] == 2
    consumers = [r.consumer for r in inv.references]
    assert "chat_attachments" in consumers
    # Only att-ready (not att-inline) is in chat_attachments refs
    att_refs = [r for r in inv.references if r.consumer == "chat_attachments"]
    assert [r.row_id for r in att_refs] == ["att-ready"]
    # Out-of-bounds pack member and missing sha256: prefix skill are flagged invalid
    invalid = {(r.consumer, r.row_id) for r in inv.references if r.check_mode == "invalid"}
    assert ("workspace_files_pack_member", "wf-oob") in invalid
    assert ("user_skills", "sk-bad") in invalid


# ---------------------------------------------------------------------------
# 2. Consistent snapshot backup: two-rule version selection, locks, sinks, restore
# ---------------------------------------------------------------------------


def test_version_selection_pre_and_post_snapshot_deletes_null_version_and_unversioned() -> None:
    s3 = _FakeS3Client(versioning_by_bucket={"main": True})
    # Key deleted BEFORE snapshot (not in snapshot_referenced_keys) -> skipped
    s3.seed_object("main", "blobs/u1/deleted_before", b"old-gone")
    s3.delete_object_marker("main", "blobs/u1/deleted_before")
    # Key deleted AFTER snapshot (still in snapshot_referenced_keys) -> previous version pinned!
    skill_old = b"old-skill-bytes"
    old_vid = s3.seed_object(
        "main",
        "user-skills/u1/sk.zip",
        skill_old,
        last_modified=dt.datetime(2026, 11, 27, 2, 0, 0, tzinfo=dt.timezone.utc),
    )
    s3.delete_object_marker(
        "main",
        "user-skills/u1/sk.zip",
        last_modified=dt.datetime(2026, 11, 27, 3, 1, 0, tzinfo=dt.timezone.utc),
    )
    # Pre-versioning object with VersionId == "null"
    s3.seed_object("main", "memo/u1/legacy.pdf", b"legacy-null-vid", version_id="null")
    # Overwritten avatar
    s3.seed_object(
        "main",
        "avatars/u1.png",
        b"old-avatar",
        last_modified=dt.datetime(2026, 11, 27, 1, 0, 0, tzinfo=dt.timezone.utc),
    )
    new_avatar_vid = s3.seed_object(
        "main",
        "avatars/u1.png",
        b"new-readable-avatar",
        last_modified=dt.datetime(2026, 11, 27, 3, 2, 0, tzinfo=dt.timezone.utc),
    )

    vers, dms = consistent_backup.list_all_object_versions(s3, "main")
    sel = consistent_backup.select_bucket_object_versions(
        bucket="main",
        versions=vers,
        delete_markers=dms,
        snapshot_referenced_keys={"user-skills/u1/sk.zip", "memo/u1/legacy.pdf", "avatars/u1.png"},
        versioning_enabled=True,
    )
    by_key = {item.key: item for item in sel.selected}
    assert "blobs/u1/deleted_before" not in by_key
    assert sel.skipped_pre_snapshot_deleted == 1
    assert by_key["user-skills/u1/sk.zip"].version_id == old_vid
    assert by_key["user-skills/u1/sk.zip"].selected_from_delete_marker is True
    assert by_key["memo/u1/legacy.pdf"].version_id == "null"
    assert by_key["avatars/u1.png"].version_id == new_avatar_vid

    # Missing snapshot-referenced key with zero versions -> MUST raise
    with pytest.raises(consistent_backup.MissingReferencedObjectsError):
        consistent_backup.select_bucket_object_versions(
            bucket="main",
            versions=vers,
            delete_markers=dms,
            snapshot_referenced_keys={"blobs/u1/never_existed"},
            versioning_enabled=True,
        )

    # Unversioned external bucket -> emits WARNING, omits VersionId, fails if ref missing
    s3_unv = _FakeS3Client(versioning_by_bucket={"ext": False})
    s3_unv.seed_object("ext", "blobs/u1/k1", b"data-k1")
    cur = consistent_backup.list_all_current_objects(s3_unv, "ext")
    sel_unv = consistent_backup.select_bucket_object_versions(
        bucket="ext",
        versions=cur,
        delete_markers=[],
        snapshot_referenced_keys={"blobs/u1/k1"},
        versioning_enabled=False,
    )
    assert sel_unv.warning is not None
    assert sel_unv.selected[0].version_id is None
    with pytest.raises(consistent_backup.MissingReferencedObjectsError):
        consistent_backup.select_bucket_object_versions(
            bucket="ext",
            versions=cur,
            delete_markers=[],
            snapshot_referenced_keys={"blobs/u1/missing"},
            versioning_enabled=False,
        )


def test_consistent_backup_e2e_lock_order_mirror_and_targz_and_restore(tmp_path: Path) -> None:
    skill_bytes = b"PK\x03\x04skill-archive-bytes"
    skill_sha = _sha(skill_bytes)
    avatar_old = b"PNG-OLD"
    avatar_new = b"PNG-NEW"
    asr_audio = b"AUDIO-BYTES"
    asr_sha = _sha(asr_audio)

    s3 = _FakeS3Client(versioning_by_bucket={"dalton-main": True, "dalton-asr": True})
    # Pre-snapshot deleted key
    s3.seed_object("dalton-main", "blobs/u1/pre_deleted", b"gone-before-snap")
    s3.delete_object_marker("dalton-main", "blobs/u1/pre_deleted")
    # Skill archive present at snapshot, deleted right AFTER snapshot commit!
    s3.seed_object("dalton-main", f"user-skills/u1/{skill_sha}.zip", skill_bytes)
    # Avatar present at snapshot, overwritten right AFTER snapshot commit!
    s3.seed_object("dalton-main", "avatars/u1.png", avatar_old)
    # ASR audio in aux bucket
    s3.seed_object("dalton-asr", "transcriptions/ws1/job-done/source/a.mp3", asr_audio)

    # ASR jobs dir: one succeeded (with raw_response.json + temp .lock + segments/), one inflight
    asr_dir = tmp_path / "asr_jobs"
    (asr_dir / "asr_usage.jsonl").parent.mkdir(parents=True)
    (asr_dir / "asr_usage.jsonl").write_text('{"job_id":"job-done"}\n', encoding="utf-8")
    done_dir = asr_dir / "job-done"
    (done_dir / "segments").mkdir(parents=True)
    (done_dir / "segments" / "s0.mp3").write_bytes(b"temp-slice")
    (done_dir / "state.json").write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")
    (done_dir / "state.json.lock").write_text("lock", encoding="utf-8")
    (done_dir / "result.json").write_text(
        json.dumps({"text": "done", "raw_response_file": "raw_response.json"}),
        encoding="utf-8",
    )
    (done_dir / "raw_response.json").write_text(json.dumps({"raw": True}), encoding="utf-8")
    inflight_dir = asr_dir / "job-running"
    inflight_dir.mkdir(parents=True)
    (inflight_dir / "state.json").write_text(json.dumps({"status": "transcribing"}), encoding="utf-8")

    def _db_handler(sql: str, params: Any) -> Any:
        s = " ".join(sql.split())
        if s.startswith("SELECT pg_export_snapshot"):
            return ("00000003-00000042-1",)
        if s.startswith("SELECT pg_advisory_lock") or s.startswith("SELECT pg_advisory_unlock"):
            return (True, True)
        if s.startswith("SELECT to_regclass"):
            return (params[0],)
        if "FROM user_skills WHERE archive_key" in s:
            return [("sk-1", f"user-skills/u1/{skill_sha}.zip", f"sha256:{skill_sha}")]
        if "FROM users WHERE" in s:
            return [("u1", None, "https://app.example.com/storage/avatars/u1.png")]
        if "FROM dalton_transcription_jobs" in s:
            return [
                ("job-done", "succeeded", "transcriptions/ws1/job-done/source/a.mp3", len(asr_audio), asr_sha, {"upload": {"completed": True}}),
                ("job-running", "transcribing", "transcriptions/ws1/job-done/source/a.mp3", len(asr_audio), asr_sha, {"upload": {"completed": True}}),
            ]
        return []

    def _after_commit(_snap_id: str, _inv: Any) -> None:
        # Simulate concurrent mutations AFTER snapshot commit and BEFORE list_object_versions!
        s3.delete_object_marker("dalton-main", f"user-skills/u1/{skill_sha}.zip")
        s3.seed_object("dalton-main", "avatars/u1.png", avatar_new)

    def _fake_pg_dump(snap_id: str, out_path: Path) -> None:
        assert snap_id == "00000003-00000042-1"
        out_path.write_bytes(b"PGDMP-synthetic")

    # Run with Everflow MirrorPoolSink
    conn = _FakeAsyncConn(_db_handler)
    mirror_sink = consistent_backup.MirrorPoolSink(tmp_path / "minio-mirror")
    res = asyncio.run(
        consistent_backup.run_consistent_backup(
            conn=conn,
            pg_dump_path=tmp_path / "pg.dump",
            pg_dump_fn=_fake_pg_dump,
            bucket_specs=[
                consistent_backup.BucketArchiveSpec(bucket="dalton-main", roles=("main",), s3_client=s3),
                consistent_backup.BucketArchiveSpec(bucket="dalton-asr", roles=("asr", "chat_upload"), s3_client=s3),
            ],
            object_sink=mirror_sink,
            asr_jobs_dir=asr_dir,
            asr_archive_path=tmp_path / "asr-jobs.tar.gz",
            env={"PUBLIC_APP_URL": "https://app.example.com"},
            after_snapshot_commit_hook=_after_commit,
        )
    )
    assert res.reconcile_report.ok is True
    # Verify protocol order: advisory lock -> REPEATABLE READ -> pg_export_snapshot -> COMMIT -> advisory unlock
    sql_texts = [q for q, _p in conn.sql_log]
    idx_lock = next(i for i, q in enumerate(sql_texts) if "pg_advisory_lock" in q)
    idx_begin = next(i for i, q in enumerate(sql_texts) if "REPEATABLE READ READ ONLY" in q)
    idx_snap = next(i for i, q in enumerate(sql_texts) if "pg_export_snapshot" in q)
    idx_commit = next(i for i, q in enumerate(sql_texts) if q == "COMMIT")
    idx_unlock = next(i for i, q in enumerate(sql_texts) if "pg_advisory_unlock" in q)
    assert idx_lock < idx_begin < idx_snap < idx_commit < idx_unlock

    # Verify ASR tar contains ONLY terminal job-done (no job-running, no .lock, no segments/) + asr_usage.jsonl
    assert res.asr_archive is not None
    with tarfile.open(res.asr_archive.archive_path, "r:gz") as tf:
        names = set(tf.getnames())
    assert "asr_usage.jsonl" in names
    assert "job-done/state.json" in names
    assert "job-done/result.json" in names
    assert "job-done/raw_response.json" in names
    assert not any("job-running" in n for n in names)
    assert not any(n.endswith(".lock") for n in names)
    assert not any("segments/" in n for n in names)

    # Authoritative manifest restore into fresh S3 buckets
    target_s3 = _FakeS3Client(versioning_by_bucket={"dalton-main": True, "dalton-asr": True})
    sha_idx = consistent_backup.build_archived_sha256_index(
        mirror_objects_dir=tmp_path / "minio-mirror" / "objects"
    )
    restored_keys = consistent_backup.restore_objects_from_manifest(
        [r.to_dict() for r in res.manifest_objects],
        sha_index=sha_idx,
        s3_clients_by_bucket={"dalton-main": target_s3, "dalton-asr": target_s3},
    )
    assert "blobs/u1/pre_deleted" not in restored_keys["dalton-main"]
    assert f"user-skills/u1/{skill_sha}.zip" in restored_keys["dalton-main"]
    # Post-snapshot deleted skill archive restored with exact old bytes!
    assert (
        target_s3.get_object(Bucket="dalton-main", Key=f"user-skills/u1/{skill_sha}.zip")["Body"].read()
        == skill_bytes
    )
    # Overwritten avatar restored with readable image bytes!
    assert target_s3.get_object(Bucket="dalton-main", Key="avatars/u1.png")["Body"].read() == avatar_new

    # Also test Standalone Core TarGzObjectSink
    conn2 = _FakeAsyncConn(_db_handler)
    tar_path = tmp_path / "minio-objects.tar.gz"
    tar_sink = consistent_backup.TarGzObjectSink(tar_path)
    res2 = asyncio.run(
        consistent_backup.run_consistent_backup(
            conn=conn2,
            pg_dump_path=tmp_path / "postgres.dump",
            pg_dump_fn=_fake_pg_dump,
            bucket_specs=[
                consistent_backup.BucketArchiveSpec(bucket="dalton-main", roles=("main",), s3_client=s3),
                consistent_backup.BucketArchiveSpec(bucket="dalton-asr", roles=("asr", "chat_upload"), s3_client=s3),
            ],
            object_sink=tar_sink,
            asr_jobs_dir=asr_dir,
            asr_archive_path=tmp_path / "asr-jobs-core.tar.gz",
            env={"PUBLIC_APP_URL": "https://app.example.com"},
        )
    )
    assert res2.reconcile_report.ok is True
    with tarfile.open(tar_path, "r:gz") as tf:
        assert "_minio_manifest.json" in tf.getnames()
        mdata = json.loads(tf.extractfile("_minio_manifest.json").read().decode("utf-8"))
        assert len(mdata["objects"]) == len(res2.manifest_objects)


# ---------------------------------------------------------------------------
# 3. backup_lib.py: preflight-disk-space, verify-mount, orphan reaping
# ---------------------------------------------------------------------------


def test_backup_lib_preflight_disk_space_and_verify_mount_layout(tmp_path: Path) -> None:
    ext_mount = tmp_path / "EveSSD"
    colima_real = ext_mount / "Dalton" / "colima"
    (colima_real / "_lima" / "colima").mkdir(parents=True)
    (colima_real / "_lima" / "colima" / "diffdisk").write_bytes(b"qcow2")
    colima_link = tmp_path / "home" / ".colima"
    colima_link.parent.mkdir(parents=True)
    colima_link.symlink_to(colima_real)

    internal_root = tmp_path / "internal_root"
    internal_root.mkdir()
    gb = 1024 * 1024 * 1024

    def _usage(ext_gb: int, int_gb: int):
        def _fn(p: Path):
            if p == ext_mount:
                return shutil._ntuple_diskusage(500 * gb, 100 * gb, ext_gb * gb)
            return shutil._ntuple_diskusage(100 * gb, 50 * gb, int_gb * gb)
        return _fn

    # All gates pass
    ok_rep = backup_lib.preflight_disk_space(
        external_mount_root=ext_mount,
        internal_root=internal_root,
        colima_dir=colima_link,
        is_mount_fn=lambda p: p == ext_mount,
        disk_usage_fn=_usage(200, 20),
        colima_docker_free_bytes_fn=lambda: 80 * gb,
    )
    assert ok_rep.ok is True
    assert all(c.ok for c in ok_rep.checks)

    # Colima on internal disk '/' -> fails colima_location gate
    colima_internal = tmp_path / "home" / ".colima_local"
    colima_internal.mkdir()
    bad_colima = backup_lib.preflight_disk_space(
        external_mount_root=ext_mount,
        internal_root=internal_root,
        colima_dir=colima_internal,
        is_mount_fn=lambda p: p == ext_mount,
        disk_usage_fn=_usage(200, 20),
        colima_docker_free_bytes_fn=lambda: 80 * gb,
    )
    assert bad_colima.ok is False
    assert any(c.name == "colima_location" and not c.ok for c in bad_colima.checks)

    # Below thresholds on each of the 3 gates
    low_all = backup_lib.preflight_disk_space(
        external_mount_root=ext_mount,
        internal_root=internal_root,
        colima_dir=colima_link,
        is_mount_fn=lambda p: p == ext_mount,
        disk_usage_fn=_usage(149, 9),
        colima_docker_free_bytes_fn=lambda: 54 * gb,
    )
    assert low_all.ok is False
    failed_names = {c.name for c in low_all.checks if not c.ok}
    assert failed_names == {"external_ssd_free", "internal_root_free", "colima_docker_free"}

    # parse_df_available_bytes
    df_out = (
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "/dev/vda1 300000000 100000000 62914560 34% /var/lib/docker\n"
    )
    assert backup_lib.parse_df_available_bytes(df_out) == 62914560 * 1024

    # verify-mount standard layout ({pg,frozen,minio-mirror,asr-jobs,sets,.tmp})
    backups_root = ext_mount / "Dalton" / "backups"
    checked = backup_lib.verify_backup_mount_layout(
        ext_mount, backups_root, is_mount_fn=lambda p: p == ext_mount
    )
    assert len(checked) == 1 + len(backup_lib.STANDARD_BACKUP_SUBDIRS)

    # Escaping symlink inside backups_root MUST fail verify-mount
    backups_root.mkdir(parents=True)
    (backups_root / "pg").symlink_to(internal_root)
    with pytest.raises(backup_lib.MountCheckError):
        backup_lib.verify_backup_mount_layout(
            ext_mount, backups_root, is_mount_fn=lambda p: p == ext_mount
        )

    # Orphan selection > 48h
    now = dt.datetime(2026, 11, 27, 12, 0, tzinfo=dt.timezone.utc)
    orphans = backup_lib.select_orphan_objects_to_reap(
        [
            {"Key": "blobs/u1/reg", "LastModified": now - dt.timedelta(hours=100)},
            {"Key": "blobs/u1/old_unreg", "LastModified": now - dt.timedelta(hours=72)},
            {"Key": "blobs/u1/fresh_unreg", "LastModified": now - dt.timedelta(hours=12)},
        ],
        registered_keys={"blobs/u1/reg"},
        older_than_hours=48,
        now=now,
    )
    assert orphans == ["blobs/u1/old_unreg"]


# ---------------------------------------------------------------------------
# 4. Post-restore inflight state convergence + Transcription resubmit API
# ---------------------------------------------------------------------------


def test_reconcile_restored_inflight_states_external_io_outside_tx() -> None:
    good_sha = _sha(b"assembled-chat-file")
    tx_state_during_inspect: list[bool] = []
    promoted_ids: list[str] = []

    def _handler(sql: str, params: Any) -> Any:
        s = " ".join(sql.split())
        if s.startswith("SELECT to_regclass"):
            return (params[0],)
        if "SELECT upload_id, object_key, size_bytes" in s:
            return [
                ("up-match", "chat-uploads/ws/up-match/f.bin", 19, good_sha, None),
                ("up-missing", "chat-uploads/ws/up-missing/f.bin", 50, "a" * 64, None),
            ]
        if "SELECT upload_id FROM dalton_chat_file_uploads" in s and "FOR UPDATE" in s:
            return [(params[0],)]
        if "UPDATE dalton_chat_file_uploads SET state = 'ready'" in s:
            promoted_ids.append(params[1])
            return 1
        if "UPDATE dalton_chat_file_uploads SET state = 'aborted'" in s:
            return 2
        if "UPDATE drive_uploads" in s:
            return 3
        if "UPDATE chat_attachments" in s:
            return 1
        if "UPDATE dalton_transcription_jobs SET status = 'cancelled'" in s:
            return 1
        if "UPDATE dalton_transcription_jobs SET status = 'failed'" in s:
            assert params[0] == consistent_backup.RESTORED_INFLIGHT_ERROR_MESSAGE
            return 2
        return 0

    conn = _FakeAsyncConn(_handler)

    def _inspect(key: str) -> tuple[int, str] | None:
        # Must NEVER hold a DB transaction/lock while doing external S3 IO!
        tx_state_during_inspect.append(conn.in_tx)
        if key == "chat-uploads/ws/up-match/f.bin":
            return (19, good_sha)
        return None

    summary = asyncio.run(
        consistent_backup.reconcile_restored_inflight_states(
            conn, inspect_chat_upload_object_fn=_inspect
        )
    )
    assert tx_state_during_inspect == [False, False]
    assert promoted_ids == ["up-match"]
    assert summary.drive_uploads_aborted == 3
    assert summary.chat_attachments_aborted == 1
    assert summary.chat_uploads_promoted_ready == 1
    assert summary.chat_uploads_aborted == 2
    assert summary.transcriptions_cancelled == 1
    assert summary.transcriptions_failed == 2


def test_transcription_resubmit_real_endpoint_to_result_and_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Verify capability registration for the new endpoint
    assert (
        DALTON_ROUTE_CAPABILITY_REGISTRY[("transcriptions.py", "resubmit_transcription_endpoint")]
        is CapabilityId.TRANSCRIPTION
    )

    audio_bytes = b"RIFF-archived-audio-bytes"
    audio_sha = _sha(audio_bytes)
    old_job_id = "11111111-1111-1111-1111-111111111111"
    source_key = f"transcriptions/ws-1/{old_job_id}/source/call.mp3"

    # In-memory DB ledger backed by real AsrJobManager + FakeAsrWorker
    jobs_store: dict[str, dict[str, Any]] = {
        old_job_id: {
            "job_id": old_job_id,
            "created_by_user_id": "user-internal-1",
            "workspace_id": "ws-1",
            "thread_id": "th-1",
            "source_object_key": source_key,
            "source_size_bytes": len(audio_bytes),
            "source_sha256": audio_sha,
            "language": "zh",
            "hotwords": ["Dalton"],
            "status": "failed",
            "stage": "failed",
            "segment_count": 0,
            "completed_segments": 0,
            "provider_request_ids": [],
            "usage": {"upload": {"completed": True}},
            "result_object_key": None,
            "error_category": "restored_inflight",
            "error_message": consistent_backup.RESTORED_INFLIGHT_ERROR_MESSAGE,
            "idempotency_key": "orig-idemp-key",
            "version": 2,
            "created_at": "2026-11-27T01:00:00+00:00",
            "updated_at": "2026-11-27T02:00:00+00:00",
            "completed_at": "2026-11-27T02:00:00+00:00",
        }
    }

    async def _fake_get_job(jid: str, *, conn: Any = None) -> dict[str, Any] | None:
        row = jobs_store.get(jid)
        return dict(row) if row else None

    async def _fake_create_job(**kwargs: Any) -> dict[str, Any]:
        # Enforce unique idempotency_key (must NOT reuse orig-idemp-key!)
        for existing in jobs_store.values():
            if existing["idempotency_key"] == kwargs["idempotency_key"]:
                return dict(existing)
        row = {
            "job_id": str(kwargs["job_id"]),
            "created_by_user_id": kwargs["created_by_user_id"],
            "workspace_id": kwargs["workspace_id"],
            "thread_id": kwargs.get("thread_id") or "",
            "source_object_key": kwargs["source_object_key"],
            "source_size_bytes": int(kwargs["source_size_bytes"]),
            "source_sha256": kwargs["source_sha256"],
            "language": kwargs.get("language"),
            "hotwords": list(kwargs.get("hotwords") or []),
            "status": str(kwargs.get("status", "awaiting_upload")),
            "stage": str(kwargs.get("stage", "awaiting_upload")),
            "segment_count": 0,
            "completed_segments": 0,
            "provider_request_ids": [],
            "usage": {},
            "result_object_key": None,
            "error_category": None,
            "error_message": None,
            "idempotency_key": kwargs["idempotency_key"],
            "version": 1,
            "created_at": "2026-11-27T03:00:00+00:00",
            "updated_at": "2026-11-27T03:00:00+00:00",
            "completed_at": None,
        }
        jobs_store[row["job_id"]] = row
        return dict(row)

    async def _fake_update_versioned(job_id: str, *, expected_version: int, **kwargs: Any):
        row = jobs_store.get(job_id)
        if row is None or int(row["version"]) != int(expected_version):
            return False, dict(row) if row else None
        for k in (
            "status",
            "stage",
            "segment_count",
            "completed_segments",
            "provider_request_ids",
            "usage",
            "result_object_key",
            "error_category",
            "error_message",
        ):
            if kwargs.get(k) is not None:
                row[k] = kwargs[k].value if hasattr(kwargs[k], "value") else kwargs[k]
        row["version"] = int(row["version"]) + 1
        return True, dict(row)

    mgr = AsrJobManager(
        jobs_dir=tmp_path / "restored_asr_jobs",
        worker=FakeAsrWorker(),
    )

    async def _bridge_submit(payload: dict[str, Any]):
        return await mgr.submit_job(payload)

    async def _bridge_get(jid: str):
        return await mgr.get_job(jid)

    async def _bridge_result(jid: str):
        return await mgr.get_job_result(jid)

    async def _bridge_raw_stream(jid: str):
        code, path_or_err = await mgr.get_job_raw_result(jid)
        if code != 200 or not isinstance(path_or_err, Path):
            return code, path_or_err
        raw_bytes = path_or_err.read_bytes()

        async def _aiter():
            yield raw_bytes

        return 200, _aiter()

    monkeypatch.setattr(
        tr_db, "get_transcription_job", create_autospec(tr_db.get_transcription_job, side_effect=_fake_get_job)
    )
    monkeypatch.setattr(
        tr_db, "create_transcription_job", create_autospec(tr_db.create_transcription_job, side_effect=_fake_create_job)
    )
    monkeypatch.setattr(
        tr_db,
        "update_transcription_job_versioned",
        create_autospec(tr_db.update_transcription_job_versioned, side_effect=_fake_update_versioned),
    )
    monkeypatch.setattr(
        tr_svc,
        "require_internal_transcription_caller",
        create_autospec(tr_svc.require_internal_transcription_caller, side_effect=lambda u: u),
    )
    monkeypatch.setattr(
        tr_svc,
        "authorize_job_access",
        create_autospec(tr_svc.authorize_job_access, return_value=None),
    )
    monkeypatch.setattr(
        tr_svc,
        "is_transcription_storage_configured",
        create_autospec(tr_svc.is_transcription_storage_configured, return_value=True),
    )
    monkeypatch.setattr(
        tr_svc,
        "object_size",
        create_autospec(tr_svc.object_size, side_effect=lambda k: len(audio_bytes) if k == source_key else None),
    )
    monkeypatch.setattr(
        tr_svc,
        "call_bridge_submit_transcription",
        create_autospec(tr_svc.call_bridge_submit_transcription, side_effect=_bridge_submit),
    )
    monkeypatch.setattr(
        tr_svc,
        "call_bridge_get_transcription",
        create_autospec(tr_svc.call_bridge_get_transcription, side_effect=_bridge_get),
    )
    monkeypatch.setattr(
        tr_svc,
        "call_bridge_get_transcription_result",
        create_autospec(tr_svc.call_bridge_get_transcription_result, side_effect=_bridge_result),
    )
    monkeypatch.setattr(
        tr_svc,
        "open_bridge_raw_result_stream",
        create_autospec(tr_svc.open_bridge_raw_result_stream, side_effect=_bridge_raw_stream),
    )

    # Enable router gates on a real FastAPI app
    monkeypatch.setattr(
        transcriptions_app,
        "is_transcription_enabled",
        create_autospec(tr_svc.is_transcription_enabled, return_value=True),
    )
    monkeypatch.setattr(
        transcriptions_app,
        "get_dalton_settings",
        create_autospec(transcriptions_app.get_dalton_settings, return_value=type("S", (), {"enabled": True})()),
    )
    monkeypatch.setattr(
        transcriptions_app,
        "admit_caller",
        create_autospec(transcriptions_app.admit_caller, return_value=None),
    )

    app = FastAPI()
    app.include_router(transcriptions_app.router)
    app.dependency_overrides[transcriptions_app.get_current_user_id] = lambda: "user-internal-1"

    with TestClient(app) as client:
        # 1. Resubmit the restored-failed job -> creates a brand-new job_id & idempotency_key
        resp = client.post(f"/api/v1/dalton/transcriptions/{old_job_id}/resubmit")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["resubmitted_from_job_id"] == old_job_id
        new_job = body["job"]
        new_job_id = new_job["job_id"]
        assert new_job_id != old_job_id
        assert new_job["idempotency_key"] != "orig-idemp-key"
        assert new_job["source_object_key"] == source_key
        # Original failed row remains intact
        assert jobs_store[old_job_id]["status"] == "failed"

        # 2. Poll real status endpoint until FakeAsrWorker completes
        for _ in range(50):
            st_resp = client.get(f"/api/v1/dalton/transcriptions/{new_job_id}")
            assert st_resp.status_code == 200
            if st_resp.json()["job"]["status"] == "succeeded":
                break
        assert jobs_store[new_job_id]["status"] == "succeeded"

        # 3. Read real /result and /result/raw endpoints -> 200
        res_resp = client.get(f"/api/v1/dalton/transcriptions/{new_job_id}/result")
        assert res_resp.status_code == 200
        assert res_resp.json()["result"]["job_id"] == new_job_id

        raw_resp = client.get(f"/api/v1/dalton/transcriptions/{new_job_id}/result/raw")
        assert raw_resp.status_code == 200
        assert raw_resp.json()["job_id"] == new_job_id

        # 4. Succeeded job cannot be resubmitted (409)
        dup_resp = client.post(f"/api/v1/dalton/transcriptions/{new_job_id}/resubmit")
        assert dup_resp.status_code == 409

        # 5. Non-creator gets 404
        app.dependency_overrides[transcriptions_app.get_current_user_id] = lambda: "other-user"
        other_resp = client.post(f"/api/v1/dalton/transcriptions/{old_job_id}/resubmit")
        assert other_resp.status_code == 404


# ---------------------------------------------------------------------------
# 5. Everflow prod scripts, thin forwarders, launchd templates, deploy_main, standalone
# ---------------------------------------------------------------------------


def test_local_backup_set_publish_and_tiered_pool_pruning(tmp_path: Path) -> None:
    backups_root = tmp_path / "backups"
    pool_dir = backups_root / "minio-mirror" / "objects"

    pool_sizes: dict[str, int] = {}

    def _seed_pool(data: bytes) -> str:
        sha = _sha(data)
        p = pool_dir / sha[:2] / sha
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        pool_sizes[sha] = len(data)
        return sha

    sha_shared = _seed_pool(b"shared-across-sets")
    sha_old_only = _seed_pool(b"old-daily-only")
    sha_manual_only = _seed_pool(b"frozen-manual-only")
    sha_orphan = _seed_pool(b"unreferenced-orphan")

    # Publish 3 daily sets (retain_daily=2 -> oldest pruned) + 1 manual-pre-blob-inline set (never pruned!)
    for idx, (sid, tag, shas) in enumerate(
        [
            ("20261124-030000", "daily", [sha_old_only, sha_shared]),
            ("20261125-030000", "daily", [sha_shared]),
            ("20261126-030000", "daily", [sha_shared]),
            ("20261126-120000", "manual-pre-blob-inline", [sha_manual_only]),
        ]
    ):
        staged = tmp_path / f"stage-{idx}.dump"
        staged.write_bytes(f"PGDMP-{sid}".encode())
        recs = [
            consistent_backup.ArchivedObjectRecord(
                bucket="dalton-main",
                key=f"blobs/u1/{s}",
                version_id="v1",
                size=pool_sizes[s],
                archived_sha256=s,
            )
            for s in shas
        ]
        local_backup_set.publish_local_backup_set(
            backups_root=backups_root,
            backup_set_id=sid,
            tag=tag,
            snapshot_id=f"snap-{idx}",
            staged_dump_path=staged,
            manifest_objects=recs,
            asr_summary=None,
            reconcile_ok=True,
            created_at=f"2026-11-2{4 + idx // 2}T0{idx}:00:00+00:00",
        )

    assert (backups_root / "frozen" / "dalton-pg-manual-pre-blob-inline-20261126-120000.dump").is_file()
    latest = json.loads((backups_root / "sets" / "latest-verified-set.json").read_text(encoding="utf-8"))
    assert latest["backup_set_id"] == "20261126-120000"

    stats = local_backup_set.prune_local_backup_sets_and_pool(
        backups_root, retain_daily=2, retain_weekly=8, retain_deploy=5
    )
    assert stats["pruned_sets"] == 1
    assert stats["surviving_sets"] == 3
    # Pool objects: shared + manual survive; old_only + orphan deleted!
    assert (pool_dir / sha_shared[:2] / sha_shared).is_file()
    assert (pool_dir / sha_manual_only[:2] / sha_manual_only).is_file()
    assert not (pool_dir / sha_old_only[:2] / sha_old_only).exists()
    assert not (pool_dir / sha_orphan[:2] / sha_orphan).exists()


def test_offsite_sync_weekly_baseline_daily_delta_and_baseline_prune_exemption(tmp_path: Path) -> None:
    s3 = _FakeS3Client(versioning_by_bucket={"dalton-main": True})
    base_bytes = b"object-in-weekly-baseline"
    base_sha = _sha(base_bytes)
    new_bytes = b"object-added-after-weekly"
    new_sha = _sha(new_bytes)
    s3.seed_object("dalton-main", f"blobs/u1/{base_sha}", base_bytes)

    baseline_tar = tmp_path / "weekly-minio.tar.gz"
    baseline_manifest = tmp_path / "sets" / "weekly-baseline-2026-11-22.json"
    records, payload = offsite_sync.build_weekly_minio_baseline(
        bucket_specs=[consistent_backup.BucketArchiveSpec(bucket="dalton-main", roles=("main",), s3_client=s3)],
        baseline_id="2026-11-22",
        out_tar_path=baseline_tar,
        manifest_out_path=baseline_manifest,
    )
    assert len(records) == 1
    assert payload["baseline_id"] == "2026-11-22"

    # Mirror pool holds both objects; daily manifest references both -> delta contains ONLY new_bytes!
    mirror_objs = tmp_path / "mirror" / "objects"
    for data, sha in ((base_bytes, base_sha), (new_bytes, new_sha)):
        p = mirror_objs / sha[:2] / sha
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    daily_objs = [
        {"bucket": "dalton-main", "key": f"blobs/u1/{base_sha}", "size": len(base_bytes), "archived_sha256": base_sha},
        {"bucket": "dalton-main", "key": f"blobs/u1/{new_sha}", "size": len(new_bytes), "archived_sha256": new_sha},
    ]
    delta = offsite_sync.compute_daily_minio_delta(daily_objs, payload["objects"])
    assert [d["archived_sha256"] for d in delta] == [new_sha]

    delta_tar = tmp_path / "minio-delta.tar.gz"
    size, _sha_tar, cnt = offsite_sync.build_daily_minio_delta_tar(
        delta, mirror_objects_dir=mirror_objs, out_tar_path=delta_tar
    )
    assert size > 0
    assert cnt == 1

    # Restore using weekly baseline tar + daily delta tar indexed by archived_sha256
    idx = consistent_backup.build_archived_sha256_index(tar_archives=[baseline_tar, delta_tar])
    dst_s3 = _FakeS3Client(versioning_by_bucket={"dalton-main": True})
    restored = consistent_backup.restore_objects_from_manifest(
        daily_objs, sha_index=idx, s3_clients_by_bucket={"dalton-main": dst_s3}
    )
    assert restored["dalton-main"] == {f"blobs/u1/{base_sha}", f"blobs/u1/{new_sha}"}

    # Weekly pruning MUST exempt baseline_id still referenced by a retained daily LATEST.json!
    to_prune = offsite_sync.select_weekly_baselines_to_prune(
        ["2026-10-25", "2026-11-01", "2026-11-08", "2026-11-15", "2026-11-22"],
        retain_weeks=2,
        referenced_baseline_ids=["2026-11-01"],
    )
    assert to_prune == ["2026-10-25", "2026-11-08"]


def test_thin_forwarders_launchd_templates_deploy_main_compat_and_standalone_wrapper(
    tmp_path: Path,
) -> None:
    # 1. Thin forwarders exec into prod/
    db_fwd = (OPS_DIR / "dalton_db_offsite.sh").read_text(encoding="utf-8")
    data_fwd = (OPS_DIR / "dalton_data_backup.sh").read_text(encoding="utf-8")
    assert 'exec "${OPS_DIR}/prod/db_offsite.sh" "$@"' in db_fwd
    assert 'exec "${OPS_DIR}/prod/data_backup.sh" "$@"' in data_fwd

    # 2. All three launchd templates are valid XML plists referencing external mount & prod scripts
    pg_plist = plistlib.loads(
        (PROD_DIR / "launchd" / "com.dalton.pg-backup.plist.template").read_bytes()
    )
    assert pg_plist["ProgramArguments"][1].endswith("/scripts/dalton/ops/prod/backup.sh")
    assert pg_plist["EnvironmentVariables"]["DALTON_BACKUP_ROOT"] == "/Volumes/EveSSD/Dalton/backups"
    assert pg_plist["EnvironmentVariables"]["DALTON_BACKUP_DIR"] == "/Volumes/EveSSD/Dalton/backups/pg"

    db_plist = plistlib.loads(
        (OPS_DIR / "launchd" / "com.dalton.db-offsite.plist.template").read_bytes()
    )
    assert db_plist["ProgramArguments"][1].endswith("/scripts/dalton/ops/prod/db_offsite.sh")
    assert db_plist["EnvironmentVariables"]["DALTON_EXTERNAL_MOUNT_ROOT"] == "/Volumes/EveSSD"

    data_plist = plistlib.loads(
        (OPS_DIR / "launchd" / "com.dalton.data-backup.plist.template").read_bytes()
    )
    assert data_plist["ProgramArguments"][1].endswith("/scripts/dalton/ops/prod/data_backup.sh")

    # 3. deploy_main.sh compatibility with new prod/backup.sh log format ("... ok  4.2G <dump> set=<id>")
    log_file = tmp_path / "dalton-pg-backup.log"
    line = "2026-11-27 03:05:12 ok  4.2G /Volumes/EveSSD/Dalton/backups/pg/dalton-pg-20261127-030000.dump set=20261127-030000"
    log_file.write_text(line + "\n", encoding="utf-8")
    now_epoch = int(
        subprocess.check_output(
            [
                "bash",
                "-c",
                "date -j -f '%Y-%m-%d %H:%M:%S' '2026-11-27 03:20:00' +%s 2>/dev/null "
                "|| date -d '2026-11-27 03:20:00' +%s",
            ],
            text=True,
        ).strip()
    )
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'DALTON_DEPLOY_LIB_ONLY=1; source "{PROD_DIR}/deploy_main.sh"; '
            f'recent_ok_backup "{log_file}" 60 {now_epoch}; '
            f'_is_terminal_pg_backup_line "[backup] exporting snapshot" && exit 9; '
            f'_is_terminal_pg_backup_line "{line}"',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "set=20261127-030000" in proc.stdout

    # 4. Standalone Core wrapper resolves mixed external main + built-in aux buckets cleanly
    fake_clients: list[dict[str, Any]] = []

    def _fake_factory(**kw: Any) -> Any:
        fake_clients.append(kw)
        return _FakeS3Client()

    specs = standalone_cb.resolve_standalone_bucket_specs(
        env={
            "DALTON_PROFILE": "core",
            "STORAGE_PROVIDER": "s3",
            "STORAGE_ENDPOINT_URL": "https://s3.external.example",
            "STORAGE_BUCKET_NAME": "corp-main",
            "STORAGE_ACCESS_KEY_ID": "ak1",
            "STORAGE_SECRET_ACCESS_KEY": "sk1",
            "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL": "http://minio:9000",
            "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME": "chat-uploads",
            "DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID": "ak2",
            "DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY": "sk2",
        },
        client_factory=_fake_factory,
    )
    assert len(specs) == 2
    assert {(s.bucket, s.roles) for s in specs} == {
        ("corp-main", ("main",)),
        ("chat-uploads", ("chat_upload",)),
    }


def test_prod_db_offsite_backup_set_mode_subprocess_e2e(tmp_path: Path) -> None:
    """End-to-end subprocess test of prod/db_offsite.sh in backup-set mode (with fake rclone)."""
    from tests.unit.dalton.test_offsite_backup import (
        _make_mounted_python_wrapper,
        _write_fake_rclone,
        _write_test_env,
    )

    home_dir = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    drive_store = tmp_path / "drive_store"
    ext_mount = tmp_path / "ext_ssd"
    for d in (home_dir, fake_bin, drive_store, ext_mount):
        d.mkdir(parents=True)

    backups_root = ext_mount / "Dalton" / "backups"
    sets_dir = backups_root / "sets"
    pg_dir = backups_root / "pg"
    asr_dir = backups_root / "asr-jobs"
    mirror_objs = backups_root / "minio-mirror" / "objects"
    for d in (sets_dir, pg_dir, asr_dir, mirror_objs):
        d.mkdir(parents=True)

    dump_bytes = b"PGDMP-consistent-set-payload"
    dump_path = pg_dir / "dalton-pg-20261127-030000.dump"
    dump_path.write_bytes(dump_bytes)

    asr_tar = asr_dir / "asr-jobs-20261127-030000.tar.gz"
    with tarfile.open(asr_tar, "w:gz") as tf:
        info = tarfile.TarInfo("asr_usage.jsonl")
        info.size = 3
        tf.addfile(info, io.BytesIO(b"{}\n"))

    obj_bytes = b"delta-only-blob"
    obj_sha = _sha(obj_bytes)
    obj_path = mirror_objs / obj_sha[:2] / obj_sha
    obj_path.parent.mkdir(parents=True)
    obj_path.write_bytes(obj_bytes)

    manifest_path = sets_dir / "minio-manifest-20261127-030000.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 2,
                "objects": [
                    {
                        "bucket": "dalton-main",
                        "key": f"blobs/u1/{obj_sha}",
                        "size": len(obj_bytes),
                        "archived_sha256": obj_sha,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (sets_dir / "weekly-baseline-2026-11-22.json").write_text(
        json.dumps({"version": 2, "baseline_id": "2026-11-22", "objects": []}),
        encoding="utf-8",
    )
    latest_set = sets_dir / "latest-verified-set.json"
    latest_set.write_text(
        json.dumps(
            {
                "version": 1,
                "backup_set_id": "20261127-030000",
                "tag": "daily",
                "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "dump_path": str(dump_path),
                "manifest_path": str(manifest_path),
                "asr_archive_path": str(asr_tar),
                "reconcile_ok": True,
            }
        ),
        encoding="utf-8",
    )

    key_file = home_dir / ".config" / "dalton" / "backup.key"
    alerts_file = home_dir / "Library" / "Logs" / "dalton-offsite-backup-alerts.log"
    log_file = home_dir / "Library" / "Logs" / "dalton-offsite-backup.log"
    env_file = tmp_path / "offsite-backup.env"
    about_file = tmp_path / "about.json"
    about_file.write_text('{"free": 1000000000}\n', encoding="utf-8")

    _write_test_env(
        env_file,
        home_dir=home_dir,
        repo_dir=tmp_path / "repo",
        key_file=key_file,
        alerts_file=alerts_file,
        log_file=log_file,
        prod_checkout=tmp_path / "prod",
        prod_var=tmp_path / "prod" / "var",
        user_config=home_dir / ".config" / "dalton",
        launchagents=home_dir / "Library" / "LaunchAgents",
        backup_scripts=pg_dir,
        colima=home_dir / ".colima",
        ext_mount=ext_mount,
    )
    with env_file.open("a", encoding="utf-8") as fh:
        fh.write(f'DALTON_BACKUP_ROOT="{backups_root}"\n')
        fh.write('DALTON_RCLONE_REMOTE="gdrive"\n')
        fh.write('DALTON_RCLONE_DRIVE_DIR="DaltonBackup"\n')

    _write_fake_rclone(
        fake_bin,
        drive_store=drive_store,
        about_file=about_file,
        proxy_check_file=tmp_path / "proxy_seen.txt",
        rcat_sizes_log=tmp_path / "rcat_sizes.jsonl",
    )
    mounted_py = _make_mounted_python_wrapper(tmp_path, ext_mount)

    res = subprocess.run(
        ["bash", str(OPS_DIR / "dalton_db_offsite.sh")],
        env={
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(home_dir),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_file),
            "DALTON_REPO_PYTHON": str(mounted_py),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr

    today_dir = drive_store / "DaltonBackup" / "db" / dt.datetime.now().strftime("%Y-%m-%d")
    latest_remote = today_dir / "LATEST.json"
    assert latest_remote.is_file()
    latest_data = json.loads(latest_remote.read_text(encoding="utf-8"))
    assert latest_data["backup_set_id"] == "20261127-030000"
    assert latest_data["baseline_id"] == "2026-11-22"
    for art_key in ("dump", "minio_manifest", "minio_delta", "asr_jobs"):
        fname = latest_data["artifacts"][art_key]["filename"]
        assert (today_dir / fname).is_file()
    assert list(today_dir.glob("*.staged-*")) == []


# ---------------------------------------------------------------------------
# 6. Round 2 review regression tests (covering every High & Medium finding)
# ---------------------------------------------------------------------------


def test_default_cli_reconcile_uses_compute_stored_object_digest_ready_branch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Codex #1 / Lead #4: default CLI calls compute_stored_object_digest (not missing sha256_object)."""
    from src.dalton import chat_upload_storage
    from src.server.database import pool as db_pool

    payload = b"assembled-multipart-chat-upload-payload"
    digest = _sha(payload)
    promoted: list[str] = []

    def _db_handler(sql: str, params: Any) -> Any:
        s = " ".join(sql.split())
        if s.startswith("SELECT to_regclass"):
            return (params[0],)
        if "SELECT upload_id, object_key, size_bytes" in s:
            return [("up-ready-1", "private-media/chat-uploads/ws1/up-ready-1/a.bin", len(payload), digest, None)]
        if "SELECT upload_id FROM dalton_chat_file_uploads" in s and "FOR UPDATE" in s:
            return [(params[0],)]
        if "UPDATE dalton_chat_file_uploads SET state = 'ready'" in s:
            promoted.append(params[1])
            return 1
        return 0

    conn = _FakeAsyncConn(_db_handler)

    async def _fake_open() -> Any:
        return conn

    class _FakeBody:
        def __init__(self, data: bytes) -> None:
            self._stream = io.BytesIO(data)

        def read(self, n: int = -1) -> bytes:
            return self._stream.read(n)

        def close(self) -> None:
            self._stream.close()

    class _FakeClient:
        def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
            assert Bucket == "dalton-asr"
            assert Key == "private-media/chat-uploads/ws1/up-ready-1/a.bin"
            return {"Body": _FakeBody(payload)}

    monkeypatch.setattr(
        db_pool,
        "open_session_connection",
        create_autospec(db_pool.open_session_connection, side_effect=_fake_open),
    )
    monkeypatch.setattr(
        chat_upload_storage,
        "is_chat_upload_storage_configured",
        create_autospec(chat_upload_storage.is_chat_upload_storage_configured, return_value=True),
    )
    monkeypatch.setattr(
        chat_upload_storage,
        "_get_client",
        create_autospec(chat_upload_storage._get_client, return_value=_FakeClient()),
    )
    monkeypatch.setattr(
        chat_upload_storage,
        "_bucket_name",
        create_autospec(chat_upload_storage._bucket_name, return_value="dalton-asr"),
    )
    monkeypatch.setattr(
        chat_upload_storage,
        "resolve_full_key",
        create_autospec(chat_upload_storage.resolve_full_key, side_effect=lambda k: k),
    )

    rc = consistent_backup.main(["reconcile-restored-inflight-states"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["chat_uploads_promoted_ready"] == 1
    assert promoted == ["up-ready-1"]


def test_hold_lock_timeout_and_early_snapshot_commit_terminates_pg_dump(tmp_path: Path) -> None:
    """Codex #2 + Grok #1 / Lead #1: early snapshot commit, hard deadline kills pg_dump & releases locks."""
    events: list[str] = []

    def _empty_db(sql: str, params: Any) -> Any:
        s = " ".join(sql.split())
        if s.startswith("SELECT pg_export_snapshot"):
            return ("00000009-00000010-1",)
        if s.startswith("SELECT pg_advisory_lock"):
            events.append("lock")
            return (True, True)
        if s.startswith("SELECT pg_advisory_unlock"):
            events.append("unlock")
            return (True, True)
        if s == "COMMIT":
            events.append("commit")
            return None
        if s == "ROLLBACK":
            events.append("rollback")
            return None
        if s.startswith("SELECT to_regclass"):
            return (None,)
        return []

    # Case 1: pg_dump emits initial bytes then pauses briefly -> COMMIT fires BEFORE pg_dump exits,
    # and GC advisory locks are released BEFORE versioned mirror download runs!
    s3 = _FakeS3Client(versioning_by_bucket={"dalton-main": True})
    s3.seed_object("dalton-main", "blobs/u1/k1", b"mirror-bytes")
    orig_get = s3.get_object

    def _tracked_get(*args: Any, **kwargs: Any) -> Any:
        events.append("mirror_get")
        return orig_get(*args, **kwargs)

    s3.get_object = _tracked_get  # type: ignore[method-assign]

    dump_out = tmp_path / "early-commit.dump"

    def _slow_finish_pg_dump(snap_id: str, out_path: Path) -> consistent_backup.PgDumpProcess:
        assert snap_id == "00000009-00000010-1"
        cmd = [
            sys.executable,
            "-c",
            "import sys, time; sys.stdout.buffer.write(b'PGDMP-hdr'); sys.stdout.buffer.flush(); "
            "time.sleep(0.25); sys.stdout.buffer.write(b'-tail'); sys.stdout.buffer.flush()",
        ]
        return consistent_backup.start_pg_dump_process(cmd, out_path, cmd_label="fake_pg_dump")

    conn1 = _FakeAsyncConn(_empty_db)
    sink1 = consistent_backup.MirrorPoolSink(tmp_path / "mirror1")
    res1 = asyncio.run(
        consistent_backup.run_consistent_backup(
            conn=conn1,
            pg_dump_path=dump_out,
            pg_dump_fn=_slow_finish_pg_dump,
            bucket_specs=[
                consistent_backup.BucketArchiveSpec(bucket="dalton-main", roles=("main",), s3_client=s3)
            ],
            object_sink=sink1,
            snapshot_import_timeout_s=5.0,
            pg_dump_timeout_s=5.0,
            lock_phase_timeout_s=5.0,
            mirror_timeout_s=5.0,
        )
    )
    assert res1.reconcile_report.ok is True
    assert dump_out.read_bytes() == b"PGDMP-hdr-tail"
    assert events.index("lock") < events.index("commit") < events.index("unlock") < events.index("mirror_get")

    # Case 2: hanging pg_dump hits snapshot_import_timeout_s -> terminates child, ROLLBACKs, unlocks!
    events.clear()
    spawned: list[consistent_backup.PgDumpProcess] = []

    def _hanging_pg_dump(_snap_id: str, out_path: Path) -> consistent_backup.PgDumpProcess:
        proc = consistent_backup.start_pg_dump_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            out_path,
            cmd_label="hanging_pg_dump",
        )
        spawned.append(proc)
        return proc

    conn2 = _FakeAsyncConn(_empty_db)
    sink2 = consistent_backup.MirrorPoolSink(tmp_path / "mirror2")
    with pytest.raises(consistent_backup.BackupTimeoutError):
        asyncio.run(
            consistent_backup.run_consistent_backup(
                conn=conn2,
                pg_dump_path=tmp_path / "hang.dump",
                pg_dump_fn=_hanging_pg_dump,
                bucket_specs=[],
                object_sink=sink2,
                snapshot_import_timeout_s=0.2,
                pg_dump_timeout_s=0.4,
                lock_phase_timeout_s=0.4,
            )
        )
    assert spawned and spawned[0].popen.poll() is not None
    assert "rollback" in events
    assert "unlock" in events


def test_response_images_private_media_and_percent_encoded_attachments_reconcile() -> None:
    """Codex #3 / Lead #5: real writer URLs for private-media/response-images & encoded attachments reconcile."""
    from urllib.parse import quote
    from src.ptc_agent.agent.middleware.image_capture import image_storage_key

    env = {
        "PUBLIC_APP_URL": "https://app.example.com",
        "STORAGE_BUCKET_NAME": "dalton-main",
        "STORAGE_PUBLIC_URL_BASE": "https://cdn.example.com",
    }

    # 1. Real writer key for new sandbox response images (`private-media/response-images/...`)
    new_img_key = image_storage_key("thread-42", "a" * 64, "chart_q4.png")
    assert new_img_key.startswith("private-media/response-images/thread-42/")
    new_img_url = f"https://cdn.example.com/{new_img_key}"
    legacy_img_url = "https://app.example.com/storage/response-images/thread-42/0123456789abcdef/old.png"
    foreign_img_url = "https://evil.example.net/private-media/response-images/thread-42/0123456789abcdef/x.png"

    assert storage_refs.extract_response_image_object_key(new_img_url, env=env) == new_img_key
    assert (
        storage_refs.extract_response_image_object_key(legacy_img_url, env=env)
        == "response-images/thread-42/0123456789abcdef/old.png"
    )
    assert storage_refs.extract_response_image_object_key(foreign_img_url, env=env) is None

    # 2. Real writer URL for attachments (`quote(key, safe="/")` with Chinese, spaces, `#`)
    raw_att_key = "private-media/attachments/thread-42/batch1/id1/研报 #1 (终版).pdf"
    encoded_att_url = f"https://cdn.example.com/{quote(raw_att_key, safe='/')}"
    legacy_att_key = "attachments/thread-42/my doc #2.pdf"
    legacy_att_url = f"https://app.example.com/storage/{quote(legacy_att_key, safe='/')}"
    foreign_att_url = f"https://evil.example.net/{quote(raw_att_key, safe='/')}"

    assert storage_refs.extract_attachment_object_key(encoded_att_url, env=env) == raw_att_key
    assert storage_refs.extract_attachment_object_key(legacy_att_url, env=env) == legacy_att_key
    assert storage_refs.extract_attachment_object_key(foreign_att_url, env=env) is None

    # 3. Full reconciliation passes against the REAL unencoded S3 object keys!
    inv = storage_refs.SnapshotRefInventory(
        references=[
            storage_refs.ObjectReference(
                consumer="response_images_turns",
                bucket_role="main",
                object_key=storage_refs.extract_response_image_object_key(new_img_url, env=env) or "",
                row_id="resp-1",
                check_mode="exists_only",
            ),
            storage_refs.ObjectReference(
                consumer="chat_attachment_history",
                bucket_role="main",
                object_key=storage_refs.extract_attachment_object_key(encoded_att_url, env=env) or "",
                row_id="q-1",
                check_mode="exists_only",
            ),
        ]
    )
    manifest = [
        {"bucket": "dalton-main", "key": new_img_key, "size": 12, "archived_sha256": _sha(b"img")},
        {"bucket": "dalton-main", "key": raw_att_key, "size": 34, "archived_sha256": _sha(b"pdf")},
    ]
    report = storage_refs.reconcile_inventory_against_manifest(
        inv, manifest, bucket_targets={"main": ("dalton-main", "")}
    )
    assert report.ok is True


def test_independent_ref_enumeration_fails_on_missing_bucket_and_supports_aws_creds(
    tmp_path: Path,
) -> None:
    """Codex #4 / Lead #5: runtime AWS_* creds work; DB refs without matching bucket fail loudly."""
    seen_kwargs: list[dict[str, Any]] = []

    def _factory(**kw: Any) -> Any:
        seen_kwargs.append(kw)
        return _FakeS3Client()

    specs = consistent_backup.resolve_runtime_bucket_specs(
        env={
            "STORAGE_PROVIDER": "s3",
            "STORAGE_BUCKET_NAME": "prod-aws-bucket",
            "AWS_ACCESS_KEY_ID": "AKIA_TEST",
            "AWS_SECRET_ACCESS_KEY": "SECRET_TEST",
            "AWS_DEFAULT_REGION": "ap-southeast-1",
        },
        client_factory=_factory,
    )
    assert len(specs) == 1
    assert specs[0].bucket == "prod-aws-bucket"
    assert specs[0].roles == ("main",)
    assert seen_kwargs[0]["access_key"] == "AKIA_TEST"
    assert seen_kwargs[0]["secret_key"] == "SECRET_TEST"
    assert seen_kwargs[0]["region"] == "ap-southeast-1"

    # DB has a main-bucket reference, but backup caller passed empty bucket_specs -> MUST raise!
    blob_sha = _sha(b"blob-data")

    def _db_with_main_ref(sql: str, params: Any) -> Any:
        s = " ".join(sql.split())
        if s.startswith("SELECT pg_export_snapshot"):
            return ("00000001-00000002-1",)
        if s.startswith("SELECT pg_advisory_lock") or s.startswith("SELECT pg_advisory_unlock"):
            return (True, True)
        if s.startswith("SELECT to_regclass"):
            return ("public.workspace_file_blobs",) if params[0] == "public.workspace_file_blobs" else (None,)
        if "FROM workspace_file_blobs" in s:
            return [("u1", blob_sha, 9)]
        return []

    conn = _FakeAsyncConn(_db_with_main_ref)
    sink = consistent_backup.MirrorPoolSink(tmp_path / "mirror")
    with pytest.raises(consistent_backup.MissingBucketConfigError, match="main"):
        asyncio.run(
            consistent_backup.run_consistent_backup(
                conn=conn,
                pg_dump_path=tmp_path / "pg.dump",
                pg_dump_fn=lambda _s, p: p.write_bytes(b"PGDMP"),
                bucket_specs=[],
                object_sink=sink,
            )
        )


def test_mirror_pool_rejects_corrupt_same_length_file_locks_and_aborts_prune_on_bad_manifest(
    tmp_path: Path,
) -> None:
    """Codex #5 + Grok #6 / Lead #2: overwrite corrupt same-length pool file, verify bytes before publish, abort prune."""
    backups_root = tmp_path / "backups"
    mirror_root = backups_root / "minio-mirror"
    good_bytes = b"GOOD-BYTES-12345"
    good_sha = _sha(good_bytes)
    corrupt_same_len = b"BAD!-BYTES-12345"
    assert len(corrupt_same_len) == len(good_bytes)

    # 1. Pre-seed corrupt same-length file -> MirrorPoolSink MUST overwrite it with verified bytes
    pool_file = mirror_root / "objects" / good_sha[:2] / good_sha
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_bytes(corrupt_same_len)

    s3 = _FakeS3Client(versioning_by_bucket={"dalton-main": True})
    ver_id = s3.seed_object("dalton-main", "blobs/u1/obj", good_bytes)
    sink = consistent_backup.MirrorPoolSink(mirror_root)
    rec = sink.archive_object(
        s3,
        consistent_backup.SelectedObjectVersion(
            bucket="dalton-main", key="blobs/u1/obj", version_id=ver_id, size=len(good_bytes)
        ),
    )
    assert pool_file.read_bytes() == good_bytes

    # 2. Corrupt pool file AFTER mirroring -> publish_local_backup_set MUST refuse to publish!
    pool_file.write_bytes(corrupt_same_len)
    staged_dump = tmp_path / "staged.dump"
    staged_dump.write_bytes(b"PGDMP-valid")
    with pytest.raises(RuntimeError, match="corrupted"):
        local_backup_set.publish_local_backup_set(
            backups_root=backups_root,
            backup_set_id="20261127-030000",
            tag="daily",
            snapshot_id="snap-bad",
            staged_dump_path=staged_dump,
            manifest_objects=[rec],
            asr_summary=None,
            reconcile_ok=True,
        )
    assert not (backups_root / "sets" / "latest-verified-set.json").exists()

    # 3. Restore valid bytes -> publish 2 sets, corrupt surviving manifest -> prune MUST abort deleting ZERO files!
    pool_file.write_bytes(good_bytes)
    extra_bytes = b"EXTRA-OBJ"
    extra_sha = _sha(extra_bytes)
    extra_pool = mirror_root / "objects" / extra_sha[:2] / extra_sha
    extra_pool.parent.mkdir(parents=True, exist_ok=True)
    extra_pool.write_bytes(extra_bytes)
    extra_rec = consistent_backup.ArchivedObjectRecord(
        bucket="dalton-main", key="blobs/u1/extra", version_id="v1", size=len(extra_bytes), archived_sha256=extra_sha
    )
    for sid, recs in (("20261126-030000", [rec]), ("20261127-030000", [extra_rec])):
        sd = tmp_path / f"{sid}.dump"
        sd.write_bytes(b"PGDMP")
        local_backup_set.publish_local_backup_set(
            backups_root=backups_root,
            backup_set_id=sid,
            tag="daily",
            snapshot_id="snap",
            staged_dump_path=sd,
            manifest_objects=recs,
            asr_summary=None,
            reconcile_ok=True,
        )

    # Corrupt surviving manifest JSON
    surviving_manifest = backups_root / "sets" / "minio-manifest-20261127-030000.json"
    surviving_manifest.write_text("{broken-json", encoding="utf-8")
    with pytest.raises(local_backup_set.PoolPruneAbortedError):
        local_backup_set.prune_local_backup_sets_and_pool(backups_root, retain_daily=1)
    # ZERO files or pool objects deleted!
    assert (backups_root / "sets" / "backup-set-20261126-030000.json").is_file()
    assert pool_file.is_file()
    assert extra_pool.is_file()


def test_weekly_baseline_immutable_pointer_after_verify_abort_prune_and_default_versioned(
    tmp_path: Path,
) -> None:
    """Codex #6 + #12 / Lead #3 & #8: weekly baseline empty-bucket exit, pointer only after remote verify, prune fail-closed."""
    from tests.unit.dalton.test_offsite_backup import (
        _make_mounted_python_wrapper,
        _write_fake_rclone,
        _write_test_env,
    )

    # 1. Empty bucket env -> weekly-minio-baseline exits non-zero (never uploads 0-object baseline)
    res_empty = subprocess.run(
        [
            sys.executable,
            str(PROD_DIR / "offsite_sync.py"),
            "weekly-minio-baseline",
            "--baseline-id",
            "2026-11-27-run1",
            "--out-tar",
            str(tmp_path / "empty.tar.gz"),
        ],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_empty.returncode != 0
    assert "No active MinIO/S3 buckets" in res_empty.stderr

    # 2. Verify data_backup.sh defaults to versioned mode and loads PROD_ENV_FILE
    data_sh_text = (PROD_DIR / "data_backup.sh").read_text(encoding="utf-8")
    assert 'DALTON_WEEKLY_MINIO_MODE:-versioned' in data_sh_text
    assert 'source "${PROD_ENV_FILE}"' in data_sh_text

    # 3. Subprocess test: when daily LATEST.json cat fails during weekly prune, zero weekly dirs are purged!
    home_dir = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    drive_store = tmp_path / "drive_store"
    ext_mount = tmp_path / "ext_ssd"
    prod_dir = tmp_path / "prod"
    prod_var = prod_dir / "var"
    for d in (home_dir, fake_bin, drive_store, ext_mount, prod_var):
        d.mkdir(parents=True)
    (prod_var / "keep.txt").write_text("ok\n", encoding="utf-8")
    prod_env = prod_dir / ".env"
    prod_env.write_text("REDIS_PASSWORD=pw\n", encoding="utf-8")

    # Seed old weekly dir that WOULD be pruned + a daily dir with LATEST.json
    old_week = drive_store / "DaltonBackup" / "weekly" / "2026-09-01"
    old_week.mkdir(parents=True)
    (old_week / "sentinel.txt").write_text("keep-me\n", encoding="utf-8")
    for w in ("2026-10-01", "2026-10-08", "2026-10-15", "2026-10-22", "2026-10-29"):
        (drive_store / "DaltonBackup" / "weekly" / w).mkdir(parents=True)
    daily_dir = drive_store / "DaltonBackup" / "db" / "2026-11-26"
    daily_dir.mkdir(parents=True)
    (daily_dir / "LATEST.json").write_text(json.dumps({"baseline_id": "2026-09-01-run1"}), encoding="utf-8")

    minio_tar = tmp_path / "m.tar.gz"
    with tarfile.open(minio_tar, "w:gz") as tf:
        info = tarfile.TarInfo("a.txt")
        info.size = 2
        tf.addfile(info, io.BytesIO(b"ok"))
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"""#!/usr/bin/env python3
import pathlib, sys
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd in ("info", "volume", "inspect"): sys.exit(0)
if cmd == "ps": sys.exit(0)
if cmd == "run":
    sys.stdout.buffer.write(pathlib.Path({str(minio_tar)!r}).read_bytes())
    sys.exit(0)
if cmd == "exec":
    sys.stdin.read()
    sys.stdout.buffer.write(b"REDIS0011ok")
    sys.exit(0)
sys.exit(1)
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    about_file = tmp_path / "about.json"
    about_file.write_text('{"free": 100000000}\n', encoding="utf-8")
    _write_fake_rclone(
        fake_bin,
        drive_store=drive_store,
        about_file=about_file,
        proxy_check_file=tmp_path / "proxy.txt",
        rcat_sizes_log=tmp_path / "sizes.jsonl",
    )
    key_file = home_dir / ".config" / "dalton" / "backup.key"
    alerts_file = home_dir / "Library" / "Logs" / "alerts.log"
    log_file = home_dir / "Library" / "Logs" / "backup.log"
    env_file = tmp_path / "offsite.env"
    _write_test_env(
        env_file,
        home_dir=home_dir,
        repo_dir=tmp_path / "repo",
        key_file=key_file,
        alerts_file=alerts_file,
        log_file=log_file,
        prod_checkout=prod_dir,
        prod_var=prod_var,
        user_config=home_dir / ".config" / "dalton",
        launchagents=home_dir / "Library" / "LaunchAgents",
        backup_scripts=tmp_path / "bs",
        colima=home_dir / ".colima",
        ext_mount=ext_mount,
    )
    with env_file.open("a", encoding="utf-8") as fh:
        fh.write(f'DALTON_PROD_ENV_FILE="{prod_env}"\n')
        fh.write('DALTON_WEEKLY_MINIO_MODE="volume"\n')
        fh.write('DALTON_DRIVE_RETAIN_WEEKS="2"\n')

    mounted_py = _make_mounted_python_wrapper(tmp_path, ext_mount)
    res = subprocess.run(
        ["bash", str(PROD_DIR / "data_backup.sh")],
        env={
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(home_dir),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_file),
            "DALTON_REPO_PYTHON": str(mounted_py),
            "FAKE_RCLONE_FAIL_CAT": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert old_week.is_dir(), "Incomplete dependency scan must abort weekly prune without deleting anything"

    # 4. publish_weekly_baseline_pointer & find_latest_weekly_baseline_manifest
    sets_dir = tmp_path / "sets"
    staged_bl = tmp_path / "staged-bl.json"
    staged_bl.write_text(
        json.dumps({"version": 2, "baseline_id": "2026-11-27-run42", "objects": [{"archived_sha256": "a" * 64}]}),
        encoding="utf-8",
    )
    # Before pointer publish, sets_dir has no visible baseline
    assert offsite_sync.find_latest_weekly_baseline_manifest(sets_dir) == ("", [])
    offsite_sync.publish_weekly_baseline_pointer(
        staged_manifest_path=staged_bl, sets_dir=sets_dir, baseline_id="2026-11-27-run42"
    )
    bid, objs = offsite_sync.find_latest_weekly_baseline_manifest(sets_dir)
    assert bid == "2026-11-27-run42"
    assert len(objs) == 1
    # Immutable baseline_id protects its YYYY-MM-DD weekly directory from pruning!
    pruned = offsite_sync.select_weekly_baselines_to_prune(
        ["2026-11-01/", "2026-11-08/", "2026-11-15/", "2026-11-22/", "2026-11-27/"],
        retain_weeks=2,
        referenced_baseline_ids=["2026-11-01-20261101-050000-abc123"],
    )
    assert "2026-11-01" not in pruned


def test_daily_offsite_immutable_reruns_preserve_old_latest_and_reject_stale_set(
    tmp_path: Path,
) -> None:
    """Codex #7 + Grok #5 / Lead #3: stale set rejected; same-day rerun never overwrites old artifacts."""
    from tests.unit.dalton.test_offsite_backup import (
        _make_mounted_python_wrapper,
        _write_fake_rclone,
        _write_test_env,
    )

    home_dir = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    drive_store = tmp_path / "drive_store"
    ext_mount = tmp_path / "ext_ssd"
    for d in (home_dir, fake_bin, drive_store, ext_mount):
        d.mkdir(parents=True)

    backups_root = ext_mount / "Dalton" / "backups"
    sets_dir = backups_root / "sets"
    pg_dir = backups_root / "pg"
    mirror_objs = backups_root / "minio-mirror" / "objects"
    for d in (sets_dir, pg_dir, mirror_objs):
        d.mkdir(parents=True)

    dump_path = pg_dir / "dalton-pg-20261127-030000.dump"
    dump_path.write_bytes(b"PGDMP-immutable-rerun-test")
    manifest_path = sets_dir / "minio-manifest-20261127-030000.json"
    manifest_path.write_text(json.dumps({"version": 2, "objects": []}), encoding="utf-8")

    # 1. Stale set (yesterday's created_at) MUST fail with BACKUP_SET_INVALID
    yesterday = (dt.datetime.now().astimezone() - dt.timedelta(days=1)).isoformat(timespec="seconds")
    latest_set = sets_dir / "latest-verified-set.json"
    latest_set.write_text(
        json.dumps(
            {
                "version": 1,
                "backup_set_id": "20261126-030000",
                "tag": "daily",
                "created_at": yesterday,
                "dump_path": str(dump_path),
                "manifest_path": str(manifest_path),
                "reconcile_ok": True,
            }
        ),
        encoding="utf-8",
    )

    key_file = home_dir / ".config" / "dalton" / "backup.key"
    alerts_file = home_dir / "Library" / "Logs" / "alerts.log"
    log_file = home_dir / "Library" / "Logs" / "backup.log"
    env_file = tmp_path / "offsite.env"
    about_file = tmp_path / "about.json"
    about_file.write_text('{"free": 1000000000}\n', encoding="utf-8")
    _write_test_env(
        env_file,
        home_dir=home_dir,
        repo_dir=tmp_path / "repo",
        key_file=key_file,
        alerts_file=alerts_file,
        log_file=log_file,
        prod_checkout=tmp_path / "prod",
        prod_var=tmp_path / "prod" / "var",
        user_config=home_dir / ".config" / "dalton",
        launchagents=home_dir / "Library" / "LaunchAgents",
        backup_scripts=pg_dir,
        colima=home_dir / ".colima",
        ext_mount=ext_mount,
    )
    with env_file.open("a", encoding="utf-8") as fh:
        fh.write(f'DALTON_BACKUP_ROOT="{backups_root}"\n')
        fh.write('DALTON_RCLONE_REMOTE="gdrive"\n')
        fh.write('DALTON_RCLONE_DRIVE_DIR="DaltonBackup"\n')

    _write_fake_rclone(
        fake_bin,
        drive_store=drive_store,
        about_file=about_file,
        proxy_check_file=tmp_path / "proxy.txt",
        rcat_sizes_log=tmp_path / "sizes.jsonl",
    )
    mounted_py = _make_mounted_python_wrapper(tmp_path, ext_mount)
    base_env = {
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(home_dir),
        "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_file),
        "DALTON_REPO_PYTHON": str(mounted_py),
    }

    res_stale = subprocess.run(
        ["bash", str(PROD_DIR / "db_offsite.sh")],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_stale.returncode != 0
    assert "BACKUP_SET_INVALID" in alerts_file.read_text(encoding="utf-8")

    # 2. Fresh today set -> Run 1 succeeds and publishes LATEST.json with run1 artifacts
    today_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    latest_set.write_text(
        json.dumps(
            {
                "version": 1,
                "backup_set_id": "20261127-030000",
                "tag": "daily",
                "created_at": today_iso,
                "dump_path": str(dump_path),
                "manifest_path": str(manifest_path),
                "reconcile_ok": True,
            }
        ),
        encoding="utf-8",
    )
    res_run1 = subprocess.run(
        ["bash", str(PROD_DIR / "db_offsite.sh")],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_run1.returncode == 0, res_run1.stderr
    today_dir = drive_store / "DaltonBackup" / "db" / dt.datetime.now().strftime("%Y-%m-%d")
    latest_run1 = json.loads((today_dir / "LATEST.json").read_text(encoding="utf-8"))
    run1_dump_name = latest_run1["artifacts"]["dump"]["filename"]
    run1_dump_md5 = latest_run1["artifacts"]["dump"]["md5"]
    assert latest_run1["run_id"] in run1_dump_name

    # 3. Same-day rerun fails when uploading LATEST.json -> Run 1's LATEST.json AND dump MD5 stay 100% intact!
    res_run2_fail = subprocess.run(
        ["bash", str(PROD_DIR / "db_offsite.sh")],
        env={**base_env, "FAKE_RCLONE_FAIL_MATCH": "LATEST.json"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_run2_fail.returncode != 0
    latest_after_fail = json.loads((today_dir / "LATEST.json").read_text(encoding="utf-8"))
    assert latest_after_fail["run_id"] == latest_run1["run_id"]
    actual_md5 = hashlib.md5((today_dir / run1_dump_name).read_bytes()).hexdigest()
    assert actual_md5 == run1_dump_md5


def test_asr_excludes_indeterminate_and_retried_jobs_and_converges_restored_dirs(
    tmp_path: Path,
) -> None:
    """Codex #8 + Grok #7 / Lead #4: indeterminate excluded, live retried dir skipped, restored dirs converged before startup."""
    from src.dalton.transcription_service.jobs import TERMINAL_JOB_STATUSES

    assert storage_refs.TERMINAL_ASR_ARCHIVE_STATUSES == TERMINAL_JOB_STATUSES
    assert "indeterminate" not in storage_refs.TERMINAL_ASR_ARCHIVE_STATUSES

    asr_dir = tmp_path / "asr_jobs"
    asr_dir.mkdir()
    (asr_dir / "asr_usage.jsonl").write_text('{"job_id":"j-ok"}\n', encoding="utf-8")

    # 1. Terminal succeeded job (included)
    (asr_dir / "j-ok").mkdir()
    (asr_dir / "j-ok" / "state.json").write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")
    (asr_dir / "j-ok" / "result.json").write_text(json.dumps({"text": "hi"}), encoding="utf-8")
    # 2. Failed in snapshot, but retried in-place before packing (live status = transcribing -> MUST skip!)
    (asr_dir / "j-retried").mkdir()
    (asr_dir / "j-retried" / "state.json").write_text(json.dumps({"status": "transcribing"}), encoding="utf-8")
    # 3. Indeterminate job (MUST skip!)
    (asr_dir / "j-indet").mkdir()
    (asr_dir / "j-indet" / "state.json").write_text(json.dumps({"status": "indeterminate"}), encoding="utf-8")

    arch_path = tmp_path / "asr.tar.gz"
    summary = consistent_backup.archive_terminal_asr_jobs(
        asr_dir, ["j-ok", "j-retried", "j-indet"], arch_path
    )
    assert summary.included_job_ids == ("j-ok",)

    # Reconcile rejects any tar containing a non-terminal/indeterminate state.json
    bad_tar = tmp_path / "bad-asr.tar.gz"
    with tarfile.open(bad_tar, "w:gz") as tf:
        raw = json.dumps({"status": "indeterminate"}).encode("utf-8")
        info = tarfile.TarInfo("j-indet/state.json")
        info.size = len(raw)
        tf.addfile(info, io.BytesIO(raw))
    bad_report = storage_refs.reconcile_inventory_against_manifest(
        storage_refs.SnapshotRefInventory(),
        [],
        bucket_targets={},
        asr_jobs_archive=bad_tar,
    )
    assert bad_report.ok is False
    assert any(e.code == "non_terminal_archived_asr_job" for e in bad_report.errors)

    # Pre-service-start convergence marks restored dirs failed (including indeterminate) & blocks auto-resume!
    seen_statuses_arg: list[Any] = []

    def _rec_handler(sql: str, params: Any) -> Any:
        s = " ".join(sql.split())
        if s.startswith("SELECT to_regclass"):
            return (params[0],)
        if "SELECT upload_id, object_key" in s:
            return []
        if "UPDATE dalton_transcription_jobs SET status = 'failed'" in s:
            seen_statuses_arg.append(params)
            return 2
        return 0

    conn = _FakeAsyncConn(_rec_handler)
    rec_summary = asyncio.run(
        consistent_backup.reconcile_restored_inflight_states(
            conn,
            asr_jobs_dir=asr_dir,
            restored_before=dt.datetime(2026, 11, 27, 3, 0, tzinfo=dt.timezone.utc),
        )
    )
    assert rec_summary.asr_dirs_failed == 2
    assert "indeterminate" in seen_statuses_arg[0][1]
    assert json.loads((asr_dir / "j-indet" / "state.json").read_text(encoding="utf-8"))["status"] == "failed"
    assert json.loads((asr_dir / "j-retried" / "state.json").read_text(encoding="utf-8"))["status"] == "failed"

    mgr = AsrJobManager(jobs_dir=asr_dir, worker=FakeAsrWorker(), auto_recover=False)
    assert mgr.recover_incomplete_jobs() == []


def test_transcription_resubmit_stable_attempt_id_concurrent_and_lost_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex #9 / Lead #4: same original job reuses one persisted attempt row on double-submit, concurrency & dropped response."""
    audio_bytes = b"RIFF-stable-attempt-audio"
    audio_sha = _sha(audio_bytes)
    old_job_id = "22222222-2222-2222-2222-222222222222"
    source_key = f"transcriptions/ws-1/{old_job_id}/source/call.mp3"

    jobs_store: dict[str, dict[str, Any]] = {
        old_job_id: {
            "job_id": old_job_id,
            "created_by_user_id": "user-internal-1",
            "workspace_id": "ws-1",
            "thread_id": "th-1",
            "source_object_key": source_key,
            "source_size_bytes": len(audio_bytes),
            "source_sha256": audio_sha,
            "language": "zh",
            "hotwords": [],
            "status": "failed",
            "stage": "failed",
            "segment_count": 0,
            "completed_segments": 0,
            "provider_request_ids": [],
            "usage": {"upload": {"completed": True}},
            "result_object_key": None,
            "error_category": "restored_inflight",
            "error_message": consistent_backup.RESTORED_INFLIGHT_ERROR_MESSAGE,
            "idempotency_key": "orig-idemp-2",
            "version": 1,
        }
    }

    async def _fake_get_job(jid: str, *, conn: Any = None) -> dict[str, Any] | None:
        row = jobs_store.get(jid)
        return dict(row) if row else None

    async def _fake_create_job(**kwargs: Any) -> dict[str, Any]:
        for existing in jobs_store.values():
            if existing["idempotency_key"] == kwargs["idempotency_key"]:
                return dict(existing)
        row = {
            "job_id": str(kwargs["job_id"]),
            "created_by_user_id": kwargs["created_by_user_id"],
            "workspace_id": kwargs["workspace_id"],
            "thread_id": kwargs.get("thread_id") or "",
            "source_object_key": kwargs["source_object_key"],
            "source_size_bytes": int(kwargs["source_size_bytes"]),
            "source_sha256": kwargs["source_sha256"],
            "language": kwargs.get("language"),
            "hotwords": list(kwargs.get("hotwords") or []),
            "status": str(kwargs.get("status", "awaiting_upload")),
            "stage": str(kwargs.get("stage", "awaiting_upload")),
            "segment_count": 0,
            "completed_segments": 0,
            "provider_request_ids": [],
            "usage": {},
            "result_object_key": None,
            "error_category": None,
            "error_message": None,
            "idempotency_key": kwargs["idempotency_key"],
            "version": 1,
        }
        jobs_store[row["job_id"]] = row
        return dict(row)

    async def _fake_update_versioned(job_id: str, *, expected_version: int, **kwargs: Any):
        row = jobs_store.get(job_id)
        if row is None or int(row["version"]) != int(expected_version):
            return False, dict(row) if row else None
        for k in ("status", "stage", "usage", "segment_count", "completed_segments"):
            if kwargs.get(k) is not None:
                row[k] = kwargs[k].value if hasattr(kwargs[k], "value") else kwargs[k]
        row["version"] = int(row["version"]) + 1
        return True, dict(row)

    mgr = AsrJobManager(jobs_dir=tmp_path / "asr_jobs", worker=FakeAsrWorker())
    bridge_calls: list[dict[str, Any]] = []
    drop_first = {"drop": True}

    async def _flaky_bridge_submit(payload: dict[str, Any]):
        bridge_calls.append(dict(payload))
        code, resp = await mgr.submit_job(payload)
        if drop_first["drop"]:
            drop_first["drop"] = False
            raise TimeoutError("simulated lost bridge response after accept")
        return code, resp

    monkeypatch.setattr(tr_db, "get_transcription_job", create_autospec(tr_db.get_transcription_job, side_effect=_fake_get_job))
    monkeypatch.setattr(tr_db, "create_transcription_job", create_autospec(tr_db.create_transcription_job, side_effect=_fake_create_job))
    monkeypatch.setattr(tr_db, "update_transcription_job_versioned", create_autospec(tr_db.update_transcription_job_versioned, side_effect=_fake_update_versioned))
    monkeypatch.setattr(tr_svc, "require_internal_transcription_caller", create_autospec(tr_svc.require_internal_transcription_caller, side_effect=lambda u: u))
    monkeypatch.setattr(tr_svc, "authorize_job_access", create_autospec(tr_svc.authorize_job_access, return_value=None))
    monkeypatch.setattr(tr_svc, "is_transcription_storage_configured", create_autospec(tr_svc.is_transcription_storage_configured, return_value=True))
    monkeypatch.setattr(tr_svc, "object_size", create_autospec(tr_svc.object_size, return_value=len(audio_bytes)))
    monkeypatch.setattr(tr_svc, "call_bridge_submit_transcription", create_autospec(tr_svc.call_bridge_submit_transcription, side_effect=_flaky_bridge_submit))

    async def _scenario() -> None:
        # 1. First submit: bridge accepts job, then response is lost -> caller receives 502
        with pytest.raises(Exception) as exc_info:
            await tr_svc.resubmit_transcription_job(job_id=old_job_id, caller_user_id="user-internal-1")
        assert getattr(exc_info.value, "status_code", None) == 502

        # 2. Retry + concurrent duplicate calls on the SAME old_job_id -> ALL converge on ONE job_id!
        r1, r2 = await asyncio.gather(
            tr_svc.resubmit_transcription_job(job_id=old_job_id, caller_user_id="user-internal-1"),
            tr_svc.resubmit_transcription_job(job_id=old_job_id, caller_user_id="user-internal-1"),
        )
        assert r1["job"]["job_id"] == r2["job"]["job_id"]
        assert r1["job"]["idempotency_key"] == r2["job"]["idempotency_key"]

        # 3. Calling /resubmit again on old_job_id returns the exact same attempt row without creating a 2nd row
        r3 = await tr_svc.resubmit_transcription_job(job_id=old_job_id, caller_user_id="user-internal-1")
        assert r3["job"]["job_id"] == r1["job"]["job_id"]
        assert len(jobs_store) == 2  # only original + 1 attempt row
        assert len({c["job_id"] for c in bridge_calls}) == 1
        assert len({c["idempotency_key"] for c in bridge_calls}) == 1

    asyncio.run(_scenario())


def test_standalone_unified_dsn_and_backup_sh_consistent_protocol(tmp_path: Path) -> None:
    """Codex #10 + Grok #2 / Lead #6: unified DSN for control conn + pg_dump; backup.sh calls consistent_backup."""
    # 1. Verify scripts/standalone/backup.sh invokes consistent_backup.py compose-backup with --snapshot
    backup_sh = (REPO_ROOT / "scripts" / "standalone" / "backup.sh").read_text(encoding="utf-8")
    assert "scripts/standalone/consistent_backup.py compose-backup" in backup_sh
    assert '--snapshot=${SNAPSHOT_ID}' in backup_sh

    # 2. Verify standalone consistent_backup uses the EXACT SAME --pg-dsn for control conn and pg_dump
    target_dsn = "postgresql://custom_u:secret@pg-custom.internal:5433/custom_db?sslmode=require"
    seen_conn_dsns: list[str] = []
    seen_dump_dsns: list[str] = []
    seen_snap_ids: list[str] = []

    def _db_handler(sql: str, params: Any) -> Any:
        s = " ".join(sql.split())
        if s.startswith("SELECT pg_export_snapshot"):
            return ("00000012-00000099-1",)
        if s.startswith("SELECT pg_advisory_lock") or s.startswith("SELECT pg_advisory_unlock"):
            return (True, True)
        if s.startswith("SELECT to_regclass"):
            return (None,)
        return []

    async def _fake_open(dsn: str) -> Any:
        seen_conn_dsns.append(dsn)
        return _FakeAsyncConn(_db_handler)

    def _fake_dump_starter(snap_id: str, out_path: Path, *, dsn: str) -> consistent_backup.PgDumpProcess:
        seen_dump_dsns.append(dsn)
        seen_snap_ids.append(snap_id)
        return consistent_backup.start_pg_dump_process(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'PGDMP-custom-dsn')"],
            out_path,
            cmd_label="fake_standalone_pg_dump",
        )

    s3 = _FakeS3Client(versioning_by_bucket={"dalton-storage": True})
    s3.seed_object("dalton-storage", "blobs/u1/abc", b"core-blob")
    args = standalone_cb.build_arg_parser().parse_args(
        ["backup", "--output-dir", str(tmp_path / "out"), "--pg-dsn", target_dsn]
    )
    rc = standalone_cb._cmd_backup(
        args,
        conn_opener=_fake_open,
        pg_dump_starter=_fake_dump_starter,
        bucket_resolver=lambda _env: [
            consistent_backup.BucketArchiveSpec(bucket="dalton-storage", roles=("main",), s3_client=s3)
        ],
    )
    assert rc == 0
    assert seen_conn_dsns == [target_dsn]
    assert seen_dump_dsns == [target_dsn]
    assert seen_snap_ids == ["00000012-00000099-1"]
    assert (tmp_path / "out" / "postgres.dump").read_bytes() == b"PGDMP-custom-dsn"
    with tarfile.open(tmp_path / "out" / "minio-objects.tar.gz", "r:gz") as tf:
        m = json.loads(tf.extractfile("_minio_manifest.json").read().decode("utf-8"))
        assert m["object_count"] == 1
        assert m["objects"][0]["sha256"] == _sha(b"core-blob")
        assert m["objects"][0]["archived_sha256"] == _sha(b"core-blob")


def test_preflight_and_verify_mount_check_device_ids_and_colima_disk_files(tmp_path: Path) -> None:
    """Codex #11 / Lead #7: st_dev verification on backup dir/parent and active Colima virtual disk file."""
    ext_mount = tmp_path / "EveSSD"
    internal_disk = tmp_path / "internal_root"
    colima_ext = ext_mount / "colima"
    lima_dir = colima_ext / "_lima" / "colima"
    lima_dir.mkdir(parents=True)
    internal_disk.mkdir(parents=True)

    # Case 1: ~/.colima is on external SSD, but diffdisk is symlinked back to internal disk -> FAIL!
    internal_diffdisk = internal_disk / "diffdisk"
    internal_diffdisk.write_bytes(b"qcow")
    (lima_dir / "diffdisk").symlink_to(internal_diffdisk)

    report_bad_link = backup_lib.preflight_disk_space(
        external_mount_root=ext_mount,
        internal_root=internal_disk,
        colima_dir=colima_ext,
        is_mount_fn=lambda p: p == ext_mount,
        disk_usage_fn=lambda _p: type("U", (), {"free": 300 * 1024**3})(),
        colima_docker_free_bytes_fn=lambda: 80 * 1024**3,
    )
    assert report_bad_link.ok is False
    colima_chk = next(c for c in report_bad_link.checks if c.name == "colima_location")
    assert colima_chk.ok is False

    # Case 2: Nested backup subdir on a different st_dev -> verify_external_mount raises MountCheckError!
    backups_root = ext_mount / "Dalton" / "backups"
    nested_pg = backups_root / "pg"
    nested_pg.mkdir(parents=True)

    def _split_dev_stat(p: Path) -> Any:
        dev = 999 if str(p.resolve(strict=False)).startswith(str(nested_pg.resolve())) else 100
        return type("Stat", (), {"st_dev": dev})()

    with pytest.raises(backup_lib.MountCheckError, match="different device"):
        backup_lib.verify_backup_mount_layout(
            ext_mount,
            backups_root,
            is_mount_fn=lambda p: p == ext_mount,
            stat_fn=_split_dev_stat,
        )


def test_sunday_weekly_tag_and_deploy_tag_and_deploy_main_running_backup_defer(
    tmp_path: Path,
) -> None:
    """Grok #3 + #4 / Lead #8: Sunday -> --tag weekly, deploy -> --tag deploy, running backup defers cleanly."""
    # 1. plist passes --tag auto
    pg_plist = plistlib.loads((PROD_DIR / "launchd" / "com.dalton.pg-backup.plist.template").read_bytes())
    assert pg_plist["ProgramArguments"][-2:] == ["--tag", "auto"]

    # 2. prod/backup.sh --tag auto resolves Sunday->weekly, Monday->daily, hint->deploy
    fake_py = tmp_path / "fake_py.sh"
    recorded_tags = tmp_path / "tags.txt"
    fake_py.write_text(
        f"""#!/usr/bin/env bash
if [[ "$2" == "verify-mount" ]]; then exit 0; fi
if [[ "$2" == "read-json-field" ]]; then
  printf '%s\\n%s\\n' "{tmp_path}/d.dump" "set-1"
  exit 0
fi
for ((i=1; i<=$#; i++)); do
  if [[ "${{!i}}" == "--tag" ]]; then
    j=$((i+1))
    echo "${{!j}}" >> "{recorded_tags}"
  fi
done
echo '{{"dump_path":"{tmp_path}/d.dump","backup_set_id":"set-1"}}'
""",
        encoding="utf-8",
    )
    fake_py.chmod(0o755)
    (tmp_path / "d.dump").write_bytes(b"PGDMP")
    tag_hint = tmp_path / ".next-tag"

    base_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "DALTON_REPO_PYTHON": str(fake_py),
        "DALTON_BACKUP_LOAD_ENV": "0",
        "DALTON_BACKUP_ROOT": str(tmp_path / "backups"),
        "DALTON_PG_BACKUP_TAG_FILE": str(tag_hint),
    }
    (tmp_path / "backups" / ".tmp").mkdir(parents=True)

    # Sunday -> weekly
    r_sun = subprocess.run(
        ["bash", str(PROD_DIR / "backup.sh"), "--tag", "auto", "--skip-mount-check"],
        env={**base_env, "DALTON_BACKUP_WEEKDAY": "0"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r_sun.returncode == 0, r_sun.stderr
    # Monday -> daily
    r_mon = subprocess.run(
        ["bash", str(PROD_DIR / "backup.sh"), "--tag", "auto", "--skip-mount-check"],
        env={**base_env, "DALTON_BACKUP_WEEKDAY": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r_mon.returncode == 0, r_mon.stderr
    # Deploy one-shot hint -> deploy (and hint file consumed!)
    tag_hint.write_text("deploy\n", encoding="utf-8")
    r_dep = subprocess.run(
        ["bash", str(PROD_DIR / "backup.sh"), "--tag", "auto", "--skip-mount-check"],
        env={**base_env, "DALTON_BACKUP_WEEKDAY": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r_dep.returncode == 0, r_dep.stderr
    assert not tag_hint.exists()
    assert recorded_tags.read_text(encoding="utf-8").splitlines() == ["weekly", "daily", "deploy"]

    # 3. deploy_main.sh: timeout while backup job is STILL running defers (exit 75, NOT "backup failed")
    # and does NOT duplicate-kickstart an already running backup!
    deploy_sh_text = (PROD_DIR / "deploy_main.sh").read_text(encoding="utf-8")
    assert "_default_backup_wait_s=25200" in deploy_sh_text
    assert "_pg_backup_job_running" in deploy_sh_text

    from tests.unit.dalton.test_deploy_main_reuse_backup import _run_deploy, _setup_deploy_env

    ctx = _setup_deploy_env(tmp_path / "dep")
    # Fake launchctl reports com.dalton.pg-backup is already running and never finishes within 1s
    (ctx["fake_bin"] / "launchctl").write_text(
        f"""#!/bin/bash
echo "$*" >> "{ctx['launchctl_trace']}"
if [ "$1" = "print" ]; then
  echo "    state = running"
  echo "    pid = 42424"
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    (ctx["fake_bin"] / "launchctl").chmod(0o755)
    res_running = _run_deploy(
        ctx,
        tmp_path / "dep",
        extra_setup='DALTON_DEPLOY_BACKUP_WAIT_S=1; DALTON_DEPLOY_BACKUP_POLL_S=1',
    )
    assert res_running.returncode == 75, res_running.stdout + res_running.stderr
    assert "still running" in res_running.stdout
    assert "backup failed" not in res_running.stdout
    assert "kickstart" not in Path(ctx["launchctl_trace"]).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Round 3 fixes: watchdog blocked-I/O kill, versioning fail-closed, standalone
# compose-backup unified DSN/ASR/restore order, restored transcription guards,
# immutable per-baseline weekly manifests, workspace_file_uploads, Colima
# active resolution, deploy tracked backup resume
# ---------------------------------------------------------------------------


def test_lock_phase_watchdog_kills_blocked_io_and_versioning_probe_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lead #1 (Codex #1 + Grok #4): fail-closed versioning probe, read-loop deadline, and
    independent watchdog killing blocked lock-holding S3/ASR/unversioned I/O + DB session."""
    import threading
    import time

    def _empty_db(sql: str, params: Any) -> Any:
        s = " ".join(sql.split())
        if s.startswith("SELECT pg_export_snapshot"):
            return ("00000019-00000001-1",)
        if s.startswith("SELECT pg_advisory_lock") or s.startswith("SELECT pg_advisory_unlock"):
            return (True, True)
        if s.startswith("SELECT to_regclass"):
            return (None,)
        return []

    # 1. Versioning probe error fails closed (NEVER misclassified as unversioned)
    s3_err = create_autospec(_FakeS3Client, instance=True)
    s3_err.get_bucket_versioning.side_effect = RuntimeError("MinIO transient 503")
    with pytest.raises(consistent_backup.ConsistentBackupError, match="Failed probing bucket versioning"):
        consistent_backup.detect_bucket_versioning(s3_err, "dalton-main")

    # 2. Blocked synchronous S3 list_object_versions while holding locks + running pg_dump ->
    #    independent watchdog closes DB control session, kills pg_dump process group, and
    #    raises BackupTimeoutError promptly (does NOT wait for the 30s blocked S3 call).
    unblock_list = threading.Event()
    s3_block_list = _FakeS3Client(versioning_by_bucket={"dalton-main": True})

    def _blocking_list(**_kw: Any) -> dict[str, Any]:
        unblock_list.wait(timeout=30.0)
        return {"Versions": [], "DeleteMarkers": [], "IsTruncated": False}

    s3_block_list.list_object_versions = _blocking_list  # type: ignore[method-assign]
    spawned_dumps: list[consistent_backup.PgDumpProcess] = []

    def _slow_dump(_snap: str, out_path: Path) -> consistent_backup.PgDumpProcess:
        proc = consistent_backup.start_pg_dump_process(
            [
                sys.executable,
                "-c",
                "import sys, time; sys.stdout.buffer.write(b'PGDMP'); sys.stdout.buffer.flush(); time.sleep(60)",
            ],
            out_path,
            cmd_label="watchdog_slow_pg_dump",
        )
        spawned_dumps.append(proc)
        return proc

    conn_list = _FakeAsyncConn(_empty_db)
    t0 = time.monotonic()
    try:
        with pytest.raises(consistent_backup.BackupTimeoutError):
            asyncio.run(
                consistent_backup.run_consistent_backup(
                    conn=conn_list,
                    pg_dump_path=tmp_path / "list-block.dump",
                    pg_dump_fn=_slow_dump,
                    bucket_specs=[
                        consistent_backup.BucketArchiveSpec(
                            bucket="dalton-main", roles=("main",), s3_client=s3_block_list
                        )
                    ],
                    object_sink=consistent_backup.MirrorPoolSink(tmp_path / "m-list"),
                    lock_phase_timeout_s=0.35,
                    pg_dump_timeout_s=10.0,
                    snapshot_import_timeout_s=5.0,
                )
            )
        elapsed_list = time.monotonic() - t0
    finally:
        unblock_list.set()
    assert elapsed_list < 4.0, f"Watchdog failed to abort blocked list_object_versions promptly ({elapsed_list:.2f}s)"
    assert conn_list.closed is True, "Watchdog must close DB control session on timeout"
    assert spawned_dumps and spawned_dumps[0].popen.poll() is not None, "Watchdog must kill pg_dump process group"

    # 3. Blocked ASR archive subprocess in its own process group -> watchdog kills ASR process group & DB session
    asr_dir = tmp_path / "asr_jobs"
    asr_dir.mkdir()
    spawned_asr_pids: list[int] = []
    orig_start_asr = consistent_backup.start_asr_archive_process

    def _stalled_asr_cmd(_d: Path, _ids: Path, _out: Path) -> list[str]:
        return [sys.executable, "-c", "import time; time.sleep(60)"]

    def _track_start_asr(d: Path, ids: Any, out: Path) -> consistent_backup.AsrArchiveProcess:
        proc = orig_start_asr(d, ids, out)
        spawned_asr_pids.append(proc.popen.pid)
        return proc

    monkeypatch.setattr(consistent_backup, "_build_asr_archive_cmd", _stalled_asr_cmd)
    monkeypatch.setattr(consistent_backup, "start_asr_archive_process", _track_start_asr)

    s3_empty = _FakeS3Client(versioning_by_bucket={"dalton-main": True})
    conn_asr = _FakeAsyncConn(_empty_db)
    t1 = time.monotonic()
    with pytest.raises(consistent_backup.BackupTimeoutError):
        asyncio.run(
            consistent_backup.run_consistent_backup(
                conn=conn_asr,
                pg_dump_path=tmp_path / "asr-block.dump",
                pg_dump_fn=lambda _s, p: p.write_bytes(b"PGDMP"),
                bucket_specs=[
                    consistent_backup.BucketArchiveSpec(
                        bucket="dalton-main", roles=("main",), s3_client=s3_empty
                    )
                ],
                object_sink=consistent_backup.MirrorPoolSink(tmp_path / "m-asr"),
                asr_jobs_dir=asr_dir,
                asr_archive_path=tmp_path / "asr.tar.gz",
                lock_phase_timeout_s=0.35,
            )
        )
    elapsed_asr = time.monotonic() - t1
    assert elapsed_asr < 4.0
    assert conn_asr.closed is True
    assert spawned_asr_pids
    with pytest.raises(ProcessLookupError):
        os.kill(spawned_asr_pids[0], 0)

    # 4. Unversioned bucket slow-drip stream: read loop checks deadline AND watchdog closes stream + DB session
    class _SlowStream:
        def __init__(self) -> None:
            self.closed = False
            self.reads = 0

        def read(self, _n: int = -1) -> bytes:
            self.reads += 1
            time.sleep(0.12)
            if self.closed:
                return b""
            return b"x" * 16

        def close(self) -> None:
            self.closed = True

    slow_stream = _SlowStream()
    s3_unver = _FakeS3Client(versioning_by_bucket={"dalton-unver": False})
    s3_unver.seed_object("dalton-unver", "blobs/u1/slow", b"initial")
    s3_unver.get_object = lambda **_kw: {  # type: ignore[method-assign]
        "Body": slow_stream,
        "ContentType": "application/octet-stream",
    }
    conn_unver = _FakeAsyncConn(_empty_db)
    t2 = time.monotonic()
    with pytest.raises(consistent_backup.BackupTimeoutError):
        asyncio.run(
            consistent_backup.run_consistent_backup(
                conn=conn_unver,
                pg_dump_path=tmp_path / "unver-slow.dump",
                pg_dump_fn=lambda _s, p: p.write_bytes(b"PGDMP"),
                bucket_specs=[
                    consistent_backup.BucketArchiveSpec(
                        bucket="dalton-unver",
                        roles=("main",),
                        s3_client=s3_unver,
                        versioning_enabled=False,
                    )
                ],
                object_sink=consistent_backup.MirrorPoolSink(tmp_path / "m-unver"),
                lock_phase_timeout_s=0.30,
            )
        )
    assert time.monotonic() - t2 < 4.0
    assert slow_stream.reads >= 1
    assert slow_stream.closed is True
    assert conn_unver.closed is True


def test_standalone_compose_backup_unified_dsn_asr_bundle_timeouts_and_restore_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lead #2 (Codex #2,#3,#4 + Grok #1,#2,#3): unified DATABASE_URL config on compose-backup,
    LOCK_TIMEOUT_S for SNAPSHOT_READY vs 120s for import, ASR volume mount + bundle, completing
    uploads allowed in backup, restore clears/extracts asr-jobs + reconciles BEFORE compose up -d."""
    # 1. resolve-pg-config resolves DATABASE_URL over DB_USER/DB_NAME and rewrites host for postgres container
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DB_HOST=postgres\n"
        "DB_USER=postgres\n"
        "DB_PASSWORD=oldpass\n"
        "DB_NAME=postgres\n"
        "DATABASE_URL=postgresql://app_owner:s3cr3t@postgres:5432/custom_core_db?sslmode=disable\n",
        encoding="utf-8",
    )
    res_cfg = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/standalone/consistent_backup.py"),
            "resolve-pg-config",
            "--env-file",
            str(env_file),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pg_cfg = json.loads(res_cfg.stdout)
    assert pg_cfg["source"] == "database_url"
    assert pg_cfg["user"] == "app_owner"
    assert pg_cfg["dbname"] == "custom_core_db"
    assert pg_cfg["dsn"] == "postgresql://app_owner:s3cr3t@postgres:5432/custom_core_db?sslmode=disable"
    assert pg_cfg["container_dsn"] == "postgresql://app_owner:s3cr3t@127.0.0.1:5432/custom_core_db?sslmode=disable"

    # 2. Actual compose-backup path: uses DATABASE_URL (same DSN), streams tar bundle with both
    #    minio-objects.tar.gz & terminal-only asr-jobs.tar.gz, and accepts `completing` chat uploads
    asr_dir = tmp_path / "asr_jobs"
    ok_dir = asr_dir / "asr-done-1"
    ok_dir.mkdir(parents=True)
    (ok_dir / "state.json").write_text(json.dumps({"job_id": "asr-done-1", "status": "succeeded"}), encoding="utf-8")
    (ok_dir / "result.json").write_text(json.dumps({"text": "ok"}), encoding="utf-8")
    inflight_dir = asr_dir / "asr-run-1"
    inflight_dir.mkdir(parents=True)
    (inflight_dir / "state.json").write_text(json.dumps({"job_id": "asr-run-1", "status": "transcribing"}), encoding="utf-8")

    s3 = _FakeS3Client(versioning_by_bucket={"chat-uploads": True})
    audio_bytes = b"audio-content"
    s3.seed_object("chat-uploads", "transcriptions/ws1/asr-done-1/src.m4a", audio_bytes)

    def _db_handler(sql: str, params: Any) -> Any:
        s = " ".join(sql.split())
        if s.startswith("SELECT pg_export_snapshot"):
            return ("00000033-00000001-1",)
        if s.startswith("SELECT pg_advisory_lock") or s.startswith("SELECT pg_advisory_unlock"):
            return (True, True)
        if s.startswith("SELECT to_regclass"):
            tbl = params[0] if params else ""
            if tbl in ("public.dalton_transcription_jobs", "public.dalton_chat_file_uploads"):
                return (tbl,)
            return (None,)
        if "FROM dalton_transcription_jobs" in s:
            return [
                (
                    "asr-done-1",
                    "succeeded",
                    "transcriptions/ws1/asr-done-1/src.m4a",
                    len(audio_bytes),
                    _sha(audio_bytes),
                    None,
                    {"upload": {"completed": True}},
                ),
                (
                    "asr-run-1",
                    "transcribing",
                    "transcriptions/ws1/asr-done-1/src.m4a",
                    len(audio_bytes),
                    _sha(audio_bytes),
                    None,
                    {"upload": {"completed": True}},
                ),
            ]
        if "FROM dalton_chat_file_uploads" in s and "WHERE cleaned_at IS NULL AND state IN" in s:
            return [
                ("up-completing-1", "chat-uploads/ws1/up-completing-1/f.pdf", 999, None, None, "completing"),
            ]
        return []

    seen_dsns: list[str] = []

    async def _fake_open(dsn: str) -> Any:
        seen_dsns.append(dsn)
        return _FakeAsyncConn(_db_handler)

    monkeypatch.setenv("DALTON_BACKUP_PG_DSN", pg_cfg["dsn"])
    ctl_in = io.StringIO("SNAPSHOT_IMPORTED\nPG_DUMP_DONE\n")
    ctl_err = io.StringIO()
    bundle_buf = io.BytesIO()
    fake_stdout = type("FakeStdout", (), {"buffer": bundle_buf})()
    monkeypatch.setattr(sys, "stdin", ctl_in)
    monkeypatch.setattr(sys, "stderr", ctl_err)
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    args = standalone_cb.build_arg_parser().parse_args(
        ["compose-backup", "--stream-bundle", "--asr-jobs-dir", str(asr_dir)]
    )
    rc = standalone_cb._cmd_compose_backup(
        args,
        conn_opener=_fake_open,
        bucket_resolver=lambda _env: [
            consistent_backup.BucketArchiveSpec(
                bucket="chat-uploads", roles=("asr", "chat_upload"), s3_client=s3
            )
        ],
    )
    assert rc == 0
    assert seen_dsns == [pg_cfg["dsn"]]
    assert "@@DALTON_CTL@@ SNAPSHOT_READY 00000033-00000001-1" in ctl_err.getvalue()
    assert "@@DALTON_CTL@@ BACKUP_SUMMARY " in ctl_err.getvalue()

    bundle_buf.seek(0)
    with tarfile.open(fileobj=bundle_buf, mode="r:") as btf:
        names = set(btf.getnames())
        assert "minio-objects.tar.gz" in names
        assert "asr-jobs.tar.gz" in names
        asr_bytes = btf.extractfile("asr-jobs.tar.gz").read()
    with tarfile.open(fileobj=io.BytesIO(asr_bytes), mode="r:gz") as atf:
        asr_names = set(atf.getnames())
        assert "asr-done-1/state.json" in asr_names
        assert "asr-done-1/result.json" in asr_names
        assert not any(n.startswith("asr-run-1/") for n in asr_names)

    # 3. storage_config.verify_backup_chat_upload_refs skips `completing` uploads
    from scripts.standalone import storage_config as standalone_sc

    copy_sql = (
        "COPY public.dalton_chat_file_uploads (upload_id, object_key, state, cleaned_at) FROM stdin;\n"
        "u-comp\tchat-uploads/ws1/u-comp/part.pdf\tcompleting\t\\N\n"
        "\\.\n"
    )
    vres = standalone_sc.verify_backup_chat_upload_refs(
        copy_sql,
        {
            "primary_bucket": "chat-uploads",
            "targets": {"chat_upload": {"bucket": "chat-uploads", "key_prefix": ""}},
            "objects": [],
        },
    )
    assert vres["missing"] == []
    assert vres["skipped_uploading"] == 1

    # 4. backup.sh & restore.sh wiring checks
    backup_sh = (REPO_ROOT / "scripts/standalone/backup.sh").read_text(encoding="utf-8")
    assert 'LOCK_TIMEOUT_S="${DALTON_BACKUP_LOCK_TIMEOUT_S:-7200}"' in backup_sh
    assert 'SNAP_READY_DEADLINE=$(( $(date +%s) + LOCK_TIMEOUT_S ))' in backup_sh
    assert 'IMPORT_DEADLINE=$(( $(date +%s) + SNAP_IMPORT_TIMEOUT_S ))' in backup_sh
    assert "--asr-jobs-dir /var/lib/dalton/asr_jobs" in backup_sh
    assert "-v \"${ASR_JOBS_VOLUME}:/var/lib/dalton/asr_jobs:ro\"" in backup_sh
    assert "consistent_backup.py\" resolve-pg-config" in backup_sh
    assert "DALTON_PG_DUMP_DSN" in backup_sh
    assert "verify-backup-refs" not in backup_sh

    restore_sh = (REPO_ROOT / "scripts/standalone/restore.sh").read_text(encoding="utf-8")
    idx_asr = restore_sh.index("Restoring terminal ASR jobs archive into /var/lib/dalton/asr_jobs")
    idx_rec = restore_sh.index("Running post-restore inflight state convergence (reconcile-restored-inflight-states)")
    idx_up = restore_sh.index('info "7/7 Starting services and running health checks..."\ncompose_cmd up -d')
    assert idx_asr < idx_rec < idx_up


def test_restored_transcription_blocks_submit_and_retry_allows_only_resubmit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lead #3 (Codex #6): persisted restored_no_resume marker blocks normal submit and /retry,
    while allowing /resubmit to create one canonical replacement attempt."""
    # 1. Verify reconcile_restored_inflight_states SQL persists usage.restored_no_resume = true
    sql_seen: list[str] = []

    def _rec_handler(sql: str, params: Any) -> Any:
        s = " ".join(sql.split())
        sql_seen.append(s)
        if s.startswith("SELECT to_regclass"):
            return (params[0],) if params and params[0] == "public.dalton_transcription_jobs" else (None,)
        if "UPDATE dalton_transcription_jobs" in s:
            return 1
        return []

    conn = _FakeAsyncConn(_rec_handler)
    summary = asyncio.run(consistent_backup.reconcile_restored_inflight_states(conn))
    assert summary.transcriptions_failed == 1
    assert any(
        "UPDATE dalton_transcription_jobs" in s and '"restored_no_resume": true' in s
        for s in sql_seen
    )

    # 2. Service-level guards: submit_transcription_job (with job_id AND via prepare collision)
    #    and retry_transcription_job MUST reject with 409 without calling bridge.
    from fastapi import HTTPException

    audio_bytes = b"restored-audio-bytes"
    restored_row: dict[str, Any] = {
        "job_id": "tr-restored-1",
        "created_by_user_id": "user-1",
        "workspace_id": "ws-1",
        "thread_id": "th-1",
        "status": "failed",
        "stage": "failed",
        "version": 3,
        "source_object_key": "transcriptions/ws-1/tr-restored-1/source.m4a",
        "source_size_bytes": len(audio_bytes),
        "source_sha256": _sha(audio_bytes),
        "idempotency_key": "idem-restored-1",
        "language": "zh",
        "hotwords": [],
        "error_category": "restored_inflight",
        "error_message": consistent_backup.RESTORED_INFLIGHT_ERROR_MESSAGE,
        "usage": {"upload": {"completed": True}, "restored_no_resume": True},
    }

    bridge_submit_mock = create_autospec(tr_svc.call_bridge_submit_transcription)
    bridge_action_mock = create_autospec(tr_svc.call_bridge_action_transcription)

    async def _fake_require_caller(uid: str) -> str:
        return uid

    async def _fake_auth(_job: dict[str, Any], _uid: str, need: str = "read") -> None:
        return None

    async def _fake_get_job(jid: str) -> dict[str, Any] | None:
        return dict(restored_row) if jid == "tr-restored-1" else None

    async def _fake_prepare(**_kw: Any) -> tuple[dict[str, Any], str, int]:
        return dict(restored_row), "tok", 9999999999

    monkeypatch.setattr(tr_svc, "require_internal_transcription_caller", _fake_require_caller)
    monkeypatch.setattr(tr_svc, "authorize_job_access", _fake_auth)
    monkeypatch.setattr(tr_db, "get_transcription_job", _fake_get_job)
    monkeypatch.setattr(tr_svc, "prepare_transcription_job", _fake_prepare)
    monkeypatch.setattr(tr_svc, "is_transcription_storage_configured", lambda: True)
    monkeypatch.setattr(tr_svc, "object_size", lambda _k: len(audio_bytes))
    monkeypatch.setattr(tr_svc, "call_bridge_submit_transcription", bridge_submit_mock)
    monkeypatch.setattr(tr_svc, "call_bridge_action_transcription", bridge_action_mock)

    # Normal submit with job_id -> 409, bridge never called
    with pytest.raises(HTTPException) as exc_sub1:
        asyncio.run(tr_svc.submit_transcription_job(caller_user_id="user-1", job_id="tr-restored-1"))
    assert exc_sub1.value.status_code == 409
    assert "/resubmit" in str(exc_sub1.value.detail)

    # Normal submit without job_id (collides via prepare_transcription_job on restored row) -> 409
    with pytest.raises(HTTPException) as exc_sub2:
        asyncio.run(
            tr_svc.submit_transcription_job(
                caller_user_id="user-1",
                workspace_id="ws-1",
                source_object_key=restored_row["source_object_key"],
                source_size_bytes=len(audio_bytes),
                source_sha256=_sha(audio_bytes),
            )
        )
    assert exc_sub2.value.status_code == 409

    # /retry on restored row -> 409, bridge never called
    with pytest.raises(HTTPException) as exc_retry:
        asyncio.run(tr_svc.retry_transcription_job(job_id="tr-restored-1", caller_user_id="user-1"))
    assert exc_retry.value.status_code == 409
    assert "/resubmit" in str(exc_retry.value.detail)

    assert bridge_submit_mock.call_count == 0
    assert bridge_action_mock.call_count == 0

    # 3. Disk-level ASR job manager also refuses submit & retry for restored_no_resume directories
    mgr = AsrJobManager(jobs_dir=tmp_path / "asr_mgr", worker=FakeAsrWorker())
    jdir = tmp_path / "asr_mgr" / "tr-restored-1"
    jdir.mkdir(parents=True)
    (jdir / "state.json").write_text(
        json.dumps(
            {
                "job_id": "tr-restored-1",
                "status": "failed",
                "stage": "failed",
                "error_category": "restored_inflight",
                "restored_no_resume": True,
                "caller": {"user_id": "user-1", "workspace_id": "ws-1", "thread_id": "th-1"},
            }
        ),
        encoding="utf-8",
    )
    code_r, body_r = asyncio.run(mgr.retry_job("tr-restored-1"))
    assert code_r == 409 and body_r["error_code"] == "restored_no_resume"
    code_s, body_s = asyncio.run(
        mgr.submit_job(
            {
                "job_id": "tr-restored-1",
                "caller": {"user_id": "user-1", "workspace_id": "ws-1", "thread_id": "th-1"},
                "source_object_key": "transcriptions/ws-1/tr-restored-1/source.m4a",
                "source_size_bytes": len(audio_bytes),
                "source_sha256": _sha(audio_bytes),
                "idempotency_key": "idem-restored-1",
            }
        )
    )
    assert code_s == 409 and body_s["error_code"] == "restored_no_resume"


def test_weekly_baseline_immutable_per_baseline_manifest_survives_same_day_rerun(
    tmp_path: Path,
) -> None:
    """Lead #4 (Codex #5): each baseline_id stores an independent remote checksum manifest;
    same-day weekly rerun updates LATEST.json without overwriting the earlier baseline manifest."""
    ext = tmp_path / "EveSSD"
    backups_root = ext / "Dalton" / "backups"
    remote_root = tmp_path / "gdrive"
    prod_var = tmp_path / "prod_var"
    prod_var.mkdir(parents=True)
    (prod_var / "note.txt").write_text("var-data\n", encoding="utf-8")
    key_file = tmp_path / "backup.key"
    key_file.write_text("test-passphrase-r3\n", encoding="utf-8")
    key_file.chmod(0o600)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # Fake rclone backed by local directory tree
    (fake_bin / "rclone").write_text(
        f"""#!/usr/bin/env python3
import hashlib, json, os, shutil, sys
from pathlib import Path
root = Path({str(remote_root)!r})
args = [a for a in sys.argv[1:] if not a.startswith("--")]
# Filter out flag values for known flags
clean = []
skip = False
for a in sys.argv[1:]:
    if skip:
        skip = False
        continue
    if a in ("--cache-dir", "--temp-dir", "--log-file", "--log-level", "--size", "--drive-chunk-size"):
        skip = True
        continue
    if a.startswith("--"):
        continue
    clean.append(a)
cmd = clean[0]
def to_p(spec):
    return root / spec.split(":", 1)[1].lstrip("/")
if cmd == "listremotes":
    print("gdrive:")
elif cmd == "rcat":
    p = to_p(clean[1])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(sys.stdin.buffer.read())
elif cmd == "lsjson":
    p = to_p(clean[1])
    print(json.dumps([{{"Name": p.name, "Size": p.stat().st_size}}]))
elif cmd == "hashsum":
    p = to_p(clean[2])
    md5 = hashlib.md5(p.read_bytes()).hexdigest()
    print(f"{{md5}}  {{p.name}}")
elif cmd == "moveto":
    src, dst = to_p(clean[1]), to_p(clean[2])
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
elif cmd == "deletefile":
    p = to_p(clean[1])
    if p.exists():
        p.unlink()
elif cmd == "lsf":
    p = to_p(clean[1])
    if p.is_dir():
        for c in sorted(p.iterdir()):
            if "--dirs-only" in sys.argv and not c.is_dir():
                continue
            if "--files-only" in sys.argv and not c.is_file():
                continue
            print(c.name + ("/" if c.is_dir() else ""))
elif cmd == "cat":
    print(to_p(clean[1]).read_text(encoding="utf-8"), end="")
""",
        encoding="utf-8",
    )
    (fake_bin / "rclone").chmod(0o755)
    (fake_bin / "docker").write_text(
        """#!/usr/bin/env bash
case "$1" in
  info) exit 0 ;;
  ps) exit 0 ;;
  exec)
    if [[ "$*" == *"--rdb"* ]] || [[ "$*" == *"cat /data/dump.rdb"* ]]; then printf 'REDIS0011'; exit 0; fi
    if [[ "$*" == *"redis-cli"* ]]; then exit 0; fi
    exit 0 ;;
  *) exit 0 ;;
esac
""",
        encoding="utf-8",
    )
    (fake_bin / "docker").chmod(0o755)
    # Fake python wrapper that passes through to real venv python except verify-mount & weekly-minio-baseline
    fake_py = tmp_path / "fake_py.py"
    fake_py.write_text(
        f"""#!{sys.executable}
import json, os, subprocess, sys, tarfile, io
from pathlib import Path
if len(sys.argv) >= 3 and sys.argv[2] == "verify-mount":
    sys.exit(0)
if len(sys.argv) >= 3 and sys.argv[2] == "check-rclone":
    print("ok")
    sys.exit(0)
if len(sys.argv) >= 3 and sys.argv[2] == "weekly-minio-baseline":
    bid = sys.argv[sys.argv.index("--baseline-id") + 1]
    out_tar = Path(sys.argv[sys.argv.index("--out-tar") + 1])
    mout = Path(sys.argv[sys.argv.index("--manifest-out") + 1])
    with tarfile.open(out_tar, "w:gz") as tf:
        info = tarfile.TarInfo("objects/b/k")
        payload = bid.encode("utf-8")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    mout.write_text(json.dumps({{"version": 2, "baseline_id": bid, "objects": []}}), encoding="utf-8")
    print(json.dumps({{"baseline_id": bid, "object_count": 1}}))
    sys.exit(0)
sys.exit(subprocess.call([{sys.executable!r}, *sys.argv[1:]]))
""",
        encoding="utf-8",
    )
    fake_py.chmod(0o755)

    run_env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "DALTON_OFFSITE_ENV_FILE": str(tmp_path / "nonexistent-offsite.env"),
        "DALTON_DATA_BACKUP_LOAD_PROD_ENV": "0",
        "DALTON_EXTERNAL_MOUNT_ROOT": str(ext),
        "DALTON_BACKUP_ROOT": str(backups_root),
        "DALTON_BACKUP_KEY_FILE": str(key_file),
        "DALTON_PROD_VAR_DIR": str(prod_var),
        "DALTON_REPO_PYTHON": str(fake_py),
        "DALTON_RCLONE_REMOTE": "gdrive",
        "DALTON_RCLONE_DRIVE_DIR": "DaltonBackup",
        "DALTON_OFFSITE_BACKUP_LOG_FILE": str(tmp_path / "offsite.log"),
        "DALTON_BACKUP_ALERTS_FILE": str(tmp_path / "alerts.log"),
    }
    ext.mkdir(parents=True)
    (backups_root / "weekly").mkdir(parents=True)

    r1 = subprocess.run(
        ["bash", str(PROD_DIR / "data_backup.sh")],
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r1.returncode == 0, r1.stdout + r1.stderr

    weekly_dir = next((remote_root / "DaltonBackup" / "weekly").iterdir())
    latest_1 = json.loads((weekly_dir / "LATEST.json").read_text(encoding="utf-8"))
    bid_1 = latest_1["baseline_id"]
    m1_path = weekly_dir / f"baseline-{bid_1}.json"
    assert m1_path.is_file()
    m1_before = m1_path.read_text(encoding="utf-8")

    # Daily LATEST.json referencing bid_1 directly cites weekly/<date>/baseline-<bid_1>.json
    daily_meta = offsite_sync.build_daily_latest_json(
        run_id="run-d1",
        date_stamp=weekly_dir.name,
        backup_set_id="set-d1",
        baseline_id=bid_1,
        artifacts={"dump": {"filename": "d.enc"}},
    )
    assert daily_meta["baseline_manifest"] == f"baseline-{bid_1}.json"
    assert daily_meta["baseline_manifest_path"] == f"weekly/{weekly_dir.name}/baseline-{bid_1}.json"

    # Second weekly run on the SAME day
    r2 = subprocess.run(
        ["bash", str(PROD_DIR / "data_backup.sh")],
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r2.returncode == 0, r2.stdout + r2.stderr
    latest_2 = json.loads((weekly_dir / "LATEST.json").read_text(encoding="utf-8"))
    bid_2 = latest_2["baseline_id"]
    assert bid_2 != bid_1
    m2_path = weekly_dir / f"baseline-{bid_2}.json"
    assert m2_path.is_file()
    # Run 1's per-baseline manifest is COMPLETELY unchanged!
    assert m1_path.read_text(encoding="utf-8") == m1_before
    m1_data = json.loads(m1_path.read_text(encoding="utf-8"))
    assert m1_data["artifacts"]["minio"]["filename"] != latest_2["artifacts"]["minio"]["filename"]


def test_workspace_file_uploads_pulling_snapshot_refs_and_restore_convergence(
    tmp_path: Path,
) -> None:
    """Lead #5 (Codex #7): `workspace_file_uploads` pulling rows enter snapshot refs (surviving
    post-snapshot DeleteMarker) and post-restore convergence re-queues verified pulling rows and
    aborts remaining uploading/completing/missing-pulling rows."""
    pull_bytes = b"assembled-workspace-upload-payload"
    pull_sha = _sha(pull_bytes)
    pull_key = "workspace-uploads/ws1/u-pull-1"

    def _backup_db(sql: str, params: Any) -> Any:
        s = " ".join(sql.split())
        if s.startswith("SELECT pg_export_snapshot"):
            return ("00000044-00000001-1",)
        if s.startswith("SELECT pg_advisory_lock") or s.startswith("SELECT pg_advisory_unlock"):
            return (True, True)
        if s.startswith("SELECT to_regclass"):
            return (params[0],) if params and params[0] == "public.workspace_file_uploads" else (None,)
        if "FROM workspace_file_uploads" in s:
            return [
                ("u-pull-1", pull_key, len(pull_bytes), pull_sha, "pulling"),
                ("u-up-1", "workspace-uploads/ws1/u-up-1", 500, None, "uploading"),
                ("u-comp-1", "workspace-uploads/ws1/u-comp-1", 600, None, "completing"),
            ]
        return []

    s3 = _FakeS3Client(versioning_by_bucket={"dalton-main": True})
    s3.seed_object("dalton-main", pull_key, pull_bytes)

    def _delete_after_snapshot(_snap_id: str, inv: storage_refs.SnapshotRefInventory) -> None:
        assert inv.skipped_pending.get("workspace_file_uploads") == 2
        assert any(r.consumer == "workspace_file_uploads" and r.object_key == pull_key for r in inv.references)
        # Simulate pull finishing right after snapshot and deleting temp object -> DeleteMarker
        s3.delete_object_marker("dalton-main", pull_key)

    res = asyncio.run(
        consistent_backup.run_consistent_backup(
            conn=_FakeAsyncConn(_backup_db),
            pg_dump_path=tmp_path / "ws-pull.dump",
            pg_dump_fn=lambda _s, p: p.write_bytes(b"PGDMP"),
            bucket_specs=[
                consistent_backup.BucketArchiveSpec(
                    bucket="dalton-main", roles=("main",), s3_client=s3
                )
            ],
            object_sink=consistent_backup.MirrorPoolSink(tmp_path / "mirror-ws"),
            after_snapshot_commit_hook=_delete_after_snapshot,
        )
    )
    assert res.reconcile_report.ok is True
    assert any(r.key == pull_key and r.archived_sha256 == pull_sha for r in res.manifest_objects)

    # Post-restore convergence: external check runs outside DB tx, verified pulling row is re-queued,
    # while unverified pulling / uploading / completing rows are explicitly aborted.
    inspector_in_tx: list[bool] = []
    requeued_ids: list[str] = []
    aborted_excluded: list[Any] = []
    conn_rec: _FakeAsyncConn

    def _rec_db(sql: str, params: Any) -> Any:
        s = " ".join(sql.split())
        if s.startswith("SELECT to_regclass"):
            return (params[0],) if params and params[0] == "public.workspace_file_uploads" else (None,)
        if s.startswith("SELECT upload_id, object_key, size, sha256 FROM workspace_file_uploads"):
            return [
                ("u-pull-ok", pull_key, len(pull_bytes), pull_sha),
                ("u-pull-missing", "workspace-uploads/ws1/missing", 123, None),
            ]
        if s.startswith("SELECT upload_id FROM workspace_file_uploads WHERE upload_id = %s"):
            return (params[0],)
        if "UPDATE workspace_file_uploads SET lease_until = NULL, placing_inode = NULL, error = NULL" in s:
            requeued_ids.append(params[0])
            return 1
        if "UPDATE workspace_file_uploads SET state = 'aborted'" in s:
            aborted_excluded.append(params)
            return 3
        return []

    conn_rec = _FakeAsyncConn(_rec_db)

    def _inspect_ws(key: str) -> tuple[int, str] | None:
        inspector_in_tx.append(conn_rec.in_tx)
        if key == pull_key:
            return (len(pull_bytes), pull_sha)
        return None

    summary = asyncio.run(
        consistent_backup.reconcile_restored_inflight_states(
            conn_rec,
            inspect_workspace_upload_object_fn=_inspect_ws,
        )
    )
    assert inspector_in_tx == [False, False]
    assert requeued_ids == ["u-pull-ok"]
    assert aborted_excluded == [(["u-pull-ok"],)]
    assert summary.workspace_uploads_requeued == 1
    assert summary.workspace_uploads_aborted == 3


def test_colima_resolution_respects_colima_home_active_profile_and_requires_disk_file(
    tmp_path: Path,
) -> None:
    """Lead #6 (Codex #8): production preflight resolves actual running Colima config respecting
    COLIMA_HOME, uses it for both disk location and free-space checks, and fails if no disk file exists."""
    ext_mount = tmp_path / "EveSSD"
    internal_root = tmp_path / "internal_root"
    ext_mount.mkdir()
    internal_root.mkdir()
    gb = 1024 * 1024 * 1024

    def _split_dev_stat(p: Path) -> Any:
        is_ext = str(p.resolve(strict=False)).startswith(str(ext_mount.resolve()))
        return type("Stat", (), {"st_dev": 200 if is_ext else 100})()

    # 1. COLIMA_HOME points to internal disk while ~/.colima was on external -> MUST FAIL
    colima_internal = internal_root / "colima-home"
    (colima_internal / "_lima" / "colima-prod").mkdir(parents=True)
    (colima_internal / "_lima" / "colima-prod" / "diffdisk").write_bytes(b"qcow")

    seen_cmds: list[tuple[list[str], str]] = []

    def _fake_colima_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen_cmds.append((list(cmd), str((kwargs.get("env") or {}).get("COLIMA_HOME") or "")))
        if cmd[:3] == ["colima", "list", "--json"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='{"name":"default","status":"Stopped"}\n{"name":"prod","status":"Running"}\n',
                stderr="",
            )
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/vda1 300000000 100000000 83886080 28% /var/lib/docker\n",
            stderr="",
        )

    runner_mock = create_autospec(subprocess.run, side_effect=_fake_colima_run)
    rep_internal = backup_lib.preflight_disk_space(
        external_mount_root=ext_mount,
        internal_root=internal_root,
        env={"COLIMA_HOME": str(colima_internal)},
        is_mount_fn=lambda p: p == ext_mount,
        disk_usage_fn=lambda _p: shutil._ntuple_diskusage(500 * gb, 100 * gb, 300 * gb),
        stat_fn=_split_dev_stat,
        colima_run_fn=runner_mock,
    )
    assert rep_internal.ok is False
    loc_chk = next(c for c in rep_internal.checks if c.name == "colima_location")
    assert loc_chk.ok is False
    assert "prod" in loc_chk.detail
    # Both `colima list --json` and `colima ssh --profile prod` used the resolved COLIMA_HOME
    assert any(c[0][:4] == ["colima", "ssh", "--profile", "prod"] and c[1] == str(colima_internal) for c in seen_cmds)

    # 2. COLIMA_HOME on external SSD with active profile `prod` but NO disk file -> MUST FAIL
    colima_empty_ext = ext_mount / "colima-empty"
    (colima_empty_ext / "_lima" / "colima-prod").mkdir(parents=True)
    rep_no_disk = backup_lib.preflight_disk_space(
        external_mount_root=ext_mount,
        internal_root=internal_root,
        env={"COLIMA_HOME": str(colima_empty_ext)},
        is_mount_fn=lambda p: p == ext_mount,
        disk_usage_fn=lambda _p: shutil._ntuple_diskusage(500 * gb, 100 * gb, 300 * gb),
        stat_fn=_split_dev_stat,
        colima_run_fn=runner_mock,
    )
    assert rep_no_disk.ok is False
    no_disk_chk = next(c for c in rep_no_disk.checks if c.name == "colima_location")
    assert no_disk_chk.ok is False
    assert "No Colima virtual disk file" in no_disk_chk.detail

    # 3. Adding actual diffdisk file under active `prod` profile on external SSD -> PASSES
    (colima_empty_ext / "_lima" / "colima-prod" / "diffdisk").write_bytes(b"qcow2")
    rep_ok = backup_lib.preflight_disk_space(
        external_mount_root=ext_mount,
        internal_root=internal_root,
        env={"COLIMA_HOME": str(colima_empty_ext)},
        is_mount_fn=lambda p: p == ext_mount,
        disk_usage_fn=lambda _p: shutil._ntuple_diskusage(500 * gb, 100 * gb, 300 * gb),
        stat_fn=_split_dev_stat,
        colima_run_fn=runner_mock,
    )
    assert rep_ok.ok is True


def test_deploy_main_wait_covers_all_phases_and_resumes_tracked_backup_on_retry(
    tmp_path: Path,
) -> None:
    """Lead #7 (Codex #9): deploy wait covers lock + mirror + margin (25200s default) and records
    the waited-on backup ID so retrying a migrating deploy after completion resumes without a new backup."""
    from tests.unit.dalton.test_deploy_main_reuse_backup import _run_deploy, _setup_deploy_env

    # 1. Default wait duration covers lock (7200) + mirror (14400) + publish margin (3600) = 25200s
    deploy_src = (PROD_DIR / "deploy_main.sh").read_text(encoding="utf-8")
    assert "_default_backup_wait_s=25200" in deploy_src
    assert "_lock_wait_t + _mirror_wait_t + 3600" in deploy_src

    # 2. Migrating deploy (pending dalton migration d041):
    #    Attempt 1 kicks off backup, waits 1s, sees job still running -> exits 75 & saves WAIT_BACKUP_ID.
    ctx = _setup_deploy_env(tmp_path / "dep-resume")
    seed = Path(ctx["seed"])
    (seed / "src/dalton/migrations/versions/d041_next.py").write_text(
        "revision = 'd041'\ndown_revision = 'd040'\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "add d041"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "dalton/main"], check=True)

    state_file = tmp_path / "deploy-wait.state"
    next_id_file = tmp_path / "next-set-id"
    running_flag = tmp_path / "backup.running"
    running_flag.write_text("1", encoding="utf-8")

    (Path(ctx["fake_bin"]) / "launchctl").write_text(
        f"""#!/bin/bash
echo "$*" >> "{ctx['launchctl_trace']}"
if [ "$1" = "print" ]; then
  if [ -f "{running_flag}" ]; then
    echo "    state = running"
    echo "    pid = 51515"
    exit 0
  fi
  exit 1
fi
exit 0
""",
        encoding="utf-8",
    )
    (Path(ctx["fake_bin"]) / "launchctl").chmod(0o755)

    extra = (
        f'DALTON_DEPLOY_BACKUP_WAIT_S=1; DALTON_DEPLOY_BACKUP_POLL_S=1; '
        f'DALTON_DEPLOY_BACKUP_WAIT_STATE_FILE="{state_file}"; '
        f'DALTON_PG_BACKUP_NEXT_ID_FILE="{next_id_file}"'
    )
    # First attempt: backup is kicked off once, still running after 1s -> exit 75
    # (simulate launchctl print returning not-running before kickstart, then running after kickstart)
    running_flag.unlink()
    (Path(ctx["fake_bin"]) / "launchctl").write_text(
        f"""#!/bin/bash
echo "$*" >> "{ctx['launchctl_trace']}"
if [ "$1" = "kickstart" ]; then
  touch "{running_flag}"
  exit 0
fi
if [ "$1" = "print" ]; then
  if [ -f "{running_flag}" ]; then
    echo "    state = running"
    echo "    pid = 51515"
    exit 0
  fi
  exit 1
fi
exit 0
""",
        encoding="utf-8",
    )
    (Path(ctx["fake_bin"]) / "launchctl").chmod(0o755)

    res1 = _run_deploy(ctx, tmp_path / "dep-resume", extra_setup=extra)
    assert res1.returncode == 75, res1.stdout + res1.stderr
    assert state_file.is_file(), "Wait state file must persist on exit 75"
    assert next_id_file.is_file()
    tracked_id = next_id_file.read_text(encoding="utf-8").strip()
    assert f"WAIT_BACKUP_ID={tracked_id}" in state_file.read_text(encoding="utf-8")
    kickstarts_after_1 = [
        ln for ln in Path(ctx["launchctl_trace"]).read_text(encoding="utf-8").splitlines()
        if "kickstart" in ln
    ]
    assert len(kickstarts_after_1) == 1

    # Now the tracked backup completes and writes its terminal ok line with set=<tracked_id>
    running_flag.unlink()
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with Path(ctx["backup_log"]).open("a", encoding="utf-8") as lf:
        lf.write(f"{now_str} ok  16G /backups/dalton-pg-{tracked_id}.dump set={tracked_id}\n")

    # Retry migrating deploy: MUST resume the completed tracked backup without starting a new one!
    res2 = _run_deploy(
        ctx,
        tmp_path / "dep-resume",
        extra_setup=extra,
    )
    assert "resuming completed deploy backup" in res2.stdout, res2.stdout + res2.stderr
    assert tracked_id in res2.stdout
    kickstarts_after_2 = [
        ln for ln in Path(ctx["launchctl_trace"]).read_text(encoding="utf-8").splitlines()
        if "kickstart" in ln
    ]
    assert len(kickstarts_after_2) == 1, f"Retry must NOT trigger a second backup: {kickstarts_after_2}"
    assert not state_file.exists(), "State file should be cleared once completed backup is resumed"
