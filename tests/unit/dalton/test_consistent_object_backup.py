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

    def cursor(self) -> _FakeAsyncCursor:
        return _FakeAsyncCursor(self)

    def transaction(self) -> _FakeAsyncTx:
        return _FakeAsyncTx(self)


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
    colima_real.mkdir(parents=True)
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

    def _seed_pool(data: bytes) -> str:
        sha = _sha(data)
        p = pool_dir / sha[:2] / sha
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
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
                size=10,
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
