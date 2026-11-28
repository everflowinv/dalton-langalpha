"""Unit tests for Backup Hotfix Round 4 (narrowed production topology + Codex R3 fixes).

Covers:
1. Narrowed storage topology validation at backup & restore entry:
   rejects main/aux sharing the same bucket name, same bucket name across
   different endpoints, and distinct/ambiguous endpoint spellings with
   ``当前备份不支持该存储布局``.
2. ``verify-restored-refs`` uses THIS backup set's ``expired_by_rule_objects``
   instead of recomputing 7-day transcription expiration against restore day.
3. Standalone Core ``backup.sh`` logs an error when alert persistence fails and
   still publishes the verified backup set (never aborts via ``set -e``).
4. Exception branches in ``storage_refs.py`` and ``minio_init.py`` never emit
   raw SDK exception text or XML response bodies (type + safe metadata only).

All test doubles use ``create_autospec``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, create_autospec

import boto3
import psycopg
import pytest
from botocore.exceptions import ClientError
from botocore.parsers import ResponseParserError

from scripts.dalton.ops import backup_lib
from scripts.dalton.ops.prod import local_backup_set
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
                    "LastModified": dt.datetime(2026, 11, 28, 3, 0, 0, tzinfo=dt.timezone.utc),
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
                    "LastModified": dt.datetime(2026, 11, 28, 3, 0, 0, tzinfo=dt.timezone.utc),
                }
            )
        return {"Contents": contents, "IsTruncated": False}

    def _get_obj(*, Bucket: str, Key: str, VersionId: str | None = None) -> dict[str, Any]:
        entry = store.get(Bucket, {}).get(Key)
        if entry is None:
            raise KeyError(f"NoSuchKey: {Bucket}/{Key}")
        data, ct = entry
        return {"Body": io.BytesIO(data), "ContentType": ct}

    client.get_bucket_versioning.side_effect = _get_ver
    client.list_object_versions.side_effect = _list_vers
    client.list_objects_v2.side_effect = _list_v2
    client.get_object.side_effect = _get_obj
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


def _load_minio_init_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "minio_init_r4_test", REPO_ROOT / "scripts/standalone/minio_init.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 1. Unsupported storage topologies rejected at backup & restore entry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case_name", "env"),
    [
        (
            "main_and_asr_same_bucket_same_endpoint",
            {
                "STORAGE_PROVIDER": "s3",
                "STORAGE_ENDPOINT_URL": "http://minio:9000",
                "STORAGE_BUCKET_NAME": "shared-bucket",
                "STORAGE_ACCESS_KEY_ID": "ak",
                "STORAGE_SECRET_ACCESS_KEY": "sk",
                "DALTON_ASR_STORAGE_ENDPOINT_URL": "http://minio:9000",
                "DALTON_ASR_STORAGE_BUCKET_NAME": "shared-bucket",
                "DALTON_ASR_STORAGE_KEY_PREFIX": "aux",
                "DALTON_ASR_STORAGE_ACCESS_KEY_ID": "ak",
                "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY": "sk",
            },
        ),
        (
            "main_and_chat_same_bucket_same_endpoint",
            {
                "STORAGE_PROVIDER": "s3",
                "STORAGE_ENDPOINT_URL": "http://minio:9000",
                "STORAGE_BUCKET_NAME": "shared-bucket",
                "STORAGE_ACCESS_KEY_ID": "ak",
                "STORAGE_SECRET_ACCESS_KEY": "sk",
                "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL": "http://minio:9000",
                "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME": "shared-bucket",
                "DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID": "ak",
                "DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY": "sk",
            },
        ),
        (
            "same_bucket_name_on_different_endpoints_main_aux",
            {
                "STORAGE_PROVIDER": "s3",
                "STORAGE_ENDPOINT_URL": "http://minio-a:9000",
                "STORAGE_BUCKET_NAME": "dalton-storage",
                "STORAGE_ACCESS_KEY_ID": "ak",
                "STORAGE_SECRET_ACCESS_KEY": "sk",
                "DALTON_ASR_STORAGE_ENDPOINT_URL": "http://minio-b:9000",
                "DALTON_ASR_STORAGE_BUCKET_NAME": "dalton-storage",
                "DALTON_ASR_STORAGE_ACCESS_KEY_ID": "ak",
                "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY": "sk",
            },
        ),
        (
            "same_bucket_name_on_different_endpoints_asr_chat",
            {
                "STORAGE_PROVIDER": "none",
                "DALTON_ASR_STORAGE_ENDPOINT_URL": "http://minio-a:9000",
                "DALTON_ASR_STORAGE_BUCKET_NAME": "aux-shared",
                "DALTON_ASR_STORAGE_ACCESS_KEY_ID": "ak",
                "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY": "sk",
                "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL": "http://minio-b:9000",
                "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME": "aux-shared",
                "DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID": "ak",
                "DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY": "sk",
            },
        ),
        (
            "ambiguous_endpoint_spellings_default_port",
            {
                "STORAGE_PROVIDER": "s3",
                "STORAGE_ENDPOINT_URL": "http://s3.internal",
                "STORAGE_BUCKET_NAME": "dalton-main",
                "STORAGE_ACCESS_KEY_ID": "ak",
                "STORAGE_SECRET_ACCESS_KEY": "sk",
                "DALTON_ASR_STORAGE_ENDPOINT_URL": "http://s3.internal:80",
                "DALTON_ASR_STORAGE_BUCKET_NAME": "dalton-asr",
                "DALTON_ASR_STORAGE_ACCESS_KEY_ID": "ak",
                "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY": "sk",
            },
        ),
        (
            "distinct_independent_asr_endpoint",
            {
                "STORAGE_PROVIDER": "s3",
                "STORAGE_ENDPOINT_URL": "http://minio:9000",
                "STORAGE_BUCKET_NAME": "dalton-main",
                "STORAGE_ACCESS_KEY_ID": "ak",
                "STORAGE_SECRET_ACCESS_KEY": "sk",
                "DALTON_ASR_STORAGE_ENDPOINT_URL": "http://asr-minio:9000",
                "DALTON_ASR_STORAGE_BUCKET_NAME": "dalton-asr",
                "DALTON_ASR_STORAGE_ACCESS_KEY_ID": "ak",
                "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY": "sk",
            },
        ),
    ],
)
def test_r4_unsupported_storage_topology_rejected_at_backup_and_restore_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case_name: str,
    env: dict[str, str],
) -> None:
    factory_spy = create_autospec(
        consistent_backup.build_backup_s3_client,
        return_value=_make_autospec_s3_client(versioning_by_bucket={}),
    )

    # 1a. Backup bucket resolution rejects BEFORE creating any S3 client
    with pytest.raises(
        consistent_backup.UnsupportedStorageTopologyError,
        match="当前备份不支持该存储布局",
    ):
        consistent_backup.resolve_runtime_bucket_specs(
            env=env,
            apply_core_defaults=False,
            rewrite_container_minio=False,
            client_factory=factory_spy,
        )
    assert factory_spy.call_count == 0, f"{case_name}: should reject before constructing S3 clients"

    # 1b. Restore verification CLI (`verify-restored-refs` -> `storage_refs reconcile`) rejects at entry
    manifest_path = tmp_path / f"manifest-{case_name}.json"
    manifest_path.write_text(json.dumps({"version": 2, "objects": []}), encoding="utf-8")
    refs_path = tmp_path / f"refs-{case_name}.json"
    refs_path.write_text(json.dumps(storage_refs.SnapshotRefInventory().to_dict()), encoding="utf-8")

    for k in (
        "STORAGE_PROVIDER",
        "STORAGE_ENDPOINT_URL",
        "S3_ENDPOINT_URL",
        "STORAGE_BUCKET_NAME",
        "S3_BUCKET_NAME",
        "STORAGE_KEY_PREFIX",
        "STORAGE_ACCESS_KEY_ID",
        "STORAGE_SECRET_ACCESS_KEY",
        "DALTON_ASR_STORAGE_ENDPOINT_URL",
        "DALTON_ASR_STORAGE_BUCKET_NAME",
        "DALTON_ASR_STORAGE_KEY_PREFIX",
        "DALTON_ASR_STORAGE_ACCESS_KEY_ID",
        "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY",
        "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL",
        "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME",
        "DALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX",
        "DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID",
        "DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    rc = storage_refs.main(
        [
            "reconcile",
            "--manifest",
            str(manifest_path),
            "--refs-json",
            str(refs_path),
            "--verify-target-buckets",
        ]
    )
    assert rc == storage_refs.EXIT_CHECK_FAILED
    err_out = capsys.readouterr().err
    assert "当前备份不支持该存储布局" in err_out

    # 1c. Standalone Core `storage_config.py check-env` (used by backup.sh & restore.sh at entry)
    env_file = tmp_path / f"{case_name}.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/standalone/storage_config.py"),
            "check-env",
            "--env-file",
            str(env_file),
            "--caller",
            "backup",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, f"{case_name}: stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "当前备份不支持该存储布局" in proc.stderr


def test_r4_run_consistent_backup_and_reconcile_reject_ambiguous_or_shared_specs_and_accept_prod_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    s3_a = _make_autospec_s3_client(versioning_by_bucket={"dalton-main": True, "dalton-asr": True})
    s3_b = _make_autospec_s3_client(versioning_by_bucket={"dalton-asr": True})
    conn = _make_autospec_async_db_conn(lambda _s, _p: [])
    dump_spy = create_autospec(_pg_dump_signature)

    # Ambiguous endpoint spelling on direct run_consistent_backup call -> rejected BEFORE touching DB!
    with pytest.raises(
        consistent_backup.UnsupportedStorageTopologyError,
        match="当前备份不支持该存储布局",
    ):
        asyncio.run(
            consistent_backup.run_consistent_backup(
                conn=conn,
                pg_dump_path=tmp_path / "pg.dump",
                pg_dump_fn=dump_spy,
                bucket_specs=[
                    consistent_backup.BucketArchiveSpec(
                        bucket="dalton-main",
                        roles=("main",),
                        s3_client=s3_a,
                        endpoint="http://s3.internal",
                    ),
                    consistent_backup.BucketArchiveSpec(
                        bucket="dalton-asr",
                        roles=("asr", "chat_upload"),
                        s3_client=s3_b,
                        endpoint="http://s3.internal:80",
                    ),
                ],
                object_sink=consistent_backup.MirrorPoolSink(tmp_path / "pool"),
            )
        )
    assert conn.cursor.call_count == 0
    assert dump_spy.call_count == 0

    # CLI override `--main-bucket X --aux-bucket X` on reconcile -> rejected at entry!
    mf = tmp_path / "mf.json"
    mf.write_text(json.dumps({"version": 2, "objects": []}), encoding="utf-8")
    refs = tmp_path / "refs.json"
    refs.write_text(json.dumps(storage_refs.SnapshotRefInventory().to_dict()), encoding="utf-8")
    for k in (
        "STORAGE_PROVIDER",
        "STORAGE_ENDPOINT_URL",
        "STORAGE_BUCKET_NAME",
        "DALTON_ASR_STORAGE_ENDPOINT_URL",
        "DALTON_ASR_STORAGE_BUCKET_NAME",
        "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL",
        "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME",
    ):
        monkeypatch.delenv(k, raising=False)
    rc_same_cli = storage_refs.main(
        [
            "reconcile",
            "--manifest",
            str(mf),
            "--refs-json",
            str(refs),
            "--main-bucket",
            "dalton-main",
            "--aux-bucket",
            "dalton-main",
        ]
    )
    assert rc_same_cli == storage_refs.EXIT_CHECK_FAILED
    assert "当前备份不支持该存储布局" in capsys.readouterr().err

    # Supported production default: single endpoint + distinct main/aux buckets -> accepted!
    factory_ok = create_autospec(
        consistent_backup.build_backup_s3_client,
        return_value=s3_a,
    )
    ok_specs = consistent_backup.resolve_runtime_bucket_specs(
        env={
            "STORAGE_PROVIDER": "s3",
            "STORAGE_ENDPOINT_URL": "http://minio:9000",
            "STORAGE_BUCKET_NAME": "dalton-main",
            "STORAGE_ACCESS_KEY_ID": "ak",
            "STORAGE_SECRET_ACCESS_KEY": "sk",
            "DALTON_ASR_STORAGE_ENDPOINT_URL": "http://minio:9000/",
            "DALTON_ASR_STORAGE_BUCKET_NAME": "dalton-asr",
            "DALTON_ASR_STORAGE_ACCESS_KEY_ID": "ak",
            "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY": "sk",
        },
        apply_core_defaults=False,
        rewrite_container_minio=False,
        client_factory=factory_ok,
    )
    assert {(s.bucket, s.roles) for s in ok_specs} == {
        ("dalton-main", ("main",)),
        ("dalton-asr", ("asr", "chat_upload")),
    }


# ---------------------------------------------------------------------------
# 2. verify-restored-refs uses backup set's expired_by_rule_objects (not restore day)
# ---------------------------------------------------------------------------


def test_r4_verify_restored_refs_uses_backup_expired_by_rule_not_restore_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Codex High #4: audio that was <7d old at backup time (NOT recorded in
    expired_by_rule_objects) MUST fail restore verification even when restore
    runs >7 days later; audio explicitly recorded in THIS backup set's
    expired_by_rule_objects passes."""
    restore_now = dt.datetime.now(dt.timezone.utc)
    # Both jobs are >7d old as of RESTORE DAY:
    # - job-unrecorded-omission was only 2d old at backup time (backup was 15d ago),
    #   erroneously omitted from manifest objects, missing_referenced_objects, AND expired_by_rule_objects.
    # - job-recorded-expired was already >7d old at backup time and recorded in THIS backup set's expired_by_rule_objects.
    audio_present = b"present-asr-audio"
    key_present = "transcriptions/ws1/job-present/source/ok.m4a"
    key_unrecorded = "transcriptions/ws1/job-unrecorded/source/leaked-gap.m4a"
    key_recorded_expired = "transcriptions/ws1/job-recorded-expired/source/old.m4a"

    db_rows_with_gap = [
        (
            "job-present",
            "succeeded",
            key_present,
            len(audio_present),
            _sha(audio_present),
            {"upload": {"completed": True, "completed_at": (restore_now - dt.timedelta(days=20)).isoformat()}},
            restore_now - dt.timedelta(days=20),
            restore_now - dt.timedelta(days=20),
            restore_now - dt.timedelta(days=20),
        ),
        (
            "job-unrecorded",
            "succeeded",
            key_unrecorded,
            4096,
            "a" * 64,
            {"upload": {"completed": True, "completed_at": (restore_now - dt.timedelta(days=17)).isoformat()}},
            restore_now - dt.timedelta(days=17),
            restore_now - dt.timedelta(days=17),
            restore_now - dt.timedelta(days=17),
        ),
        (
            "job-recorded-expired",
            "succeeded",
            key_recorded_expired,
            8192,
            "b" * 64,
            {"upload": {"completed": True, "completed_at": (restore_now - dt.timedelta(days=30)).isoformat()}},
            restore_now - dt.timedelta(days=30),
            restore_now - dt.timedelta(days=30),
            restore_now - dt.timedelta(days=30),
        ),
    ]

    orig_open_conn = storage_refs._open_cli_readonly_connection

    def _make_db_opener(rows: list[tuple[Any, ...]]) -> Any:
        def _query_handler(sql: str, params: Any) -> Any:
            s = " ".join(sql.split())
            if s.startswith("SELECT to_regclass"):
                tbl = params[0] if params else ""
                return (tbl,) if tbl == "public.dalton_transcription_jobs" else (None,)
            if "FROM dalton_transcription_jobs" in s:
                return rows
            return []

        async def _open() -> Any:
            return _make_autospec_async_db_conn(_query_handler)

        return create_autospec(orig_open_conn, side_effect=_open)

    # Publish a real backup set with job-present in objects & job-recorded-expired in expired_by_rule_objects
    backups_root = tmp_path / "backups"
    pool_dir = backups_root / "minio-mirror" / "objects"
    obj_path = pool_dir / _sha(audio_present)[:2] / _sha(audio_present)
    obj_path.parent.mkdir(parents=True, exist_ok=True)
    obj_path.write_bytes(audio_present)
    staged_dump = backups_root / ".tmp" / "pg.dump"
    staged_dump.parent.mkdir(parents=True, exist_ok=True)
    staged_dump.write_bytes(b"PGDMP")

    published = local_backup_set.publish_local_backup_set(
        backups_root=backups_root,
        backup_set_id="20261113-030000",
        tag="daily",
        snapshot_id="snap-r4-exp",
        staged_dump_path=staged_dump,
        manifest_objects=[
            consistent_backup.ArchivedObjectRecord(
                bucket="dalton-asr",
                key=key_present,
                version_id=None,
                size=len(audio_present),
                archived_sha256=_sha(audio_present),
            )
        ],
        asr_summary=None,
        reconcile_ok=True,
        expired_by_rule_objects=[
            storage_refs.ExpiredByRuleObject(
                bucket="dalton-asr",
                key=key_recorded_expired,
                consumer="dalton_transcription_jobs",
                rule_id=storage_bucket_rules.RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE,
                row_id="job-recorded-expired",
            )
        ],
    )

    asr_dir = tmp_path / "restored-asr"
    for jid in ("job-present", "job-unrecorded", "job-recorded-expired"):
        jd = asr_dir / jid
        jd.mkdir(parents=True)
        (jd / "state.json").write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")
        (jd / "result.json").write_text(json.dumps({"text": "ok"}), encoding="utf-8")

    target_s3 = _make_autospec_s3_client(
        versioning_by_bucket={"dalton-asr": True},
        objects_by_bucket={"dalton-asr": {key_present: audio_present}},
    )
    orig_builder = storage_refs._build_target_s3_clients_for_reconcile
    monkeypatch.setattr(
        storage_refs,
        "_build_target_s3_clients_for_reconcile",
        create_autospec(orig_builder, return_value={"dalton-asr": target_s3}),
    )
    for k in (
        "STORAGE_PROVIDER",
        "STORAGE_ENDPOINT_URL",
        "STORAGE_BUCKET_NAME",
        "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL",
        "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DALTON_ASR_STORAGE_ENDPOINT_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("DALTON_ASR_STORAGE_BUCKET_NAME", "dalton-asr")
    monkeypatch.delenv("DALTON_ASR_STORAGE_KEY_PREFIX", raising=False)

    # Case 2a: DB has unrecorded job-unrecorded (>7d old on restore day!) -> MUST FAIL (never masked!)
    monkeypatch.setattr(
        storage_refs,
        "_open_cli_readonly_connection",
        _make_db_opener(db_rows_with_gap),
    )
    rc_fail = backup_lib.main(
        [
            "verify-restored-refs",
            "--backup-set",
            str(published.set_path),
            "--asr-dir",
            str(asr_dir),
            "--aux-bucket",
            "dalton-asr",
        ]
    )
    assert rc_fail == 1
    out_fail = json.loads(capsys.readouterr().out)
    assert out_fail["ok"] is False
    assert any(
        e["code"] == "missing_object" and e["row_id"] == "job-unrecorded"
        for e in out_fail["errors"]
    )
    assert out_fail["expired_by_rule_count"] == 1
    assert out_fail["expired_by_rule"][0]["row_id"] == "job-recorded-expired"

    # Case 2b: DB only has job-present (restored in target bucket) + job-recorded-expired (in backup set's
    # expired_by_rule_objects) -> PASSES with expired_by_rule_count=1 and 0 errors!
    monkeypatch.setattr(
        storage_refs,
        "_open_cli_readonly_connection",
        _make_db_opener([db_rows_with_gap[0], db_rows_with_gap[2]]),
    )
    rc_ok = backup_lib.main(
        [
            "verify-restored-refs",
            "--backup-set",
            str(published.set_path),
            "--asr-dir",
            str(asr_dir),
            "--aux-bucket",
            "dalton-asr",
        ]
    )
    assert rc_ok == 0
    out_ok = json.loads(capsys.readouterr().out)
    assert out_ok["ok"] is True
    assert out_ok["error_count"] == 0
    assert out_ok["known_missing_count"] == 0
    assert out_ok["expired_by_rule_count"] == 1
    assert out_ok["expired_by_rule"][0]["row_id"] == "job-recorded-expired"

    # Case 2c: Backup-set JSON injecting an extra expired_by_rule_objects entry not in manifest -> FAIL (exit 2)
    tampered_set = json.loads(published.set_path.read_text(encoding="utf-8"))
    tampered_set["expired_by_rule_objects"].append(
        {
            "bucket": "dalton-asr",
            "key": key_unrecorded,
            "consumer": "dalton_transcription_jobs",
            "rule_id": storage_bucket_rules.RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE,
            "row_id": "job-unrecorded",
        }
    )
    tampered_set_path = tmp_path / "tampered-set.json"
    tampered_set_path.write_text(json.dumps(tampered_set), encoding="utf-8")
    rc_tampered = backup_lib.main(
        [
            "verify-restored-refs",
            "--manifest",
            str(published.manifest_path),
            "--backup-set",
            str(tampered_set_path),
            "--asr-dir",
            str(asr_dir),
            "--aux-bucket",
            "dalton-asr",
        ]
    )
    assert rc_tampered == 2
    assert "expired_by_rule_objects" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 3. Standalone backup.sh alert write failure logs an error & still publishes
# ---------------------------------------------------------------------------


def _setup_standalone_backup_harness(tmp_path: Path) -> tuple[Path, Path]:
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
        dst_p.write_text(
            src_p.read_text(encoding="utf-8") if src_p.exists() else "",
            encoding="utf-8",
        )
    (work_dir / "src" / "__init__.py").write_text("", encoding="utf-8")
    (work_dir / ".env").write_text(
        "COMPOSE_PROJECT_NAME=dalton-r4-alert\n"
        "DB_HOST=postgres\n"
        "DB_PORT=5432\n"
        "DB_USER=dalton\n"
        "DB_PASSWORD=secret\n"
        "DB_NAME=dalton\n",
        encoding="utf-8",
    )
    (work_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    missing_items = [
        {
            "bucket": "chat-uploads",
            "key": "chat-uploads/ws1/up-01/missing.pdf",
            "consumer": "dalton_chat_file_uploads",
        }
    ]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
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
    sys.stderr.write("@@DALTON_CTL@@ SNAPSHOT_READY snap-r4-alert\\n")
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
        "missing_referenced_objects": {json.dumps(missing_items)},
        "missing_referenced_count": 1,
        "expired_by_rule_objects": [],
        "expired_by_rule_count": 0,
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
    sys.stderr.write(
        "@@DALTON_CTL@@ BACKUP_SUMMARY "
        + json.dumps({{
            "snapshot_id": "snap-r4-alert",
            "object_count": 0,
            "missing_referenced_count": 1,
            "missing_referenced_sample": ["chat-uploads/chat-uploads/ws1/up-01/missing.pdf"],
            "expired_by_rule_count": 0,
            "reconcile_ok": True,
            "checked_refs": 1,
        }})
        + "\\n"
    )
    sys.stderr.flush()
    sys.exit(0)
if "pg_dump" in joined:
    sys.stdout.buffer.write(b"PGDMP-SYNTHETIC")
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
    return work_dir, fake_bin


def test_r4_standalone_backup_alert_write_failure_logs_error_and_publishes_backup(
    tmp_path: Path,
) -> None:
    work_dir, fake_bin = _setup_standalone_backup_harness(tmp_path)

    # Case 3a: Primary DALTON_BACKUP_ALERTS_FILE is unwritable (existing directory) while fallback is writable
    out_dir_a = tmp_path / "backups-fallback"
    out_dir_a.mkdir()
    unwritable_primary = tmp_path / "primary-is-a-dir"
    unwritable_primary.mkdir()
    env_a = dict(os.environ)
    env_a["PATH"] = f"{fake_bin}:{env_a['PATH']}"
    env_a["DALTON_BACKUP_ALERTS_FILE"] = str(unwritable_primary)
    proc_a = subprocess.run(
        [
            "bash",
            str(work_dir / "scripts/standalone/backup.sh"),
            "--work-dir",
            str(work_dir),
            "--output-dir",
            str(out_dir_a),
            "--compose-files",
            "docker-compose.yml",
        ],
        env=env_a,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_a.returncode == 0, f"stdout={proc_a.stdout}\nstderr={proc_a.stderr}"
    assert "[backup] ERROR:" in proc_a.stderr
    assert (out_dir_a / "backup-alerts.log").is_file()
    published_a = [d for d in out_dir_a.iterdir() if d.is_dir() and d.name.startswith("dalton-backup-")]
    assert len(published_a) == 1
    assert (published_a[0] / "manifest.json").is_file()
    assert (published_a[0] / "SHA256SUMS").is_file()

    # Case 3b: BOTH primary AND fallback alert paths fail (fallback path is a directory) -> still logs ERROR & publishes!
    out_dir_b = tmp_path / "backups-both-fail"
    out_dir_b.mkdir()
    (out_dir_b / "backup-alerts.log").mkdir()  # directory blocks os.open(O_WRONLY|O_CREAT)
    env_b = dict(os.environ)
    env_b["PATH"] = f"{fake_bin}:{env_b['PATH']}"
    env_b["DALTON_BACKUP_ALERTS_FILE"] = str(unwritable_primary)
    proc_b = subprocess.run(
        [
            "bash",
            str(work_dir / "scripts/standalone/backup.sh"),
            "--work-dir",
            str(work_dir),
            "--output-dir",
            str(out_dir_b),
            "--compose-files",
            "docker-compose.yml",
        ],
        env=env_b,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_b.returncode == 0, f"stdout={proc_b.stdout}\nstderr={proc_b.stderr}"
    assert "[backup] ERROR:" in proc_b.stderr
    assert "continuing backup publication" in proc_b.stderr
    published_b = [d for d in out_dir_b.iterdir() if d.is_dir() and d.name.startswith("dalton-backup-")]
    assert len(published_b) == 1
    assert (published_b[0] / "manifest.json").is_file()
    assert (published_b[0] / "SHA256SUMS").is_file()


# ---------------------------------------------------------------------------
# 4. Exception branches never leak raw SDK exception text / XML response body
# ---------------------------------------------------------------------------


def test_r4_storage_refs_and_minio_init_do_not_leak_raw_sdk_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_body = "SECRET_RAW_XML_PAYLOAD_TOKEN_987654321"
    secret_msg = "SECRET_CLIENT_ERROR_MESSAGE_ABCDEF"

    # 4a. storage_refs.py target bucket query error with ClientError + ResponseParserError
    inv = storage_refs.SnapshotRefInventory(
        references=[
            storage_refs.ObjectReference(
                consumer="dalton_chat_file_uploads",
                bucket_role="chat_upload",
                object_key="chat-uploads/ws1/u1/a.pdf",
                row_id="u1",
                expected_size=5,
                expected_sha256=_sha(b"hello"),
                check_mode="strict",
            )
        ]
    )
    manifest_objs = [
        {
            "bucket": "dalton-asr",
            "key": "chat-uploads/ws1/u1/a.pdf",
            "size": 5,
            "archived_sha256": _sha(b"hello"),
        }
    ]

    s3_client_err = _make_autospec_s3_client(
        versioning_by_bucket={"dalton-asr": False},
        list_v2_error_by_bucket={
            "dalton-asr": ClientError(
                {
                    "Error": {"Code": "AccessDenied", "Message": secret_msg},
                    "ResponseMetadata": {"HTTPStatusCode": 403, "RequestId": "req-403-xyz"},
                },
                "ListObjectsV2",
            )
        },
    )
    report_ce = storage_refs.reconcile_inventory_against_manifest(
        inv,
        manifest_objs,
        bucket_targets={"chat_upload": ("dalton-asr", "")},
        target_s3_clients={"dalton-asr": s3_client_err},
        verify_target_buckets=True,
    )
    assert report_ce.ok is False
    ce_detail = report_ce.errors[0].detail
    assert "ClientError" in ce_detail
    assert "AccessDenied" in ce_detail
    assert "403" in ce_detail
    assert "req-403-xyz" in ce_detail
    assert secret_msg not in ce_detail

    s3_xml_err = _make_autospec_s3_client(
        versioning_by_bucket={"dalton-asr": False},
        list_v2_error_by_bucket={
            "dalton-asr": ResponseParserError(
                f"Unable to parse response: Invalid XML received: <Secret>{secret_body}</Secret>"
            )
        },
    )
    report_xml = storage_refs.reconcile_inventory_against_manifest(
        inv,
        manifest_objs,
        bucket_targets={"chat_upload": ("dalton-asr", "")},
        target_s3_clients={"dalton-asr": s3_xml_err},
        verify_target_buckets=True,
    )
    assert report_xml.ok is False
    xml_detail = report_xml.errors[0].detail
    assert "ResponseParserError" in xml_detail
    assert "ListObjectsV2" in xml_detail
    assert secret_body not in xml_detail

    # 4b. minio_init.py lifecycle & policy error branches sanitize raw SDK text/body
    minio_init = _load_minio_init_module()
    s3_spec = boto3.session.Session().client(
        "s3",
        endpoint_url="http://minio:9000",
        aws_access_key_id="ak",
        aws_secret_access_key="sk",
        region_name="us-east-1",
    )

    fake_xml_client = create_autospec(s3_spec)
    fake_xml_client.get_bucket_lifecycle_configuration.side_effect = ResponseParserError(
        f"Invalid XML body: <Credentials>{secret_body}</Credentials>"
    )
    rc_xml = minio_init._ensure_secondary_lifecycle(
        fake_xml_client,
        bucket="dalton-asr",
        key_prefix="",
        endpoint="http://minio:9000",
    )
    assert rc_xml == 1
    err_xml = capsys.readouterr().err
    assert "ResponseParserError" in err_xml
    assert "GetBucketLifecycleConfiguration" in err_xml
    assert secret_body not in err_xml

    fake_ce_client = create_autospec(s3_spec)
    fake_ce_client.get_bucket_lifecycle_configuration.side_effect = ClientError(
        {
            "Error": {"Code": "InternalError", "Message": secret_msg},
            "ResponseMetadata": {"HTTPStatusCode": 500, "RequestId": "req-500-abc"},
        },
        "GetBucketLifecycleConfiguration",
    )
    rc_ce = minio_init._ensure_secondary_lifecycle(
        fake_ce_client,
        bucket="dalton-asr",
        key_prefix="",
        endpoint="http://minio:9000",
    )
    assert rc_ce == 1
    err_ce = capsys.readouterr().err
    assert "ClientError" in err_ce
    assert "InternalError" in err_ce
    assert "500" in err_ce
    assert "req-500-abc" in err_ce
    assert secret_msg not in err_ce
