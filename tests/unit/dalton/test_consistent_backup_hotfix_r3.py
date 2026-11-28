"""Unit tests for Backup Hotfix Round 3 (Codex review findings #1-#7).

Covers:
1. Transcription source audio exemption: uses snapshot time vs verifiable upload/job
   completion time (never ``created_at`` or ``updated_at``); only exempts keys under
   a secondary bucket covered by the managed 7-day rule AND ``<prefix>transcriptions/``;
   records exempted keys in ``expired_by_rule_objects`` (no alert) across manifest/set.
2. Physical primary bucket identification by ``(endpoint, bucket_name)``: zero tolerance
   across ALL prefixes on the primary bucket, including prefixes shared with aux configs.
3. ``verify-restored-refs`` queries restore target bucket(s), fails when any manifest
   object is absent from target bucket, and accepts known_missing strictly from the
   same backup set's manifest (excluding any object present in manifest ``objects``).
4. Strict source audio check for in-flight, uploaded/submitted ``awaiting_upload``,
   and ``indeterminate`` ASR jobs: missing audio fails immediately (never tolerated),
   while un-uploaded ``awaiting_upload`` jobs are not enumerated.
5. Standalone Core ``restore.sh`` error classification: only explicit 404 / NoSuchKey /
   NotFound matches known missing; 403 and 5xx remain verification failures.
6. Unified transcription prefix (``DALTON_ASR_STORAGE_KEY_PREFIX``) across
   ``configure_minio.sh`` and ``minio_init.py``; lifecycle read-back rejects rules with
   extra tag filters or extra actions.
7. Standalone Core alert persistence & concise summary: ``compose-backup`` does not
   write ephemeral container alerts and emits only count + top 10 keys; host
   ``backup.sh`` writes one persistent alert on the host and keeps full list in manifest.

All test doubles use ``create_autospec``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, create_autospec

import boto3
import psycopg
import pytest
from botocore.exceptions import ClientError

from scripts.dalton.ops import backup_lib
from scripts.dalton.ops.prod import local_backup_set
from scripts.standalone import consistent_backup as standalone_cb
from src.dalton import consistent_backup, storage_bucket_rules, storage_refs

REPO_ROOT = Path(__file__).resolve().parents[3]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _S3ClientContract:
    def get_bucket_versioning(self, *, Bucket: str) -> dict[str, Any]: ...
    def list_object_versions(
        self, *, Bucket: str, Prefix: str = "", **kwargs: Any
    ) -> dict[str, Any]: ...
    def list_objects_v2(
        self, *, Bucket: str, Prefix: str = "", **kwargs: Any
    ) -> dict[str, Any]: ...
    def get_object(
        self, *, Bucket: str, Key: str, VersionId: str | None = None
    ) -> dict[str, Any]: ...
    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str = "application/octet-stream",
    ) -> dict[str, Any]: ...


def _make_autospec_s3_client(
    *,
    versioning_by_bucket: dict[str, bool],
    objects_by_bucket: dict[str, dict[str, bytes]] | None = None,
    list_v2_error_by_bucket: dict[str, Exception] | None = None,
) -> Any:
    store: dict[str, dict[str, tuple[bytes, str]]] = {
        b: {k: (v, "application/octet-stream") for k, v in objs.items()}
        for b, objs in (objects_by_bucket or {}).items()
    }
    err_map = dict(list_v2_error_by_bucket or {})
    client = create_autospec(_S3ClientContract, instance=True)

    def _get_ver(*, Bucket: str) -> dict[str, Any]:
        return {"Status": "Enabled"} if versioning_by_bucket.get(Bucket, False) else {}

    def _list_vers(*, Bucket: str, Prefix: str = "", **_kw: Any) -> dict[str, Any]:
        versions = []
        for k, (data, _ct) in sorted(store.get(Bucket, {}).items()):
            if Prefix and not k.startswith(Prefix):
                continue
            versions.append(
                {
                    "Key": k,
                    "VersionId": "v1" if versioning_by_bucket.get(Bucket, False) else "null",
                    "IsLatest": True,
                    "Size": len(data),
                    "LastModified": dt.datetime(2026, 11, 27, 3, 0, 0, tzinfo=dt.timezone.utc),
                }
            )
        return {"Versions": versions, "DeleteMarkers": [], "IsTruncated": False}

    def _list_v2(*, Bucket: str, Prefix: str = "", **_kw: Any) -> dict[str, Any]:
        if Bucket in err_map:
            raise err_map[Bucket]
        contents = []
        for k, (data, _ct) in sorted(store.get(Bucket, {}).items()):
            if Prefix and not k.startswith(Prefix):
                continue
            contents.append(
                {
                    "Key": k,
                    "Size": len(data),
                    "LastModified": dt.datetime(2026, 11, 27, 3, 0, 0, tzinfo=dt.timezone.utc),
                }
            )
        return {"Contents": contents, "IsTruncated": False}

    def _get_obj(*, Bucket: str, Key: str, VersionId: str | None = None) -> dict[str, Any]:
        entry = store.get(Bucket, {}).get(Key)
        if entry is None:
            raise KeyError(f"NoSuchKey: {Bucket}/{Key}")
        data, ct = entry
        return {"Body": io.BytesIO(data), "ContentType": ct}

    def _put_obj(
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str = "application/octet-stream",
    ) -> dict[str, Any]:
        store.setdefault(Bucket, {})[Key] = (bytes(Body), ContentType)
        return {"ETag": '"ok"'}

    client.get_bucket_versioning.side_effect = _get_ver
    client.list_object_versions.side_effect = _list_vers
    client.list_objects_v2.side_effect = _list_v2
    client.get_object.side_effect = _get_obj
    client.put_object.side_effect = _put_obj
    return client


def _make_autospec_async_db_conn(rows_by_query_fn: Any) -> Any:
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    last_result: dict[str, Any] = {"val": []}

    async def _execute(sql: Any, params: Any = None, **_kw: Any) -> Any:
        last_result["val"] = rows_by_query_fn(str(sql), params)
        return cur

    async def _fetchone() -> Any:
        val = last_result["val"]
        if isinstance(val, list):
            return val[0] if val else None
        return val

    async def _fetchall() -> list[Any]:
        val = last_result["val"]
        if isinstance(val, list):
            return list(val)
        return [val] if val is not None else []

    cur.execute = AsyncMock(side_effect=_execute)
    cur.fetchone = AsyncMock(side_effect=_fetchone)
    cur.fetchall = AsyncMock(side_effect=_fetchall)
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__ = AsyncMock(return_value=False)

    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.cursor.return_value = cur
    conn.close = AsyncMock(return_value=None)
    return conn


def _pg_dump_signature(snapshot_id: str, out_path: Path) -> None: ...


def _alert_signature(code: str, message: str) -> Any: ...


def _make_pg_dump_spy() -> Any:
    spy = create_autospec(_pg_dump_signature)

    def _write_dump(snapshot_id: str, out_path: Path) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(f"PGDMP:{snapshot_id}".encode("utf-8"))

    spy.side_effect = _write_dump
    return spy


# ---------------------------------------------------------------------------
# 1. Transcription audio exemption: verifiable upload/completion time, bucket+prefix, manifest record
# ---------------------------------------------------------------------------


def test_r3_transcription_exemption_uses_verifiable_time_and_covered_bucket_prefix_and_records_manifest(
    tmp_path: Path,
) -> None:
    snap_now = dt.datetime(2026, 11, 28, 12, 0, 0, tzinfo=dt.timezone.utc)
    audio = b"asr-source-audio-bytes"
    audio_sha = _sha(audio)

    def _row(
        jid: str,
        status: str,
        key: str,
        *,
        usage: dict[str, Any],
        created_at: dt.datetime | None,
        completed_at: dt.datetime | None,
        updated_at: dt.datetime | None = None,
    ) -> tuple[Any, ...]:
        return (
            jid,
            status,
            key,
            len(audio),
            audio_sha,
            usage,
            created_at,
            completed_at,
            updated_at,
        )

    rows = [
        # Created 8d ago, uploaded & completed today -> MUST NOT be exempted!
        _row(
            "job-created-8d-uploaded-today",
            "succeeded",
            "transcriptions/ws1/job-created-8d-uploaded-today/source/call.m4a",
            usage={"upload": {"completed": True, "completed_at": (snap_now - dt.timedelta(hours=2)).isoformat()}},
            created_at=snap_now - dt.timedelta(days=8),
            completed_at=snap_now - dt.timedelta(hours=1),
        ),
        # Created 10d ago, no upload timestamp, completed_at today -> MUST NOT be exempted!
        _row(
            "job-created-10d-completed-today",
            "succeeded",
            "transcriptions/ws1/job-created-10d-completed-today/source/call.m4a",
            usage={"upload": {"completed": True}},
            created_at=snap_now - dt.timedelta(days=10),
            completed_at=snap_now - dt.timedelta(hours=3),
        ),
        # Created 10d ago, completed_at=None, updated_at=10d ago -> unverifiable -> MUST NOT be exempted!
        _row(
            "job-unverifiable-ts",
            "succeeded",
            "transcriptions/ws1/job-unverifiable-ts/source/call.m4a",
            usage={"upload": {"completed": True}},
            created_at=snap_now - dt.timedelta(days=10),
            completed_at=None,
            updated_at=snap_now - dt.timedelta(days=10),
        ),
        # upload.completed=False with stale upload.completed_at=10d ago, completed_at=1d ago -> MUST NOT trust upload ts!
        _row(
            "job-untrusted-upload-ts",
            "succeeded",
            "transcriptions/ws1/job-untrusted-upload-ts/source/call.m4a",
            usage={"upload": {"completed": False, "completed_at": (snap_now - dt.timedelta(days=10)).isoformat()}},
            created_at=snap_now - dt.timedelta(days=10),
            completed_at=snap_now - dt.timedelta(days=1),
        ),
        # Legacy uploads/ prefix even completed 30d ago -> NOT covered by transcriptions/ 7d rule!
        _row(
            "job-legacy-uploads-30d",
            "succeeded",
            "uploads/ws1/job-legacy-uploads-30d/call.m4a",
            usage={"upload": {"completed": True}},
            created_at=snap_now - dt.timedelta(days=30),
            completed_at=snap_now - dt.timedelta(days=30),
        ),
        # Covered transcriptions/ key, verifiable completed_at > 7d -> EXEMPTED & recorded
        _row(
            "job-expired-by-completed-at",
            "succeeded",
            "transcriptions/ws1/job-expired-by-completed-at/source/call.m4a",
            usage={"upload": {"completed": True}},
            created_at=snap_now - dt.timedelta(days=9),
            completed_at=snap_now - dt.timedelta(days=8),
        ),
        # Covered transcriptions/ key, verifiable upload.completed_at > 7d -> EXEMPTED & recorded
        _row(
            "job-expired-by-upload-ts",
            "succeeded",
            "transcriptions/ws1/job-expired-by-upload-ts/source/call.m4a",
            usage={"upload": {"completed": True, "completed_at": (snap_now - dt.timedelta(days=8)).isoformat()}},
            created_at=snap_now - dt.timedelta(days=9),
            completed_at=snap_now - dt.timedelta(days=6),
        ),
    ]

    def _query_handler(sql: str, params: Any) -> Any:
        s = " ".join(sql.split())
        if s.startswith("SELECT pg_export_snapshot"):
            return ("00000099-00000001-1",)
        if s.startswith("SELECT pg_advisory_lock") or s.startswith("SELECT pg_advisory_unlock"):
            return (True, True)
        if s.startswith("SELECT to_regclass"):
            tbl = params[0] if params else ""
            return (tbl,) if tbl == "public.dalton_transcription_jobs" else (None,)
        if "FROM dalton_transcription_jobs" in s:
            return rows
        return []

    # Case A: Unknown ASR bucket (no env) -> cannot determine -> NONE exempted
    inv_unknown = asyncio.run(
        storage_refs.enumerate_snapshot_refs(
            _make_autospec_async_db_conn(_query_handler),
            env={},
            include_main=False,
            include_aux=True,
            now=snap_now,
        )
    )
    assert len(inv_unknown.references) == len(rows)
    assert inv_unknown.expired_by_rule_objects == []

    # Case B: ASR shares physical primary bucket (same endpoint + bucket as main) -> NONE exempted
    inv_primary = asyncio.run(
        storage_refs.enumerate_snapshot_refs(
            _make_autospec_async_db_conn(_query_handler),
            env={
                "STORAGE_PROVIDER": "s3",
                "STORAGE_ENDPOINT_URL": "http://minio:9000",
                "STORAGE_BUCKET_NAME": "dalton-storage",
                "DALTON_ASR_STORAGE_ENDPOINT_URL": "http://minio:9000",
                "DALTON_ASR_STORAGE_BUCKET_NAME": "dalton-storage",
            },
            include_main=False,
            include_aux=True,
            now=snap_now,
        )
    )
    assert len(inv_primary.references) == len(rows)
    assert inv_primary.expired_by_rule_objects == []

    # Case C: Secondary bucket (dalton-asr with prefix 'prod') -> ONLY the 2 verifiable >7d transcriptions/ keys exempt
    inv_sec = asyncio.run(
        storage_refs.enumerate_snapshot_refs(
            _make_autospec_async_db_conn(_query_handler),
            env={
                "DALTON_ASR_STORAGE_ENDPOINT_URL": "http://minio:9000",
                "DALTON_ASR_STORAGE_BUCKET_NAME": "dalton-asr",
                "DALTON_ASR_STORAGE_KEY_PREFIX": "prod",
            },
            include_main=False,
            include_aux=True,
            now=snap_now,
        )
    )
    ref_ids = {r.row_id for r in inv_sec.references}
    assert ref_ids == {
        "job-created-8d-uploaded-today",
        "job-created-10d-completed-today",
        "job-unverifiable-ts",
        "job-untrusted-upload-ts",
        "job-legacy-uploads-30d",
    }
    assert inv_sec.skipped_expired == {"dalton_transcription_jobs": 2}
    assert {e.row_id for e in inv_sec.expired_by_rule_objects} == {
        "job-expired-by-completed-at",
        "job-expired-by-upload-ts",
    }
    assert {e.key for e in inv_sec.expired_by_rule_objects} == {
        "prod/transcriptions/ws1/job-expired-by-completed-at/source/call.m4a",
        "prod/transcriptions/ws1/job-expired-by-upload-ts/source/call.m4a",
    }
    assert all(
        e.bucket == "dalton-asr"
        and e.rule_id == storage_bucket_rules.RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE
        for e in inv_sec.expired_by_rule_objects
    )

    # Case D: End-to-end run_consistent_backup + TarGzObjectSink + local_backup_set records
    # expired_by_rule_objects in manifests with ZERO alerts and ZERO missing_referenced_objects!
    asr_dir = tmp_path / "asr_jobs"
    for row in rows:
        jdir = asr_dir / row[0]
        jdir.mkdir(parents=True)
        (jdir / "state.json").write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")
        (jdir / "result.json").write_text(json.dumps({"text": "ok"}), encoding="utf-8")

    # Seed ONLY the 5 non-exempt objects in S3; omit the 2 expired-by-rule objects!
    s3_objs = {
        f"prod/{r.object_key}": audio for r in inv_sec.references
    }
    s3 = _make_autospec_s3_client(
        versioning_by_bucket={"dalton-asr": True},
        objects_by_bucket={"dalton-asr": s3_objs},
    )
    alert_spy = create_autospec(_alert_signature)
    tar_path = tmp_path / "minio-objects.tar.gz"
    res = asyncio.run(
        consistent_backup.run_consistent_backup(
            conn=_make_autospec_async_db_conn(_query_handler),
            pg_dump_path=tmp_path / "pg.dump",
            pg_dump_fn=_make_pg_dump_spy(),
            bucket_specs=[
                consistent_backup.BucketArchiveSpec(
                    bucket="dalton-asr",
                    roles=("asr", "chat_upload"),
                    key_prefix="prod",
                    s3_client=s3,
                    endpoint="http://minio:9000",
                )
            ],
            object_sink=consistent_backup.TarGzObjectSink(tar_path),
            asr_jobs_dir=asr_dir,
            asr_archive_path=tmp_path / "asr-jobs.tar.gz",
            snapshot_time=snap_now,
            alert_fn=alert_spy,
        )
    )
    assert res.reconcile_report.ok is True
    assert res.missing_referenced_objects == ()
    assert len(res.expired_by_rule_objects) == 2
    assert alert_spy.call_count == 0  # Expired-by-rule MUST NOT alert!

    with tarfile.open(tar_path, "r:gz") as tf:
        tar_mf = json.loads(tf.extractfile("_minio_manifest.json").read().decode("utf-8"))
    assert tar_mf["expired_by_rule_count"] == 2
    assert {e["row_id"] for e in tar_mf["expired_by_rule_objects"]} == {
        "job-expired-by-completed-at",
        "job-expired-by-upload-ts",
    }

    # Publish via local_backup_set -> also records expired_by_rule_objects in both manifest & set JSON
    backups_root = tmp_path / "backups"
    pool_dir = backups_root / "minio-mirror" / "objects"
    sha_path = pool_dir / audio_sha[:2] / audio_sha
    sha_path.parent.mkdir(parents=True, exist_ok=True)
    sha_path.write_bytes(audio)
    staged_dump = backups_root / ".tmp" / "pg.dump"
    staged_dump.parent.mkdir(parents=True, exist_ok=True)
    staged_dump.write_bytes(b"PGDMP")
    published = local_backup_set.publish_local_backup_set(
        backups_root=backups_root,
        backup_set_id="20261128-120000",
        tag="daily",
        snapshot_id=res.snapshot_id,
        staged_dump_path=staged_dump,
        manifest_objects=res.manifest_objects,
        asr_summary=res.asr_archive,
        reconcile_ok=True,
        missing_referenced_objects=res.missing_referenced_objects,
        expired_by_rule_objects=res.expired_by_rule_objects,
    )
    mf_doc = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    set_doc = json.loads(published.set_path.read_text(encoding="utf-8"))
    assert mf_doc["expired_by_rule_count"] == 2
    assert set_doc["expired_by_rule_count"] == 2
    assert len(mf_doc["expired_by_rule_objects"]) == 2


# ---------------------------------------------------------------------------
# 2. Physical primary bucket identified by (endpoint, bucket_name): zero tolerance on ALL prefixes
# ---------------------------------------------------------------------------


def test_r3_physical_primary_bucket_zero_tolerance_across_all_prefixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_blob = b"main-blob"
    inventory = storage_refs.SnapshotRefInventory(
        references=[
            storage_refs.ObjectReference(
                consumer="workspace_file_blobs",
                bucket_role="main",
                object_key=f"blobs/u1/{_sha(main_blob)}",
                row_id="u1:blob",
                expected_size=len(main_blob),
                expected_sha256=_sha(main_blob),
                check_mode="strict",
            ),
            storage_refs.ObjectReference(
                consumer="dalton_chat_file_uploads",
                bucket_role="chat_upload",
                object_key="chat-uploads/ws1/up-miss/missing.pdf",
                row_id="up-miss",
                expected_size=123,
                expected_sha256="a" * 64,
                check_mode="strict",
            ),
        ]
    )
    monkeypatch.setattr(
        consistent_backup,
        "enumerate_snapshot_refs",
        create_autospec(storage_refs.enumerate_snapshot_refs, return_value=inventory),
    )

    def _query_handler(sql: str, _params: Any) -> Any:
        s = " ".join(sql.split())
        if s.startswith("SELECT pg_export_snapshot"):
            return ("snap-primary-shared",)
        if s.startswith("SELECT pg_advisory_lock") or s.startswith("SELECT pg_advisory_unlock"):
            return (True, True)
        return []

    s3_shared = _make_autospec_s3_client(
        versioning_by_bucket={"dalton-storage": True},
        objects_by_bucket={
            "dalton-storage": {
                f"main/blobs/u1/{_sha(main_blob)}": main_blob,
                # aux/chat-uploads/ws1/up-miss/missing.pdf intentionally absent!
            }
        },
    )

    # R4 narrowed topology: main + aux sharing the same bucket name (same endpoint or different
    # endpoint) is now rejected at entry with UnsupportedStorageTopologyError.
    with pytest.raises(
        consistent_backup.UnsupportedStorageTopologyError,
        match="当前备份不支持该存储布局",
    ):
        asyncio.run(
            consistent_backup.run_consistent_backup(
                conn=_make_autospec_async_db_conn(_query_handler),
                pg_dump_path=tmp_path / "pg.dump",
                pg_dump_fn=_make_pg_dump_spy(),
                bucket_specs=[
                    consistent_backup.BucketArchiveSpec(
                        bucket="dalton-storage",
                        roles=("main",),
                        key_prefix="main",
                        s3_client=s3_shared,
                        endpoint="http://127.0.0.1:9000",
                    ),
                    consistent_backup.BucketArchiveSpec(
                        bucket="dalton-storage",
                        roles=("asr", "chat_upload"),
                        key_prefix="aux",
                        s3_client=s3_shared,
                        endpoint="http://127.0.0.1:9000/",
                    ),
                ],
                object_sink=consistent_backup.MirrorPoolSink(tmp_path / "pool-fail"),
                max_missing_referenced_objects=50,
            )
        )

    s3_aux_other_ep = _make_autospec_s3_client(
        versioning_by_bucket={"dalton-storage": True},
        objects_by_bucket={"dalton-storage": {}},
    )
    with pytest.raises(
        consistent_backup.UnsupportedStorageTopologyError,
        match="当前备份不支持该存储布局",
    ):
        asyncio.run(
            consistent_backup.run_consistent_backup(
                conn=_make_autospec_async_db_conn(_query_handler),
                pg_dump_path=tmp_path / "pg2.dump",
                pg_dump_fn=_make_pg_dump_spy(),
                bucket_specs=[
                    consistent_backup.BucketArchiveSpec(
                        bucket="dalton-storage",
                        roles=("main",),
                        key_prefix="main",
                        s3_client=s3_shared,
                        endpoint="http://127.0.0.1:9000",
                    ),
                    consistent_backup.BucketArchiveSpec(
                        bucket="dalton-storage",
                        roles=("asr", "chat_upload"),
                        key_prefix="aux",
                        s3_client=s3_aux_other_ep,
                        endpoint="http://127.0.0.1:9001",
                    ),
                ],
                object_sink=consistent_backup.MirrorPoolSink(tmp_path / "pool-ok"),
                max_missing_referenced_objects=50,
            )
        )

    # Supported topology (single endpoint + distinct main/aux bucket names): aux missing is tolerated
    # while main remains zero-tolerance.
    s3_distinct = _make_autospec_s3_client(
        versioning_by_bucket={"dalton-storage": True, "dalton-asr": True},
        objects_by_bucket={
            "dalton-storage": {f"main/blobs/u1/{_sha(main_blob)}": main_blob},
            "dalton-asr": {},
        },
    )
    res_supported = asyncio.run(
        consistent_backup.run_consistent_backup(
            conn=_make_autospec_async_db_conn(_query_handler),
            pg_dump_path=tmp_path / "pg3.dump",
            pg_dump_fn=_make_pg_dump_spy(),
            bucket_specs=[
                consistent_backup.BucketArchiveSpec(
                    bucket="dalton-storage",
                    roles=("main",),
                    key_prefix="main",
                    s3_client=s3_distinct,
                    endpoint="http://127.0.0.1:9000",
                ),
                consistent_backup.BucketArchiveSpec(
                    bucket="dalton-asr",
                    roles=("asr", "chat_upload"),
                    key_prefix="aux",
                    s3_client=s3_distinct,
                    endpoint="http://127.0.0.1:9000/",
                ),
            ],
            object_sink=consistent_backup.MirrorPoolSink(tmp_path / "pool-supported"),
            max_missing_referenced_objects=50,
        )
    )
    assert res_supported.reconcile_report.ok is True
    assert len(res_supported.missing_referenced_objects) == 1


# ---------------------------------------------------------------------------
# 3. verify-restored-refs queries target bucket & enforces same-backup-set known_missing
# ---------------------------------------------------------------------------


def test_r3_verify_restored_refs_queries_target_bucket_and_enforces_same_backup_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    present_bytes = b"restored-chat-file"
    known_missing_key = "chat-uploads/ws-old/up-miss/gone.xlsx"

    backups_root = tmp_path / "backups"
    pool_dir = backups_root / "minio-mirror" / "objects"
    p = pool_dir / _sha(present_bytes)[:2] / _sha(present_bytes)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(present_bytes)

    staged_dump = backups_root / ".tmp" / "pg.dump"
    staged_dump.parent.mkdir(parents=True, exist_ok=True)
    staged_dump.write_bytes(b"PGDMP")

    published = local_backup_set.publish_local_backup_set(
        backups_root=backups_root,
        backup_set_id="20261128-050000",
        tag="daily",
        snapshot_id="snap-vrr-1",
        staged_dump_path=staged_dump,
        manifest_objects=[
            consistent_backup.ArchivedObjectRecord(
                bucket="dalton-asr",
                key="chat-uploads/ws1/up1/ok.xlsx",
                version_id=None,
                size=len(present_bytes),
                archived_sha256=_sha(present_bytes),
            )
        ],
        asr_summary=None,
        reconcile_ok=True,
        missing_referenced_objects=[
            consistent_backup.MissingReferencedObject(
                bucket="dalton-asr",
                key=known_missing_key,
                consumer="dalton_chat_file_uploads",
            )
        ],
    )

    inv = storage_refs.SnapshotRefInventory(
        references=[
            storage_refs.ObjectReference(
                consumer="dalton_chat_file_uploads",
                bucket_role="chat_upload",
                object_key="chat-uploads/ws1/up1/ok.xlsx",
                row_id="up1",
                expected_size=len(present_bytes),
                expected_sha256=_sha(present_bytes),
                check_mode="strict",
            ),
            storage_refs.ObjectReference(
                consumer="dalton_chat_file_uploads",
                bucket_role="chat_upload",
                object_key=known_missing_key,
                row_id="up-miss",
                expected_size=99,
                expected_sha256="b" * 64,
                check_mode="strict",
            ),
        ]
    )
    refs_path = tmp_path / "refs.json"
    refs_path.write_text(json.dumps(inv.to_dict()), encoding="utf-8")

    # 3a. Target bucket is MISSING the object that IS in the manifest -> MUST FAIL!
    orig_builder = storage_refs._build_target_s3_clients_for_reconcile
    empty_target_s3 = _make_autospec_s3_client(
        versioning_by_bucket={"dalton-asr": False},
        objects_by_bucket={"dalton-asr": {}},
    )
    monkeypatch.setattr(
        storage_refs,
        "_build_target_s3_clients_for_reconcile",
        create_autospec(
            orig_builder,
            return_value={"dalton-asr": empty_target_s3},
        ),
    )
    rc_missing_target = backup_lib.main(
        [
            "verify-restored-refs",
            "--backup-set",
            str(published.set_path),
            "--refs-json",
            str(refs_path),
            "--aux-bucket",
            "dalton-asr",
        ]
    )
    assert rc_missing_target == 1
    out_missing = json.loads(capsys.readouterr().out)
    assert out_missing["ok"] is False
    assert any(e["code"] == "missing_in_target_bucket" for e in out_missing["errors"])

    # 3b. Target bucket HAS the manifest object -> PASSES with known_missing_count=1!
    populated_target_s3 = _make_autospec_s3_client(
        versioning_by_bucket={"dalton-asr": False},
        objects_by_bucket={"dalton-asr": {"chat-uploads/ws1/up1/ok.xlsx": present_bytes}},
    )
    monkeypatch.setattr(
        storage_refs,
        "_build_target_s3_clients_for_reconcile",
        create_autospec(
            orig_builder,
            return_value={"dalton-asr": populated_target_s3},
        ),
    )
    rc_ok = backup_lib.main(
        [
            "verify-restored-refs",
            "--backup-set",
            str(published.set_path),
            "--refs-json",
            str(refs_path),
            "--aux-bucket",
            "dalton-asr",
        ]
    )
    assert rc_ok == 0
    out_ok = json.loads(capsys.readouterr().out)
    assert out_ok["ok"] is True
    assert out_ok["known_missing_count"] == 1

    # 3c. Foreign backup-set JSON from a DIFFERENT backup set -> MUST FAIL (exit 2)!
    foreign_set = json.loads(published.set_path.read_text(encoding="utf-8"))
    foreign_set["backup_set_id"] = "20261120-000000"
    foreign_set_path = tmp_path / "foreign-backup-set.json"
    foreign_set_path.write_text(json.dumps(foreign_set), encoding="utf-8")
    rc_foreign = backup_lib.main(
        [
            "verify-restored-refs",
            "--manifest",
            str(published.manifest_path),
            "--backup-set",
            str(foreign_set_path),
            "--refs-json",
            str(refs_path),
            "--aux-bucket",
            "dalton-asr",
        ]
    )
    assert rc_foreign == 2
    capsys.readouterr()

    # 3d. Target bucket query raises 403 -> MUST FAIL (target_bucket_query_error)
    err_403_s3 = _make_autospec_s3_client(
        versioning_by_bucket={"dalton-asr": False},
        list_v2_error_by_bucket={
            "dalton-asr": ClientError(
                {
                    "Error": {"Code": "AccessDenied", "Message": "Forbidden"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                },
                "ListObjectsV2",
            )
        },
    )
    monkeypatch.setattr(
        storage_refs,
        "_build_target_s3_clients_for_reconcile",
        create_autospec(
            orig_builder,
            return_value={"dalton-asr": err_403_s3},
        ),
    )
    rc_err = backup_lib.main(
        [
            "verify-restored-refs",
            "--backup-set",
            str(published.set_path),
            "--refs-json",
            str(refs_path),
            "--aux-bucket",
            "dalton-asr",
        ]
    )
    assert rc_err == 1
    out_err = json.loads(capsys.readouterr().out)
    assert out_err["ok"] is False
    assert any(e["code"] == "target_bucket_query_error" for e in out_err["errors"])

    # 3e. Object present in manifest['objects'] is NEVER excused as known_missing even if also listed in missing_referenced_objects
    tampered_mf = json.loads(published.manifest_path.read_text(encoding="utf-8"))
    tampered_mf["missing_referenced_objects"].append(
        {
            "bucket": "dalton-asr",
            "key": "chat-uploads/ws1/up1/ok.xlsx",
            "consumer": "dalton_chat_file_uploads",
        }
    )
    tampered_mf_path = tmp_path / "tampered-manifest.json"
    tampered_mf_path.write_text(json.dumps(tampered_mf), encoding="utf-8")
    monkeypatch.setattr(
        storage_refs,
        "_build_target_s3_clients_for_reconcile",
        create_autospec(
            orig_builder,
            return_value={"dalton-asr": empty_target_s3},
        ),
    )
    rc_tampered = backup_lib.main(
        [
            "verify-restored-refs",
            "--manifest",
            str(tampered_mf_path),
            "--refs-json",
            str(refs_path),
            "--aux-bucket",
            "dalton-asr",
        ]
    )
    assert rc_tampered == 1
    out_tampered = json.loads(capsys.readouterr().out)
    assert out_tampered["ok"] is False
    assert any(
        e["code"] == "missing_in_target_bucket" and "up1/ok.xlsx" in e["object_key"]
        for e in out_tampered["errors"]
    )


# ---------------------------------------------------------------------------
# 4. Strict source audio check for active / uploaded / submitted awaiting_upload / indeterminate ASR
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "usage"),
    [
        ("queued", {"upload": {"completed": True}}),
        ("transcribing", {"upload": {"completed": True}}),
        ("indeterminate", {"upload": {"completed": True}}),
        ("awaiting_upload", {"upload": {"completed": True}}),
        ("awaiting_upload", {"submit_attempted": True}),
    ],
)
def test_r3_active_uploaded_submitted_and_indeterminate_asr_audio_fails_directly_without_tolerance(
    tmp_path: Path,
    status: str,
    usage: dict[str, Any],
) -> None:
    now = dt.datetime(2026, 11, 28, 12, 0, 0, tzinfo=dt.timezone.utc)
    missing_key = f"transcriptions/ws1/job-{status}/source/required.m4a"

    def _query_handler(sql: str, params: Any) -> Any:
        s = " ".join(sql.split())
        if s.startswith("SELECT pg_export_snapshot"):
            return ("snap-strict-asr",)
        if s.startswith("SELECT pg_advisory_lock") or s.startswith("SELECT pg_advisory_unlock"):
            return (True, True)
        if s.startswith("SELECT to_regclass"):
            tbl = params[0] if params else ""
            return (tbl,) if tbl == "public.dalton_transcription_jobs" else (None,)
        if "FROM dalton_transcription_jobs" in s:
            return [
                (
                    f"job-{status}",
                    status,
                    missing_key,
                    4096,
                    "c" * 64,
                    usage,
                    now - dt.timedelta(days=15),
                    None,
                    now - dt.timedelta(hours=1),
                )
            ]
        return []

    s3 = _make_autospec_s3_client(
        versioning_by_bucket={"dalton-asr": True},
        objects_by_bucket={"dalton-asr": {}},
    )
    with pytest.raises(consistent_backup.MissingReferencedObjectsError) as exc_info:
        asyncio.run(
            consistent_backup.run_consistent_backup(
                conn=_make_autospec_async_db_conn(_query_handler),
                pg_dump_path=tmp_path / "pg.dump",
                pg_dump_fn=_make_pg_dump_spy(),
                bucket_specs=[
                    consistent_backup.BucketArchiveSpec(
                        bucket="dalton-asr",
                        roles=("asr", "chat_upload"),
                        s3_client=s3,
                        endpoint="http://minio:9000",
                    )
                ],
                object_sink=consistent_backup.MirrorPoolSink(tmp_path / "pool"),
                max_missing_referenced_objects=50,
                snapshot_time=now,
            )
        )
    assert exc_info.value.bucket == "dalton-asr"
    assert missing_key in exc_info.value.missing_keys
    assert exc_info.value.threshold is None


def test_r3_unuploaded_awaiting_upload_is_not_enumerated_and_does_not_fail(
    tmp_path: Path,
) -> None:
    now = dt.datetime(2026, 11, 28, 12, 0, 0, tzinfo=dt.timezone.utc)

    def _query_handler(sql: str, params: Any) -> Any:
        s = " ".join(sql.split())
        if s.startswith("SELECT pg_export_snapshot"):
            return ("snap-unuploaded",)
        if s.startswith("SELECT pg_advisory_lock") or s.startswith("SELECT pg_advisory_unlock"):
            return (True, True)
        if s.startswith("SELECT to_regclass"):
            tbl = params[0] if params else ""
            return (tbl,) if tbl == "public.dalton_transcription_jobs" else (None,)
        if "FROM dalton_transcription_jobs" in s:
            return [
                (
                    "job-not-uploaded-yet",
                    "awaiting_upload",
                    "transcriptions/ws1/job-not-uploaded-yet/source/pending.m4a",
                    4096,
                    "d" * 64,
                    {"upload": {"completed": False}},
                    now - dt.timedelta(hours=1),
                    None,
                    now - dt.timedelta(hours=1),
                )
            ]
        return []

    s3 = _make_autospec_s3_client(
        versioning_by_bucket={"dalton-asr": True},
        objects_by_bucket={"dalton-asr": {}},
    )
    res = asyncio.run(
        consistent_backup.run_consistent_backup(
            conn=_make_autospec_async_db_conn(_query_handler),
            pg_dump_path=tmp_path / "pg.dump",
            pg_dump_fn=_make_pg_dump_spy(),
            bucket_specs=[
                consistent_backup.BucketArchiveSpec(
                    bucket="dalton-asr",
                    roles=("asr", "chat_upload"),
                    s3_client=s3,
                    endpoint="http://minio:9000",
                )
            ],
            object_sink=consistent_backup.MirrorPoolSink(tmp_path / "pool"),
            max_missing_referenced_objects=0,
            snapshot_time=now,
        )
    )
    assert res.reconcile_report.ok is True
    assert res.inventory.references == []
    assert res.inventory.skipped_pending == {"dalton_transcription_jobs": 1}


# ---------------------------------------------------------------------------
# 5. Standalone restore.sh error classification: only 404/NoSuchKey matches known missing
# ---------------------------------------------------------------------------


def _extract_restore_missing_check_py() -> str:
    text = (REPO_ROOT / "scripts/standalone/restore.sh").read_text(encoding="utf-8")
    m = re.search(r"MISSING_CHECK_PY='\n(.*?)\n'\n", text, flags=re.DOTALL)
    assert m is not None, "Could not extract MISSING_CHECK_PY from scripts/standalone/restore.sh"
    return m.group(1).replace(r"\x27", "'")


@pytest.mark.parametrize(
    ("error_code", "http_status", "expect_rc"),
    [
        ("404", 404, 0),
        ("NoSuchKey", 404, 0),
        ("AccessDenied", 403, 1),
        ("InternalError", 500, 1),
        ("ServiceUnavailable", 503, 1),
    ],
)
def test_r3_standalone_restore_error_classification_only_404_matches_known_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_code: str,
    http_status: int,
    expect_rc: int,
) -> None:
    snippet = _extract_restore_missing_check_py()
    s3_spec = boto3.session.Session().client(
        "s3",
        endpoint_url="http://minio:9000",
        aws_access_key_id="ak",
        aws_secret_access_key="sk",
        region_name="us-east-1",
    )
    fake_s3 = create_autospec(s3_spec)
    fake_s3.head_object.side_effect = ClientError(
        {
            "Error": {"Code": error_code, "Message": f"simulated {error_code}"},
            "ResponseMetadata": {"HTTPStatusCode": http_status},
        },
        "HeadObject",
    )
    monkeypatch.setattr(
        boto3,
        "client",
        create_autospec(boto3.session.Session().client, return_value=fake_s3),
    )
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME", "chat-uploads")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY", "sk")
    monkeypatch.delenv("DALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX", raising=False)

    known_json = json.dumps(
        [{"bucket": "chat-uploads", "key": "chat-uploads/ws1/up1/a.pdf", "consumer": "dalton_chat_file_uploads"}]
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("chat_upload\tup1\tchat-uploads/ws1/up1/a.pdf\n"))
    monkeypatch.setattr(sys, "argv", ["restore-check", "0", known_json])

    rc = 0
    try:
        exec(compile(snippet, "restore.sh:MISSING_CHECK_PY", "exec"), {"__name__": "__main__"})
    except SystemExit as exc:
        rc = int(exc.code) if isinstance(exc.code, int) else 1

    captured = capsys.readouterr()
    assert rc == expect_rc, f"code={error_code} status={http_status} stderr={captured.err}"
    if expect_rc == 1:
        assert "non-404 error" in captured.err
        assert error_code in captured.err


# ---------------------------------------------------------------------------
# 6. Unified transcription prefix across configure_minio.sh & minio_init.py + strict readback
# ---------------------------------------------------------------------------


def test_r3_configure_minio_uses_asr_prefix_for_secondary_and_readback_rejects_extra_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 6a. configure_minio.sh wires DALTON_ASR_STORAGE_KEY_PREFIX for role=secondary
    cfg_text = (REPO_ROOT / "scripts/dalton/configure_minio.sh").read_text(encoding="utf-8")
    assert 'if [[ "$ROLE" == "secondary" ]]; then' in cfg_text
    assert 'TARGET_KEY_PREFIX="$DALTON_ASR_STORAGE_KEY_PREFIX"' in cfg_text
    assert '--prefix "$TARGET_KEY_PREFIX"' in cfg_text

    # 6b. storage_bucket_rules CLI defaults --prefix by role when omitted
    s3_spec = boto3.session.Session().client(
        "s3",
        endpoint_url="http://minio:9000",
        aws_access_key_id="ak",
        aws_secret_access_key="sk",
        region_name="us-east-1",
    )
    fake_s3 = create_autospec(s3_spec)
    state: dict[str, Any] = {"rules": []}

    def _get_lc(*, Bucket: str, **_kw: Any) -> dict[str, Any]:
        return {"Rules": [dict(r) for r in state["rules"]]}

    def _put_lc(*, Bucket: str, LifecycleConfiguration: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        state["rules"] = [dict(r) for r in LifecycleConfiguration.get("Rules", [])]
        return {}

    fake_s3.head_bucket.return_value = {}
    fake_s3.delete_bucket_policy.return_value = {}
    fake_s3.get_bucket_policy.side_effect = ClientError(
        {"Error": {"Code": "NoSuchBucketPolicy", "Message": "none"}}, "GetBucketPolicy"
    )
    fake_s3.get_bucket_versioning.return_value = {"Status": "Enabled"}
    fake_s3.get_bucket_lifecycle_configuration.side_effect = _get_lc
    fake_s3.put_bucket_lifecycle_configuration.side_effect = _put_lc

    monkeypatch.setenv("STORAGE_KEY_PREFIX", "main-only")
    monkeypatch.setenv("DALTON_ASR_STORAGE_KEY_PREFIX", "asr-only")
    monkeypatch.setattr(
        storage_bucket_rules,
        "_build_s3_client_from_env",
        create_autospec(storage_bucket_rules._build_s3_client_from_env, return_value=fake_s3),
    )
    rc = storage_bucket_rules.main(["apply", "--bucket", "dalton-asr", "--role", "secondary"])
    assert rc == 0
    by_id = {r["ID"]: r for r in state["rules"]}
    assert by_id["dalton-transcription-audio-expire"]["Filter"]["Prefix"] == "asr-only/transcriptions/"

    # 6c. Read-back verification rejects same-ID rule carrying extra tag filter or extra actions
    tagged_rules = storage_bucket_rules.build_lifecycle_rules(role="secondary", key_prefix="asr-only")
    tagged_rules[0]["Filter"] = {
        "And": {
            "Prefix": "asr-only/transcriptions/",
            "Tags": [{"Key": "env", "Value": "cold"}],
        }
    }
    with pytest.raises(storage_bucket_rules.BucketRuleVerificationError, match="extra filter"):
        storage_bucket_rules.verify_lifecycle_rules(
            tagged_rules, role="secondary", key_prefix="asr-only"
        )

    extra_action_rules = storage_bucket_rules.build_lifecycle_rules(
        role="secondary", key_prefix="asr-only"
    )
    extra_action_rules[0]["NoncurrentVersionExpiration"] = {"NoncurrentDays": 60}
    with pytest.raises(storage_bucket_rules.BucketRuleVerificationError, match="extra action"):
        storage_bucket_rules.verify_lifecycle_rules(
            extra_action_rules, role="secondary", key_prefix="asr-only"
        )

    # Also reject extra Tag filter on main bucket rule
    main_tagged = storage_bucket_rules.build_lifecycle_rules(role="main", key_prefix="main-only")
    main_tagged[0]["Filter"] = {
        "And": {
            "Prefix": "main-only/uploads/",
            "Tags": [{"Key": "scope", "Value": "temp"}],
        }
    }
    with pytest.raises(storage_bucket_rules.BucketRuleVerificationError, match="extra filter"):
        storage_bucket_rules.verify_lifecycle_rules(
            main_tagged, role="main", key_prefix="main-only"
        )


# ---------------------------------------------------------------------------
# 7. Standalone Core alert persistence on host & concise summary (top 10 keys only)
# ---------------------------------------------------------------------------


def test_r3_standalone_compose_backup_summary_caps_at_10_and_host_backup_persists_alert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 7a. _cmd_compose_backup emits ONLY count + first 10 sample keys (no full list) and
    # does NOT write an ephemeral alert file inside container by default.
    refs = [
        storage_refs.ObjectReference(
            consumer="dalton_chat_file_uploads",
            bucket_role="chat_upload",
            object_key=f"chat-uploads/ws1/up-{i:02d}/file-{i:02d}.pdf",
            row_id=f"up-{i:02d}",
            expected_size=100,
            expected_sha256="e" * 64,
            check_mode="strict",
        )
        for i in range(15)
    ]
    inventory = storage_refs.SnapshotRefInventory(references=refs)
    monkeypatch.setattr(
        consistent_backup,
        "enumerate_snapshot_refs",
        create_autospec(storage_refs.enumerate_snapshot_refs, return_value=inventory),
    )

    def _query_handler(sql: str, _params: Any) -> Any:
        s = " ".join(sql.split())
        if s.startswith("SELECT pg_export_snapshot"):
            return ("snap-core-15",)
        if s.startswith("SELECT pg_advisory_lock") or s.startswith("SELECT pg_advisory_unlock"):
            return (True, True)
        return []

    s3 = _make_autospec_s3_client(
        versioning_by_bucket={"chat-uploads": True},
        objects_by_bucket={"chat-uploads": {}},
    )
    ephemeral_alert = tmp_path / "ephemeral-container-alerts.log"
    monkeypatch.setenv("DALTON_BACKUP_ALERTS_FILE", str(ephemeral_alert))
    monkeypatch.setenv("DALTON_BACKUP_PG_DSN", "postgresql://u:p@localhost:5432/db")

    ctl_in = io.StringIO("SNAPSHOT_IMPORTED\nPG_DUMP_DONE\n")
    ctl_err = io.StringIO()
    bundle_buf = io.BytesIO()
    fake_stdout = type("FakeStdout", (), {"buffer": bundle_buf})()
    monkeypatch.setattr(sys, "stdin", ctl_in)
    monkeypatch.setattr(sys, "stderr", ctl_err)
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    async def _open_conn(_dsn: str) -> Any:
        return _make_autospec_async_db_conn(_query_handler)

    args = standalone_cb.build_arg_parser().parse_args(["compose-backup", "--stream-bundle"])
    rc = standalone_cb._cmd_compose_backup(
        args,
        conn_opener=_open_conn,
        bucket_resolver=lambda _env: [
            consistent_backup.BucketArchiveSpec(
                bucket="chat-uploads",
                roles=("asr", "chat_upload"),
                s3_client=s3,
            )
        ],
    )
    assert rc == 0
    # Container did NOT write ephemeral alert file
    assert not ephemeral_alert.exists()

    err_text = ctl_err.getvalue()
    summary_line = next(
        line for line in err_text.splitlines() if line.startswith("@@DALTON_CTL@@ BACKUP_SUMMARY ")
    )
    summary_doc = json.loads(summary_line.split("@@DALTON_CTL@@ BACKUP_SUMMARY ", 1)[1])
    assert summary_doc["missing_referenced_count"] == 15
    assert "missing_referenced_objects" not in summary_doc
    assert len(summary_doc["missing_referenced_sample"]) == 10
    # Keys 10..14 MUST NOT appear anywhere in compose-backup stderr output!
    for i in range(10, 15):
        assert f"file-{i:02d}.pdf" not in err_text

    # Full 15-item list IS in the streamed tar's _minio_manifest.json
    bundle_buf.seek(0)
    with tarfile.open(fileobj=bundle_buf, mode="r:") as btf:
        minio_tar_bytes = btf.extractfile("minio-objects.tar.gz").read()
    with tarfile.open(fileobj=io.BytesIO(minio_tar_bytes), mode="r:gz") as mtf:
        tar_mf = json.loads(mtf.extractfile("_minio_manifest.json").read().decode("utf-8"))
    assert tar_mf["missing_referenced_count"] == 15
    assert len(tar_mf["missing_referenced_objects"]) == 15

    # 7b. Host scripts/standalone/backup.sh writes ONE persistent alert to host OUTPUT_DIR/backup-alerts.log,
    # prints ONLY count + top 10 keys in summary, and writes full list + expired_by_rule into manifest.json!
    work_dir = tmp_path / "work"
    (work_dir / "scripts" / "standalone").mkdir(parents=True)
    for rel in (
        "scripts/standalone/backup.sh",
        "scripts/standalone/storage_config.py",
        "scripts/standalone/consistent_backup.py",
    ):
        src_p = REPO_ROOT / rel
        dst_p = work_dir / rel
        dst_p.write_text(src_p.read_text(encoding="utf-8"), encoding="utf-8")
        if rel.endswith(".sh"):
            dst_p.chmod(0o755)
    (work_dir / "src" / "dalton").mkdir(parents=True)
    for rel in ("src/dalton/__init__.py", "src/dalton/pg_config.py"):
        src_p = REPO_ROOT / rel
        dst_p = work_dir / rel
        if src_p.exists():
            dst_p.write_text(src_p.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            dst_p.write_text("", encoding="utf-8")
    (work_dir / "src" / "__init__.py").write_text("", encoding="utf-8")
    (work_dir / ".env").write_text(
        "COMPOSE_PROJECT_NAME=dalton-test\n"
        "DB_HOST=postgres\n"
        "DB_PORT=5432\n"
        "DB_USER=dalton\n"
        "DB_PASSWORD=secret\n"
        "DB_NAME=dalton\n",
        encoding="utf-8",
    )
    (work_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    # Fake docker executable streaming our 15-missing tar + expired_by_rule
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    missing_15 = [
        {
            "bucket": "chat-uploads",
            "key": f"chat-uploads/ws1/up-{i:02d}/file-{i:02d}.pdf",
            "consumer": "dalton_chat_file_uploads",
        }
        for i in range(15)
    ]
    expired_1 = [
        {
            "bucket": "chat-uploads",
            "key": "transcriptions/ws1/job-old/source/old.m4a",
            "consumer": "dalton_transcription_jobs",
            "rule_id": "dalton-transcription-audio-expire",
            "row_id": "job-old",
        }
    ]
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"""#!{sys.executable}
import io, json, sys, tarfile

