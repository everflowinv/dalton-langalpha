"""Unit tests for tolerated missing referenced objects in consistent backup & restore verification.

Covers:
1. Small count of missing referenced objects on non-main bucket (both unversioned & versioned):
   - Backup succeeds (DB dump + present objects archived & reconciled).
   - ``missing_referenced_objects: [{bucket, key, consumer}]`` recorded in Everflow
     ``backup-set-*.json``, ``latest-verified-set.json``, ``minio-manifest-*.json``, and
     Standalone Core ``_minio_manifest.json``.
   - Logger warning and ops alert exit (``backup_lib.append_alert`` via
     ``local_backup_set.emit_missing_referenced_alert``) each report ONCE with total count
     and first 10 keys.
2. Exceeding threshold (default 50, configurable via ``DALTON_BACKUP_MAX_MISSING_REFS``) -> fails.
3. Missing referenced object on main bucket (``dalton-main`` or ``role='main'``) -> always fails.
4. Restore verification (``backup_lib.verify_restored_references``, ``backup_lib verify-restored-refs``,
   ``storage_refs.reconcile_inventory_against_manifest``, and ``consistent_backup.restore_objects_from_manifest``):
   - Known missing objects registered in manifest are reported and NOT treated as restore failures.
   - Unregistered missing objects still fail verification.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import json
import logging
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

from scripts.dalton.ops import backup_lib
from scripts.dalton.ops.prod import local_backup_set
from scripts.standalone import consistent_backup as standalone_cb
from src.dalton import consistent_backup, storage_refs


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _S3ClientContract:
    """Typed S3 client interface used with ``create_autospec`` in tests."""

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
) -> Any:
    """Build a ``create_autospec`` S3 client backed by an in-memory object map."""
    store: dict[str, dict[str, tuple[bytes, str]]] = {
        b: {k: (v, "application/octet-stream") for k, v in objs.items()}
        for b, objs in (objects_by_bucket or {}).items()
    }
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


class _SnapshotCursorContract:
    async def __aenter__(self) -> _SnapshotCursorContract: ...
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...
    async def execute(self, sql: str, params: Any = None) -> None: ...
    async def fetchone(self) -> Any: ...
    async def fetchall(self) -> list[Any]: ...


class _SnapshotConnContract:
    def cursor(self) -> _SnapshotCursorContract: ...
    async def close(self) -> None: ...


def _make_autospec_pg_conn(snapshot_id: str = "00000001-00000001-1") -> Any:
    """Build a ``create_autospec`` async PostgreSQL control connection for lock + snapshot SQL."""
    last_sql: list[str] = [""]
    cur = create_autospec(_SnapshotCursorContract, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None

    async def _exec(sql: str, params: Any = None) -> None:
        last_sql[0] = sql.strip()

    async def _fetchone() -> Any:
        sql = last_sql[0]
        if "pg_export_snapshot" in sql:
            return (snapshot_id,)
        if "pg_advisory_unlock" in sql:
            return (True, True)
        if "pg_advisory_lock" in sql:
            return (None, None)
        return (None,)

    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = _fetchone
    cur.fetchall.return_value = []

    conn = create_autospec(_SnapshotConnContract, instance=True)
    conn.cursor.return_value = cur
    return conn


def _pg_dump_signature(snapshot_id: str, out_path: Path) -> None: ...


def _alert_signature(code: str, message: str) -> Any: ...


def _log_signature(msg: str) -> None: ...


def _make_pg_dump_spy() -> Any:
    spy = create_autospec(_pg_dump_signature)

    def _write_dump(snapshot_id: str, out_path: Path) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(f"PGDMP:{snapshot_id}".encode("utf-8"))

    spy.side_effect = _write_dump
    return spy


def _build_prod_like_missing_inventory(
    *,
    present_chat_bytes: bytes,
    present_asr_bytes: bytes,
    missing_chat_count: int = 4,
    missing_asr_count: int = 7,
) -> storage_refs.SnapshotRefInventory:
    refs: list[storage_refs.ObjectReference] = [
        storage_refs.ObjectReference(
            consumer="dalton_chat_file_uploads",
            bucket_role="chat_upload",
            object_key="chat-uploads/ws-ok/up-ok/present.xlsx",
            row_id="up-ok",
            expected_size=len(present_chat_bytes),
            expected_sha256=_sha(present_chat_bytes),
            check_mode="strict",
        ),
        storage_refs.ObjectReference(
            consumer="dalton_transcription_jobs",
            bucket_role="asr",
            object_key="transcriptions/ws-ok/job-ok/source/present.mp3",
            row_id="job-ok",
            expected_size=len(present_asr_bytes),
            expected_sha256=_sha(present_asr_bytes),
            check_mode="strict",
        ),
    ]
    for i in range(missing_chat_count):
        refs.append(
            storage_refs.ObjectReference(
                consumer="dalton_chat_file_uploads",
                bucket_role="chat_upload",
                object_key=f"chat-uploads/ws-old/up-{i:02d}/missing_{i:02d}.xlsx",
                row_id=f"up-miss-{i:02d}",
                expected_size=1234,
                expected_sha256="a" * 64,
                check_mode="strict",
            )
        )
    for i in range(missing_asr_count):
        refs.append(
            storage_refs.ObjectReference(
                consumer="dalton_transcription_jobs",
                bucket_role="asr",
                object_key=f"transcriptions/ws-old/job-{i:02d}/source/missing_{i:02d}.mp3",
                row_id=f"job-miss-{i:02d}",
                expected_size=5678,
                expected_sha256="b" * 64,
                check_mode="strict",
            )
        )
    return storage_refs.SnapshotRefInventory(references=refs)


@pytest.mark.parametrize("versioning_enabled", [False, True])
def test_missing_referenced_small_count_succeeds_manifest_log_and_alert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    versioning_enabled: bool,
) -> None:
    """11 missing objects on dalton-asr (<50) -> succeeds, records manifest, logs & alerts once."""
    present_chat = b"present-chat-xlsx-bytes"
    present_asr = b"present-asr-mp3-bytes"
    inventory = _build_prod_like_missing_inventory(
        present_chat_bytes=present_chat,
        present_asr_bytes=present_asr,
        missing_chat_count=4,
        missing_asr_count=7,
    )

    enum_spy = create_autospec(storage_refs.enumerate_snapshot_refs, return_value=inventory)
    monkeypatch.setattr(consistent_backup, "enumerate_snapshot_refs", enum_spy)

    s3 = _make_autospec_s3_client(
        versioning_by_bucket={"dalton-asr": versioning_enabled},
        objects_by_bucket={
            "dalton-asr": {
                "chat-uploads/ws-ok/up-ok/present.xlsx": present_chat,
                "transcriptions/ws-ok/job-ok/source/present.mp3": present_asr,
            }
        },
    )
    conn = _make_autospec_pg_conn("snap-missing-11")
    dump_spy = _make_pg_dump_spy()
    logger_spy = create_autospec(logging.Logger, instance=True)

    alerts_file = tmp_path / "ops-alerts.log"
    append_spy = create_autospec(backup_lib.append_alert, side_effect=backup_lib.append_alert)

    alert_spy = create_autospec(
        _alert_signature,
        side_effect=lambda code, msg: local_backup_set.emit_missing_referenced_alert(
            code,
            msg,
            env={"DALTON_BACKUP_ALERTS_FILE": str(alerts_file)},
            append_alert_fn=append_spy,
        ),
    )

    backups_root = tmp_path / "backups"
    dump_stage = backups_root / ".tmp" / "dalton-pg-20261128.dump"
    sink = consistent_backup.MirrorPoolSink(backups_root / "minio-mirror")

    res = asyncio.run(
        consistent_backup.run_consistent_backup(
            conn=conn,
            pg_dump_path=dump_stage,
            pg_dump_fn=dump_spy,
            bucket_specs=[
                consistent_backup.BucketArchiveSpec(
                    bucket="dalton-asr",
                    roles=("asr", "chat_upload"),
                    s3_client=s3,
                )
            ],
            object_sink=sink,
            alert_fn=alert_spy,
            target_logger=logger_spy,
        )
    )

    assert dump_stage.is_file()
    assert res.reconcile_report.ok is True
    assert len(res.reconcile_report.known_missing) == 11
    assert len(res.manifest_objects) == 2
    assert len(res.missing_referenced_objects) == 11
    assert {m.consumer for m in res.missing_referenced_objects} == {
        "dalton_chat_file_uploads",
        "dalton_transcription_jobs",
    }
    assert all(m.bucket == "dalton-asr" for m in res.missing_referenced_objects)

    # Logger reports missing objects ONCE with total count (11) and first 10 keys (+1 more)
    missing_log_calls = [
        call
        for call in logger_spy.warning.call_args_list
        if "missing snapshot-referenced object" in (call.args[0] % call.args[1:] if len(call.args) > 1 else str(call.args[0]))
    ]
    assert len(missing_log_calls) == 1
    logged_msg = (
        missing_log_calls[0].args[0] % missing_log_calls[0].args[1:]
        if len(missing_log_calls[0].args) > 1
        else str(missing_log_calls[0].args[0])
    )
    assert "11 missing snapshot-referenced object(s)" in logged_msg
    assert "(+1 more)" in logged_msg
    first_10_keys = [m.key for m in res.missing_referenced_objects[:10]]
    eleventh_key = res.missing_referenced_objects[10].key
    for k in first_10_keys:
        assert k in logged_msg
    assert eleventh_key not in logged_msg

    # Ops alert exit called ONCE with count and first 10 keys, and persisted to alerts log
    assert alert_spy.call_count == 1
    assert append_spy.call_count == 1
    alert_code, alert_msg = alert_spy.call_args.args
    assert alert_code == consistent_backup.MISSING_REFERENCED_OBJECTS_ALERT_CODE
    assert "11 missing snapshot-referenced object(s)" in alert_msg
    assert "(+1 more)" in alert_msg
    assert eleventh_key not in alert_msg
    alert_log_text = alerts_file.read_text(encoding="utf-8")
    assert "[WARN]" in alert_log_text
    assert "[MISSING_REFERENCED_OBJECTS]" in alert_log_text
    assert "11 missing snapshot-referenced object(s)" in alert_log_text

    # Publish Everflow backup set -> verify missing_referenced_objects in backup-set & minio-manifest
    published = local_backup_set.publish_local_backup_set(
        backups_root=backups_root,
        backup_set_id="20261128-030000",
        tag="manual-pre-blob-inline",
        snapshot_id=res.snapshot_id,
        staged_dump_path=dump_stage,
        manifest_objects=res.manifest_objects,
        asr_summary=res.asr_archive,
        reconcile_ok=res.reconcile_report.ok,
        missing_referenced_objects=res.missing_referenced_objects,
    )
    set_json = json.loads(published.set_path.read_text(encoding="utf-8"))
    latest_json = json.loads(
        (backups_root / "sets" / "latest-verified-set.json").read_text(encoding="utf-8")
    )
    manifest_json = json.loads(published.manifest_path.read_text(encoding="utf-8"))

    for doc in (set_json, latest_json, manifest_json):
        assert doc["missing_referenced_count"] == 11
        assert len(doc["missing_referenced_objects"]) == 11
        assert all(
            set(entry.keys()) == {"bucket", "key", "consumer"}
            for entry in doc["missing_referenced_objects"]
        )

    # Also verify Standalone Core TarGzObjectSink embeds missing_referenced_objects in _minio_manifest.json
    tar_path = tmp_path / "standalone" / "minio-objects.tar.gz"
    tar_sink = consistent_backup.TarGzObjectSink(tar_path)
    dump_stage_core = tmp_path / "standalone" / "postgres.dump"
    res_core = asyncio.run(
        consistent_backup.run_consistent_backup(
            conn=_make_autospec_pg_conn("snap-core-11"),
            pg_dump_path=dump_stage_core,
            pg_dump_fn=_make_pg_dump_spy(),
            bucket_specs=[
                consistent_backup.BucketArchiveSpec(
                    bucket="dalton-asr",
                    roles=("asr", "chat_upload"),
                    s3_client=s3,
                )
            ],
            object_sink=tar_sink,
            target_logger=create_autospec(logging.Logger, instance=True),
        )
    )
    assert len(res_core.missing_referenced_objects) == 11
    with tarfile.open(tar_path, "r:gz") as tf:
        mf_fh = tf.extractfile("_minio_manifest.json")
        assert mf_fh is not None
        tar_manifest = json.loads(mf_fh.read().decode("utf-8"))
    assert tar_manifest["missing_referenced_count"] == 11
    assert len(tar_manifest["missing_referenced_objects"]) == 11


def test_missing_referenced_exceeds_threshold_fails_and_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing count > threshold (default 50, or DALTON_BACKUP_MAX_MISSING_REFS) MUST fail."""
    present_chat = b"ok-chat"
    present_asr = b"ok-asr"

    # 1. Default threshold 50: 51 missing objects -> MUST fail
    inv_51 = _build_prod_like_missing_inventory(
        present_chat_bytes=present_chat,
        present_asr_bytes=present_asr,
        missing_chat_count=20,
        missing_asr_count=31,
    )
    monkeypatch.setattr(
        consistent_backup,
        "enumerate_snapshot_refs",
        create_autospec(storage_refs.enumerate_snapshot_refs, return_value=inv_51),
    )
    s3 = _make_autospec_s3_client(
        versioning_by_bucket={"dalton-asr": False},
        objects_by_bucket={
            "dalton-asr": {
                "chat-uploads/ws-ok/up-ok/present.xlsx": present_chat,
                "transcriptions/ws-ok/job-ok/source/present.mp3": present_asr,
            }
        },
    )
    alert_spy = create_autospec(_alert_signature)
    with pytest.raises(consistent_backup.MissingReferencedObjectsError) as exc_info:
        asyncio.run(
            consistent_backup.run_consistent_backup(
                conn=_make_autospec_pg_conn("snap-51"),
                pg_dump_path=tmp_path / "dump51.dump",
                pg_dump_fn=_make_pg_dump_spy(),
                bucket_specs=[
                    consistent_backup.BucketArchiveSpec(
                        bucket="dalton-asr",
                        roles=("asr", "chat_upload"),
                        s3_client=s3,
                    )
                ],
                object_sink=consistent_backup.MirrorPoolSink(tmp_path / "pool51"),
                alert_fn=alert_spy,
                env={},
            )
        )
    assert exc_info.value.threshold == 50
    assert len(exc_info.value.missing_keys) == 51
    assert alert_spy.call_count == 0

    # 2. Custom env DALTON_BACKUP_MAX_MISSING_REFS=3: 4 missing fails, 3 missing succeeds
    inv_4 = _build_prod_like_missing_inventory(
        present_chat_bytes=present_chat,
        present_asr_bytes=present_asr,
        missing_chat_count=2,
        missing_asr_count=2,
    )
    monkeypatch.setattr(
        consistent_backup,
        "enumerate_snapshot_refs",
        create_autospec(storage_refs.enumerate_snapshot_refs, return_value=inv_4),
    )
    with pytest.raises(consistent_backup.MissingReferencedObjectsError) as exc_env:
        asyncio.run(
            consistent_backup.run_consistent_backup(
                conn=_make_autospec_pg_conn("snap-4"),
                pg_dump_path=tmp_path / "dump4.dump",
                pg_dump_fn=_make_pg_dump_spy(),
                bucket_specs=[
                    consistent_backup.BucketArchiveSpec(
                        bucket="dalton-asr",
                        roles=("asr", "chat_upload"),
                        s3_client=s3,
                    )
                ],
                object_sink=consistent_backup.MirrorPoolSink(tmp_path / "pool4"),
                env={"DALTON_BACKUP_MAX_MISSING_REFS": "3"},
            )
        )
    assert exc_env.value.threshold == 3

    # Exact boundary (4 missing with threshold=4) succeeds
    res_4 = asyncio.run(
        consistent_backup.run_consistent_backup(
            conn=_make_autospec_pg_conn("snap-4-ok"),
            pg_dump_path=tmp_path / "dump4ok.dump",
            pg_dump_fn=_make_pg_dump_spy(),
            bucket_specs=[
                consistent_backup.BucketArchiveSpec(
                    bucket="dalton-asr",
                    roles=("asr", "chat_upload"),
                    s3_client=s3,
                )
            ],
            object_sink=consistent_backup.MirrorPoolSink(tmp_path / "pool4ok"),
            env={"DALTON_BACKUP_MAX_MISSING_REFS": "4"},
        )
    )
    assert len(res_4.missing_referenced_objects) == 4

    # Threshold=0 disables tolerance even for 1 missing object
    with pytest.raises(consistent_backup.MissingReferencedObjectsError):
        asyncio.run(
            consistent_backup.run_consistent_backup(
                conn=_make_autospec_pg_conn("snap-0"),
                pg_dump_path=tmp_path / "dump0.dump",
                pg_dump_fn=_make_pg_dump_spy(),
                bucket_specs=[
                    consistent_backup.BucketArchiveSpec(
                        bucket="dalton-asr",
                        roles=("asr", "chat_upload"),
                        s3_client=s3,
                    )
                ],
                object_sink=consistent_backup.MirrorPoolSink(tmp_path / "pool0"),
                env={"DALTON_BACKUP_MAX_MISSING_REFS": "0"},
            )
        )