argv = sys.argv[1:]
joined = " ".join(argv)
if "compose" in argv and "version" in argv:
    print("Docker Compose version v2.29.0")
    sys.exit(0)
if "compose" in argv and "config" in argv and "--services" in argv:
    print("postgres\\nminio\\nedge\\nbackend")
    sys.exit(0)
if "compose" in argv and "config" in argv and "--format" in argv:
    print(json.dumps({{"services": {{"backend": {{"environment": {{}}}}, "postgres": {{"environment": {{}}}}}}, "volumes": {{}}}}))
    sys.exit(0)
if "compose" in argv and "ps" in argv:
    print("postgres\\nminio\\nedge\\nbackend")
    sys.exit(0)
if "compose-backup" in argv:
    sys.stderr.write("@@DALTON_CTL@@ SNAPSHOT_READY snap-host-15\\n")
    sys.stderr.flush()
    assert sys.stdin.readline().strip() == "SNAPSHOT_IMPORTED"
    assert sys.stdin.readline().strip() == "PG_DUMP_DONE"
    mf = {{
        "version": 2,
        "buckets": ["chat-uploads"],
        "bucket_count": 1,
        "object_count": 0,
        "total_bytes": 0,
        "objects": [],
        "missing_referenced_objects": {json.dumps(missing_15)},
        "missing_referenced_count": 15,
        "expired_by_rule_objects": {json.dumps(expired_1)},
        "expired_by_rule_count": 1,
    }}
    mbuf = io.BytesIO()
    with tarfile.open(fileobj=mbuf, mode="w:gz") as tf:
        raw = json.dumps(mf).encode("utf-8")
        ti = tarfile.TarInfo(name="_minio_manifest.json")
        ti.size = len(raw)
        tf.addfile(ti, io.BytesIO(raw))
    mbytes = mbuf.getvalue()
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as btf:
        bti = tarfile.TarInfo(name="minio-objects.tar.gz")
        bti.size = len(mbytes)
        btf.addfile(bti, io.BytesIO(mbytes))
    sys.stdout.buffer.flush()
    # Even if a caller emitted all 15 in BACKUP_SUMMARY, backup.sh must cap printed output at 10!
    sys.stderr.write(
        "@@DALTON_CTL@@ BACKUP_SUMMARY "
        + json.dumps({{
            "snapshot_id": "snap-host-15",
            "object_count": 0,
            "missing_referenced_count": 15,
            "missing_referenced_sample": [f"{{m['bucket']}}/{{m['key']}}" for m in {json.dumps(missing_15[:10])}],
            "expired_by_rule_count": 1,
            "reconcile_ok": True,
            "checked_refs": 15,
        }})
        + "\\n"
    )
    sys.stderr.flush()
    sys.exit(0)
if "pg_dump" in joined:
    sys.stdout.buffer.write(b"PGDMP-SYNTHETIC-CONTENT")
    sys.stdout.buffer.flush()
    sys.exit(0)
if "pg_restore" in joined and "--list" in joined:
    print("1; 0 0 TABLE public alembic_version_dalton")
    sys.exit(0)
if "psql" in joined:
    print("d022")
    sys.exit(0)
if "tar -czf - -C /data" in joined:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        ti = tarfile.TarInfo(name="caddy.txt")
        ti.size = 2
        tf.addfile(ti, io.BytesIO(b"ok"))
    sys.stdout.buffer.write(buf.getvalue())
    sys.stdout.buffer.flush()
    sys.exit(0)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    out_dir = tmp_path / "host-backups"
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env.pop("DALTON_BACKUP_ALERTS_FILE", None)
    proc = subprocess.run(
        [
            "bash",
            str(work_dir / "scripts/standalone/backup.sh"),
            "--work-dir",
            str(work_dir),
            "--output-dir",
            str(out_dir),
            "--compose-files",
            "docker-compose.yml",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"

    # Host persistent alert file was created in OUTPUT_DIR/backup-alerts.log with ONE alert line
    host_alert_file = out_dir / "backup-alerts.log"
    assert host_alert_file.is_file()
    alert_lines = host_alert_file.read_text(encoding="utf-8").splitlines()
    assert len(alert_lines) == 1
    assert "[MISSING_REFERENCED_OBJECTS]" in alert_lines[0]
    assert "Tolerated 15 missing" in alert_lines[0]
    assert "(+5 more)" in alert_lines[0]
    assert "file-00.pdf" in alert_lines[0]
    assert "file-09.pdf" in alert_lines[0]
    for i in range(10, 15):
        assert f"file-{i:02d}.pdf" not in alert_lines[0]

    # Combined stdout + stderr NEVER prints keys 10..14 (only count + top 10 keys!)
    combined_logs = proc.stdout + "\n" + proc.stderr
    assert "file-00.pdf" in combined_logs
    assert "file-09.pdf" in combined_logs
    for i in range(10, 15):
        assert f"file-{i:02d}.pdf" not in combined_logs

    # Published manifest.json retains the full 15 missing objects AND expired_by_rule_objects
    backup_dirs = [d for d in out_dir.iterdir() if d.is_dir() and d.name.startswith("dalton-backup-")]
    assert len(backup_dirs) == 1
    manifest_doc = json.loads((backup_dirs[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_doc["missing_referenced_count"] == 15
    assert len(manifest_doc["missing_referenced_objects"]) == 15
    assert manifest_doc["expired_by_rule_count"] == 1
    assert manifest_doc["expired_by_rule_objects"][0]["row_id"] == "job-old"