@pytest.mark.parametrize(
    ("bucket_name", "roles", "bucket_role"),
    [
        ("dalton-main", ("main",), "main"),
        ("dalton-storage", ("main",), "main"),
        ("dalton-main", ("asr",), "asr"),
    ],
)
def test_missing_referenced_on_main_bucket_always_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bucket_name: str,
    roles: tuple[str, ...],
    bucket_role: str,
) -> None:
    """Even 1 missing referenced object on dalton-main or role='main' MUST fail backup."""
    blob_data = b"workspace-blob-bytes"
    inventory = storage_refs.SnapshotRefInventory(
        references=[
            storage_refs.ObjectReference(
                consumer="workspace_file_blobs",
                bucket_role=bucket_role,
                object_key="blobs/u1/" + ("c" * 64),
                row_id="u1:" + ("c" * 64),
                expected_size=len(blob_data),
                expected_sha256=_sha(blob_data),
                check_mode="strict",
            )
        ]
    )
    monkeypatch.setattr(
        consistent_backup,
        "enumerate_snapshot_refs",
        create_autospec(storage_refs.enumerate_snapshot_refs, return_value=inventory),
    )
    s3 = _make_autospec_s3_client(
        versioning_by_bucket={bucket_name: True},
        objects_by_bucket={bucket_name: {}},
    )
    alert_spy = create_autospec(_alert_signature)
    with pytest.raises(consistent_backup.MissingReferencedObjectsError) as exc_info:
        asyncio.run(
            consistent_backup.run_consistent_backup(
                conn=_make_autospec_pg_conn("snap-main-miss"),
                pg_dump_path=tmp_path / "dump-main.dump",
                pg_dump_fn=_make_pg_dump_spy(),
                bucket_specs=[
                    consistent_backup.BucketArchiveSpec(
                        bucket=bucket_name,
                        roles=roles,
                        s3_client=s3,
                    )
                ],
                object_sink=consistent_backup.MirrorPoolSink(tmp_path / "pool-main"),
                alert_fn=alert_spy,
                env={"DALTON_BACKUP_MAX_MISSING_REFS": "50"},
            )
        )
    assert exc_info.value.bucket == bucket_name
    assert alert_spy.call_count == 0


def test_restore_verification_accepts_known_missing_and_rejects_unregistered_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Restore verification in backup_lib, storage_refs, and consistent_backup handles known missing."""
    present_chat = b"present-chat-data"
    present_asr = b"present-asr-data"
    known_missing_key = "chat-uploads/ws-old/up-01/old.xlsx"
    unregistered_missing_key = "transcriptions/ws-real/job-99/source/lost.mp3"

    # Build backup-set & minio-manifest registering known_missing_key
    backups_root = tmp_path / "backups"
    pool_dir = backups_root / "minio-mirror" / "objects"
    for payload in (present_chat, present_asr):
        sha = _sha(payload)
        p = pool_dir / sha[:2] / sha
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(payload)

    staged_dump = backups_root / ".tmp" / "pg.dump"
    staged_dump.parent.mkdir(parents=True, exist_ok=True)
    staged_dump.write_bytes(b"PGDMP-TEST")

    manifest_records = [
        consistent_backup.ArchivedObjectRecord(
            bucket="dalton-asr",
            key="chat-uploads/ws-ok/up-ok/present.xlsx",
            version_id=None,
            size=len(present_chat),
            archived_sha256=_sha(present_chat),
        ),
        consistent_backup.ArchivedObjectRecord(
            bucket="dalton-asr",
            key="transcriptions/ws-ok/job-ok/source/present.mp3",
            version_id=None,
            size=len(present_asr),
            archived_sha256=_sha(present_asr),
        ),
    ]
    published = local_backup_set.publish_local_backup_set(
        backups_root=backups_root,
        backup_set_id="20261128-040000",
        tag="daily",
        snapshot_id="snap-restore-1",
        staged_dump_path=staged_dump,
        manifest_objects=manifest_records,
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

    # 1. backup_lib.verify_restored_references -> passes on known missing and reports via log_fn
    log_spy = create_autospec(_log_signature)
    report_ok = backup_lib.verify_restored_references(
        referenced_objects=[
            {
                "bucket": "dalton-asr",
                "key": "chat-uploads/ws-ok/up-ok/present.xlsx",
                "consumer": "dalton_chat_file_uploads",
            },
            {
                "bucket": "dalton-asr",
                "key": known_missing_key,
                "consumer": "dalton_chat_file_uploads",
            },
        ],
        present_objects=[
            ("dalton-asr", "chat-uploads/ws-ok/up-ok/present.xlsx"),
            ("dalton-asr", "transcriptions/ws-ok/job-ok/source/present.mp3"),
        ],
        manifest_or_set=published.set_path,
        log_fn=log_spy,
    )
    assert report_ok.ok is True
    assert report_ok.present_count == 1
    assert len(report_ok.known_missing) == 1
    assert report_ok.known_missing[0].key == known_missing_key
    assert report_ok.unexpected_missing == ()
    assert log_spy.call_count == 1
    assert "KNOWN_MISSING_OBJECTS" in log_spy.call_args.args[0]
    assert known_missing_key in log_spy.call_args.args[0]

    # Unregistered missing object STILL fails backup_lib.verify_restored_references
    report_fail = backup_lib.verify_restored_references(
        referenced_objects=[
            {"bucket": "dalton-asr", "key": known_missing_key},
            {"bucket": "dalton-asr", "key": unregistered_missing_key},
        ],
        present_objects=[],
        manifest_or_set=published.set_path,
    )
    assert report_fail.ok is False
    assert len(report_fail.known_missing) == 1
    assert len(report_fail.unexpected_missing) == 1
    assert report_fail.unexpected_missing[0].key == unregistered_missing_key

    # 2. backup_lib CLI `verify-restored-refs` -> exit 0 on known missing, exit 1 on unregistered
    inv_known = storage_refs.SnapshotRefInventory(
        references=[
            storage_refs.ObjectReference(
                consumer="dalton_chat_file_uploads",
                bucket_role="chat_upload",
                object_key="chat-uploads/ws-ok/up-ok/present.xlsx",
                row_id="up-ok",
                expected_size=len(present_chat),
                expected_sha256=_sha(present_chat),
                check_mode="strict",
            ),
            storage_refs.ObjectReference(
                consumer="dalton_chat_file_uploads",
                bucket_role="chat_upload",
                object_key=known_missing_key,
                row_id="up-known-miss",
                expected_size=999,
                expected_sha256="d" * 64,
                check_mode="strict",
            ),
        ]
    )
    refs_known_path = tmp_path / "refs-known.json"
    refs_known_path.write_text(json.dumps(inv_known.to_dict()), encoding="utf-8")

    rc_ok = backup_lib.main(
        [
            "verify-restored-refs",
            "--backup-set",
            str(published.set_path),
            "--refs-json",
            str(refs_known_path),
            "--aux-bucket",
            "dalton-asr",
        ]
    )
    assert rc_ok == 0
    cli_out_ok = json.loads(capsys.readouterr().out)
    assert cli_out_ok["ok"] is True
    assert cli_out_ok["known_missing_count"] == 1
    assert cli_out_ok["error_count"] == 0

    inv_unreg = storage_refs.SnapshotRefInventory(
        references=[
            *inv_known.references,
            storage_refs.ObjectReference(
                consumer="dalton_transcription_jobs",
                bucket_role="asr",
                object_key=unregistered_missing_key,
                row_id="job-lost",
                expected_size=888,
                expected_sha256="e" * 64,
                check_mode="strict",
            ),
        ]
    )
    refs_unreg_path = tmp_path / "refs-unreg.json"
    refs_unreg_path.write_text(json.dumps(inv_unreg.to_dict()), encoding="utf-8")
    rc_fail = backup_lib.main(
        [
            "verify-restored-refs",
            "--backup-set",
            str(published.set_path),
            "--refs-json",
            str(refs_unreg_path),
            "--aux-bucket",
            "dalton-asr",
        ]
    )
    assert rc_fail == 1
    cli_out_fail = json.loads(capsys.readouterr().out)
    assert cli_out_fail["ok"] is False
    assert cli_out_fail["known_missing_count"] == 1
    assert cli_out_fail["error_count"] == 1

    # 3. consistent_backup.restore_objects_from_manifest with known_missing_objects
    target_s3 = _make_autospec_s3_client(versioning_by_bucket={"dalton-asr": False})
    sha_index = consistent_backup.build_archived_sha256_index(mirror_objects_dir=pool_dir)
    restore_logger_spy = create_autospec(logging.Logger, instance=True)
    restored = consistent_backup.restore_objects_from_manifest(
        [r.to_dict() for r in manifest_records],
        sha_index=sha_index,
        s3_clients_by_bucket={"dalton-asr": target_s3},
        known_missing_objects=json.loads(published.set_path.read_text(encoding="utf-8"))[
            "missing_referenced_objects"
        ],
        target_logger=restore_logger_spy,
    )
    assert restored == {
        "dalton-asr": {
            "chat-uploads/ws-ok/up-ok/present.xlsx",
            "transcriptions/ws-ok/job-ok/source/present.mp3",
        }
    }
    assert restore_logger_spy.warning.call_count == 1


def test_standalone_missing_referenced_backup_manifest_and_alert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Standalone Core `_cmd_backup` records missing_referenced_objects in JSON + tar manifest and alerts once."""
    present_chat = b"standalone-present-chat"
    present_asr = b"standalone-present-asr"
    inventory = _build_prod_like_missing_inventory(
        present_chat_bytes=present_chat,
        present_asr_bytes=present_asr,
        missing_chat_count=2,
        missing_asr_count=1,
    )
    monkeypatch.setattr(
        consistent_backup,
        "enumerate_snapshot_refs",
        create_autospec(storage_refs.enumerate_snapshot_refs, return_value=inventory),
    )

    s3 = _make_autospec_s3_client(
        versioning_by_bucket={"chat-uploads": True},
        objects_by_bucket={
            "chat-uploads": {
                "chat-uploads/ws-ok/up-ok/present.xlsx": present_chat,
                "transcriptions/ws-ok/job-ok/source/present.mp3": present_asr,
            }
        },
    )
    alerts_file = tmp_path / "standalone-alerts.log"
    monkeypatch.setenv("DALTON_BACKUP_ALERTS_FILE", str(alerts_file))
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME", "chat-uploads")

    conn = _make_autospec_pg_conn("snap-standalone-missing")
    conn_opener = create_autospec(standalone_cb.open_standalone_session_connection, return_value=conn)
    pg_dump_spy = create_autospec(standalone_cb._start_pg_dump)

    def _dump(snapshot_id: str, out_path: Path, *, dsn: str) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(f"PGDMP:{snapshot_id}:{dsn}".encode("utf-8"))

    pg_dump_spy.side_effect = _dump
    bucket_resolver = create_autospec(
        standalone_cb.resolve_standalone_bucket_specs,
        return_value=[
            consistent_backup.BucketArchiveSpec(
                bucket="chat-uploads",
                roles=("asr", "chat_upload"),
                s3_client=s3,
            )
        ],
    )
    alert_spy = create_autospec(
        standalone_cb._emit_standalone_missing_alert,
        side_effect=standalone_cb._emit_standalone_missing_alert,
    )

    out_dir = tmp_path / "out"
    parser = standalone_cb.build_arg_parser()
    args = parser.parse_args(
        ["backup", "--output-dir", str(out_dir), "--pg-dsn", "postgresql://u:p@localhost:5432/db"]
    )
    rc = standalone_cb._cmd_backup(
        args,
        conn_opener=conn_opener,
        pg_dump_starter=pg_dump_spy,
        bucket_resolver=bucket_resolver,
        alert_fn=alert_spy,
    )
    assert rc == 0
    stdout_doc = json.loads(capsys.readouterr().out)
    assert stdout_doc["missing_referenced_count"] == 3
    assert len(stdout_doc["missing_referenced_objects"]) == 3
    assert stdout_doc["reconcile"]["ok"] is True
    assert stdout_doc["reconcile"]["known_missing_count"] == 3

    with tarfile.open(out_dir / "minio-objects.tar.gz", "r:gz") as tf:
        mf_fh = tf.extractfile("_minio_manifest.json")
        assert mf_fh is not None
        tar_manifest = json.loads(mf_fh.read().decode("utf-8"))
    assert tar_manifest["missing_referenced_count"] == 3
    assert len(tar_manifest["missing_referenced_objects"]) == 3

    assert alert_spy.call_count == 1
    assert "[MISSING_REFERENCED_OBJECTS]" in alerts_file.read_text(encoding="utf-8")

