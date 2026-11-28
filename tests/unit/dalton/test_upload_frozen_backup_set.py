"""Unit tests for frozen backup set encrypted upload (upload_frozen_backup_set.sh & offsite_sync.py)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from collections import namedtuple
from pathlib import Path
from unittest.mock import create_autospec

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPS_DIR = REPO_ROOT / "scripts" / "dalton" / "ops"
PROD_DIR = OPS_DIR / "prod"
UPLOAD_SCRIPT = PROD_DIR / "upload_frozen_backup_set.sh"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

from scripts.dalton.ops import backup_lib
from scripts.dalton.ops.prod import offsite_sync
from tests.unit.dalton.test_offsite_backup import (
    _make_mounted_python_wrapper,
    _write_fake_rclone,
    _write_test_env,
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_valid_objects_tar_bytes(
    objects: dict[str, bytes],
    *,
    bucket: str = "dalton-asr",
    layout: str = "sha",  # "sha" or "bucket"
    backup_set_id: str = "",
) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        seen_shas: set[str] = set()
        meta_objs: list[dict[str, object]] = []
        for key, payload in sorted(objects.items()):
            sha = _sha256_bytes(payload)
            if layout == "sha":
                if sha in seen_shas:
                    continue
                seen_shas.add(sha)
                arcname = f"objects/{sha[:2]}/{sha}"
            else:
                arcname = f"objects/{bucket}/{key.lstrip('/')}"
            info = tarfile.TarInfo(name=arcname)
            info.size = len(payload)
            info.mtime = 0
            info.mode = 0o600
            tf.addfile(info, io.BytesIO(payload))
            meta_objs.append(
                {
                    "bucket": bucket,
                    "key": key,
                    "archived_sha256": sha,
                    "size": len(payload),
                }
            )
        meta_name = "_delta_manifest.json" if layout == "sha" else "_minio_manifest.json"
        meta_doc: dict[str, object] = {
            "version": 1,
            "object_count": len(meta_objs),
            "objects": meta_objs,
        }
        if backup_set_id:
            meta_doc["backup_set_id"] = backup_set_id
        meta_bytes = (json.dumps(meta_doc, sort_keys=True) + "\n").encode("utf-8")
        minfo = tarfile.TarInfo(name=meta_name)
        minfo.size = len(meta_bytes)
        minfo.mtime = 0
        minfo.mode = 0o600
        tf.addfile(minfo, io.BytesIO(meta_bytes))
    return buf.getvalue()


def _create_frozen_set(
    backups_root: Path,
    *,
    set_id: str = "20261128-140000",
    tag: str = "manual-pre-blob-inline",
    include_asr: bool = True,
    mirror_objects: dict[str, bytes] | None = None,
    prebuilt_objects: dict[str, bytes] | None = None,
    prebuilt_layout: str = "sha",
    prebuilt_raw_bytes: bytes | None = None,
    include_prebuilt_sha: bool = True,
    reconcile_ok: bool = True,
) -> dict[str, Path]:
    frozen_dir = backups_root / "frozen"
    sets_dir = backups_root / "sets"
    mirror_dir = backups_root / "minio-mirror" / "objects"
    for d in (frozen_dir, sets_dir, mirror_dir, backups_root / ".tmp"):
        d.mkdir(parents=True, exist_ok=True)

    dump_bytes = b"PGDMP-frozen-pre-blob-inline-payload-" * 64
    dump_path = frozen_dir / f"dalton-pg-{tag}-{set_id}.dump"
    dump_path.write_bytes(dump_bytes)

    asr_path: Path | None = None
    asr_bytes = b"ASR-TAR-GZ-SYNTHETIC-CONTENT-" * 32
    if include_asr:
        asr_path = frozen_dir / f"asr-jobs-{tag}-{set_id}.tar.gz"
        asr_path.write_bytes(asr_bytes)

    manifest_objs: list[dict[str, object]] = []
    source_objects = prebuilt_objects if prebuilt_objects is not None else mirror_objects
    if source_objects:
        for key, payload in source_objects.items():
            sha = _sha256_bytes(payload)
            if mirror_objects is not None and prebuilt_objects is None:
                obj_path = mirror_dir / sha[:2] / sha
                obj_path.parent.mkdir(parents=True, exist_ok=True)
                obj_path.write_bytes(payload)
            manifest_objs.append(
                {
                    "bucket": "dalton-asr",
                    "key": key,
                    "version_id": "v1",
                    "size": len(payload),
                    "archived_sha256": sha,
                }
            )

    manifest_path = sets_dir / f"minio-manifest-{set_id}.json"
    manifest_payload = {
        "version": 2,
        "backup_set_id": set_id,
        "tag": tag,
        "created_at": "2026-11-28T14:00:00+00:00",
        "snapshot_id": "00000003-00000001-1",
        "objects": manifest_objs,
    }
    manifest_path.write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_sha = _sha256_bytes(manifest_path.read_bytes())

    prebuilt_path: Path | None = None
    if prebuilt_raw_bytes is not None:
        prebuilt_path = frozen_dir / f"minio-objects-{tag}-{set_id}.tar.gz"
        prebuilt_path.write_bytes(prebuilt_raw_bytes)
    elif prebuilt_objects is not None:
        prebuilt_path = frozen_dir / f"minio-objects-{tag}-{set_id}.tar.gz"
        prebuilt_path.write_bytes(
            _build_valid_objects_tar_bytes(
                prebuilt_objects,
                layout=prebuilt_layout,
                backup_set_id=set_id,
            )
        )

    set_path = sets_dir / f"backup-set-{set_id}.json"
    set_payload = {
        "version": 1,
        "backup_set_id": set_id,
        "tag": tag,
        "created_at": "2026-11-28T14:00:00+00:00",
        "snapshot_id": "00000003-00000001-1",
        "dump_path": str(dump_path),
        "dump_size_bytes": len(dump_bytes),
        "dump_sha256": _sha256_bytes(dump_bytes),
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "set_path": str(set_path),
        "asr_archive_path": str(asr_path) if asr_path else None,
        "asr_archive_size_bytes": len(asr_bytes) if asr_path else None,
        "asr_archive_sha256": _sha256_bytes(asr_bytes) if asr_path else None,
        "object_count": len(manifest_objs),
        "reconcile_ok": reconcile_ok,
    }
    if prebuilt_path is not None and include_prebuilt_sha:
        prebuilt_data = prebuilt_path.read_bytes()
        set_payload["objects_archive_sha256"] = _sha256_bytes(prebuilt_data)
        set_payload["objects_archive_size_bytes"] = len(prebuilt_data)
    set_path.write_text(
        json.dumps(set_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    out: dict[str, Path] = {
        "dump": dump_path,
        "manifest": manifest_path,
        "set": set_path,
    }
    if asr_path is not None:
        out["asr"] = asr_path
    if prebuilt_path is not None:
        out["prebuilt_objects"] = prebuilt_path
    return out


def _decrypt_file(enc_file: Path, key_file: Path) -> bytes:
    dec = subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-in",
            str(enc_file),
            "-pass",
            f"file:{key_file}",
        ],
        capture_output=True,
        check=True,
    )
    return dec.stdout


def _read_rcat_uploaded_names(rcat_sizes_log: Path) -> list[str]:
    if not rcat_sizes_log.is_file():
        return []
    names: list[str] = []
    for line in rcat_sizes_log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rel = str(json.loads(line)["rel"])
        base = Path(rel).name
        if ".staged-" in base:
            base = base.split(".staged-", 1)[0]
        names.append(base)
    return names


def _write_fake_openssl(fake_bin: Path, *, enc_calls_log: Path) -> Path:
    real_openssl = shutil.which("openssl") or "/usr/bin/openssl"
    wrapper = fake_bin / "openssl"
    wrapper.write_text(
        f"""#!/usr/bin/env python3
import os, pathlib, sys

real_openssl = {real_openssl!r}
enc_log = pathlib.Path({str(enc_calls_log)!r})
args = sys.argv[1:]
if args and args[0] == "enc":
    mode = "decrypt" if "-d" in args else "encrypt"
    with enc_log.open("a", encoding="utf-8") as fh:
        fh.write(mode + "\\n")
os.execv(real_openssl, [real_openssl, *args])
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def _setup_offsite_env(
    tmp_path: Path,
    *,
    key_secret: str = "SUPER_SECRET_BACKUP_PASSPHRASE_999111",
    service_token: str = "SUPER_SECRET_INTERNAL_TOKEN_777222",
) -> dict[str, Path]:
    home_dir = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    drive_store = tmp_path / "drive_store"
    ext_mount = tmp_path / "ext_ssd"
    for d in (home_dir, fake_bin, drive_store, ext_mount):
        d.mkdir(parents=True, exist_ok=True)

    backups_root = ext_mount / "Dalton" / "backups"
    backups_root.mkdir(parents=True, exist_ok=True)

    user_cfg = home_dir / ".config" / "dalton"
    user_cfg.mkdir(parents=True, exist_ok=True)
    key_file = user_cfg / "backup.key"
    key_file.write_text(key_secret + "\n", encoding="utf-8")
    key_file.chmod(0o600)

    alerts_file = home_dir / "Library" / "Logs" / "dalton-offsite-backup-alerts.log"
    log_file = home_dir / "Library" / "Logs" / "dalton-offsite-backup.log"
    env_file = tmp_path / "offsite-backup.env"
    about_file = tmp_path / "about.json"
    about_file.write_text('{"free": 1000000000}\n', encoding="utf-8")
    proxy_check_file = tmp_path / "proxy_seen.txt"
    rcat_sizes_log = tmp_path / "rcat_sizes.jsonl"
    enc_calls_log = tmp_path / "openssl_enc_calls.log"

    _write_test_env(
        env_file,
        home_dir=home_dir,
        repo_dir=tmp_path / "repo",
        key_file=key_file,
        alerts_file=alerts_file,
        log_file=log_file,
        prod_checkout=tmp_path / "prod",
        prod_var=tmp_path / "prod" / "var",
        user_config=user_cfg,
        launchagents=home_dir / "Library" / "LaunchAgents",
        backup_scripts=backups_root / "pg",
        colima=home_dir / ".colima",
        ext_mount=ext_mount,
    )
    with env_file.open("a", encoding="utf-8") as fh:
        fh.write(f'DALTON_BACKUP_ROOT="{backups_root}"\n')
        fh.write('DALTON_RCLONE_REMOTE="gdrive"\n')
        fh.write('DALTON_RCLONE_DRIVE_DIR="DaltonBackup"\n')
        fh.write(f'INTERNAL_SERVICE_TOKEN="{service_token}"\n')
        fh.write('DALTON_CHANNEL_GATEWAY_URL=""\n')

    _write_fake_rclone(
        fake_bin,
        drive_store=drive_store,
        about_file=about_file,
        proxy_check_file=proxy_check_file,
        rcat_sizes_log=rcat_sizes_log,
    )
    _write_fake_openssl(fake_bin, enc_calls_log=enc_calls_log)
    mounted_py = _make_mounted_python_wrapper(tmp_path, ext_mount)

    return {
        "home_dir": home_dir,
        "fake_bin": fake_bin,
        "drive_store": drive_store,
        "ext_mount": ext_mount,
        "backups_root": backups_root,
        "key_file": key_file,
        "alerts_file": alerts_file,
        "log_file": log_file,
        "env_file": env_file,
        "about_file": about_file,
        "proxy_check_file": proxy_check_file,
        "rcat_sizes_log": rcat_sizes_log,
        "enc_calls_log": enc_calls_log,
        "mounted_py": mounted_py,
    }


def _assert_no_secrets(
    secrets: tuple[str, ...],
    *,
    stdout: str = "",
    stderr: str = "",
    files: tuple[Path, ...] = (),
) -> None:
    for secret in secrets:
        assert secret not in stdout
        assert secret not in stderr
        for path in files:
            if path.is_file():
                assert secret not in path.read_text(encoding="utf-8", errors="replace")


def test_frozen_upload_script_bash_syntax() -> None:
    proc = subprocess.run(
        ["bash", "-n", str(UPLOAD_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_frozen_plan_and_remote_verify_unit_with_autospec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backups_root = tmp_path / "backups"
    _create_frozen_set(
        backups_root,
        set_id="20261128-140500",
        mirror_objects={"uploads/ws1/audio.wav": b"audio-bytes-123"},
    )

    plan = offsite_sync.resolve_frozen_backup_set(
        backup_set_id="20261128-140500",
        backups_root=backups_root,
        remote="gdrive",
        drive_dir="DaltonBackup",
    )
    assert plan.backup_set_id == "20261128-140500"
    assert plan.remote_dir_name == "manual-pre-blob-inline-20261128-140500"
    assert plan.remote_dir == "gdrive:DaltonBackup/frozen/manual-pre-blob-inline-20261128-140500"
    assert [a.key for a in plan.artifacts] == [
        "dump",
        "asr_jobs",
        "minio_manifest",
        "backup_set",
        "minio_objects",
    ]
    assert plan.artifacts[-1].kind == "mirror_pack"
    assert plan.artifacts[-1].object_count == 1
    assert len(plan.artifacts[-1].pack_fingerprint) == 64

    # Accepting "<tag>-<set_id>" resolves the same backup set.
    plan_by_full = offsite_sync.resolve_frozen_backup_set(
        backup_set_id="manual-pre-blob-inline-20261128-140500",
        backups_root=backups_root,
        remote="gdrive",
        drive_dir="DaltonBackup",
    )
    assert plan_by_full.backup_set_id == plan.backup_set_id
    assert plan_by_full.remote_dir == plan.remote_dir

    # Unreconciled set is rejected.
    bad_root = tmp_path / "bad_backups"
    _create_frozen_set(bad_root, set_id="20261128-140600", reconcile_ok=False)
    with pytest.raises(ValueError, match="reconcile_ok"):
        offsite_sync.resolve_frozen_backup_set(
            backup_set_id="20261128-140600",
            backups_root=bad_root,
            remote="gdrive",
            drive_dir="DaltonBackup",
        )

    # Path traversal set ID is rejected.
    with pytest.raises(ValueError, match="Invalid backup set ID"):
        offsite_sync.resolve_frozen_backup_set(
            backup_set_id="../escape",
            backups_root=backups_root,
            remote="gdrive",
            drive_dir="DaltonBackup",
        )

    # Verify delegation to backup_lib.verify_rclone_object_size / verify_rclone_hashsum via create_autospec.
    mock_verify_size = create_autospec(
        backup_lib.verify_rclone_object_size,
        return_value=(True, "size ok"),
    )
    mock_verify_hash = create_autospec(
        backup_lib.verify_rclone_hashsum,
        return_value=(True, "md5 ok"),
    )
    monkeypatch.setattr(backup_lib, "verify_rclone_object_size", mock_verify_size)
    monkeypatch.setattr(backup_lib, "verify_rclone_hashsum", mock_verify_hash)

    plan_dict = plan.to_dict()
    dump_art = plan_dict["artifacts"][0]
    key_fp = "0123456789abcdef"
    manifest = offsite_sync.update_frozen_manifest(
        plan=plan_dict,
        existing_manifest=None,
        artifact_key="dump",
        plain_size=dump_art["plain_size"],
        stream_meta={
            "size": dump_art["expected_enc_size"],
            "md5": "d41d8cd98f00b204e9800998ecf8427e",
            "sha256": "a" * 64,
        },
        plain_meta={
            "size": dump_art["plain_size"],
            "sha256": dump_art["source_sha256"],
        },
        key_fingerprint=key_fp,
    )
    assert manifest["key_fingerprint"] == key_fp
    assert manifest["artifacts"]["dump"]["key_fingerprint"] == key_fp
    assert manifest["artifacts"]["dump"]["source_sha256"] == dump_art["source_sha256"]

    ok, _ = offsite_sync.is_frozen_artifact_verified_on_remote(
        artifact=dump_art,
        remote_manifest=manifest,
        lsjson_out="[]",
        hashsum_out="",
        current_key_fingerprint=key_fp,
        expected_backup_set_id=plan.backup_set_id,
    )
    assert ok is True
    mock_verify_size.assert_called_once_with(
        "[]", dump_art["remote_filename"], dump_art["expected_enc_size"]
    )
    mock_verify_hash.assert_called_once_with(
        "", dump_art["remote_filename"], "d41d8cd98f00b204e9800998ecf8427e"
    )

    # Key fingerprint mismatch refuses skip.
    ok_wrong_key, reason = offsite_sync.is_frozen_artifact_verified_on_remote(
        artifact=dump_art,
        remote_manifest=manifest,
        lsjson_out="[]",
        hashsum_out="",
        current_key_fingerprint="fedcba9876543210",
        expected_backup_set_id=plan.backup_set_id,
    )
    assert ok_wrong_key is False
    assert "key_fingerprint" in reason

    # Local pack space check via create_autospec(shutil.disk_usage).
    Usage = namedtuple("usage", ["total", "used", "free"])
    mock_du_low = create_autospec(shutil.disk_usage, return_value=Usage(10_000, 9_000, 1_099))
    ok_low, _ = offsite_sync.check_local_pack_free_space(
        tmp_path, 1_000, multiplier=1.1, disk_usage_fn=mock_du_low
    )
    assert ok_low is False
    mock_du_ok = create_autospec(shutil.disk_usage, return_value=Usage(10_000, 8_000, 1_100))
    ok_enough, _ = offsite_sync.check_local_pack_free_space(
        tmp_path, 1_000, multiplier=1.1, disk_usage_fn=mock_du_ok
    )
    assert ok_enough is True


def test_source_sha256_corruption_and_prebuilt_archive_strict_validation(tmp_path: Path) -> None:
    # 1. Same-length dump corruption is rejected immediately.
    root1 = tmp_path / "corrupt_dump"
    created1 = _create_frozen_set(root1, set_id="20261128-141000")
    dump_buf = bytearray(created1["dump"].read_bytes())
    dump_buf[0] ^= 0xFF
    created1["dump"].write_bytes(bytes(dump_buf))
    with pytest.raises(RuntimeError, match="Dump SHA-256 mismatch"):
        offsite_sync.resolve_frozen_backup_set(
            backup_set_id="20261128-141000",
            backups_root=root1,
            remote="gdrive",
            drive_dir="DaltonBackup",
        )

    # 2. Same-length ASR archive corruption is rejected immediately.
    root2 = tmp_path / "corrupt_asr"
    created2 = _create_frozen_set(root2, set_id="20261128-141100")
    asr_buf = bytearray(created2["asr"].read_bytes())
    asr_buf[-1] ^= 0xFF
    created2["asr"].write_bytes(bytes(asr_buf))
    with pytest.raises(RuntimeError, match="ASR archive SHA-256 mismatch"):
        offsite_sync.resolve_frozen_backup_set(
            backup_set_id="20261128-141100",
            backups_root=root2,
            remote="gdrive",
            drive_dir="DaltonBackup",
        )

    # 3. Wrong dump_sha256 recorded in backup-set JSON is rejected.
    root3 = tmp_path / "bad_meta_sha"
    created3 = _create_frozen_set(root3, set_id="20261128-141200")
    sdata = json.loads(created3["set"].read_text(encoding="utf-8"))
    sdata["dump_sha256"] = "0" * 64
    created3["set"].write_text(json.dumps(sdata), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Dump SHA-256 mismatch"):
        offsite_sync.resolve_frozen_backup_set(
            backup_set_id="20261128-141200",
            backups_root=root3,
            remote="gdrive",
            drive_dir="DaltonBackup",
        )

    # 4. Plain JSON file masquerading as prebuilt objects archive is rejected.
    root4 = tmp_path / "json_as_tar"
    _create_frozen_set(
        root4,
        set_id="20261128-141300",
        mirror_objects={"a/b.wav": b"wave"},
        prebuilt_raw_bytes=b'{"not": "a tar archive"}\n',
    )
    with pytest.raises(RuntimeError, match="Invalid prebuilt objects archive"):
        offsite_sync.resolve_frozen_backup_set(
            backup_set_id="20261128-141300",
            backups_root=root4,
            remote="gdrive",
            drive_dir="DaltonBackup",
        )

    # 5. Prebuilt tar belonging to a different backup set is rejected.
    root5 = tmp_path / "wrong_set_tar"
    objs5 = {"a/b.wav": b"wave-data-5"}
    wrong_set_tar = _build_valid_objects_tar_bytes(
        objs5, layout="bucket", backup_set_id="20261128-999999"
    )
    _create_frozen_set(
        root5,
        set_id="20261128-141400",
        prebuilt_objects=objs5,
        prebuilt_raw_bytes=wrong_set_tar,
    )
    with pytest.raises(RuntimeError, match="belongs to another backup set"):
        offsite_sync.resolve_frozen_backup_set(
            backup_set_id="20261128-141400",
            backups_root=root5,
            remote="gdrive",
            drive_dir="DaltonBackup",
        )

    # 6. Prebuilt tar missing a manifest object is rejected.
    root6 = tmp_path / "missing_obj_tar"
    objs6 = {"a/one.wav": b"one-bytes", "a/two.wav": b"two-bytes"}
    incomplete_tar = _build_valid_objects_tar_bytes(
        {"a/one.wav": b"one-bytes"}, layout="sha", backup_set_id="20261128-141500"
    )
    _create_frozen_set(
        root6,
        set_id="20261128-141500",
        prebuilt_objects=objs6,
        prebuilt_raw_bytes=incomplete_tar,
    )
    with pytest.raises(RuntimeError, match="member set does not match manifest"):
        offsite_sync.resolve_frozen_backup_set(
            backup_set_id="20261128-141500",
            backups_root=root6,
            remote="gdrive",
            drive_dir="DaltonBackup",
        )

    # 7. Prebuilt tar in bucket/key layout with valid members is accepted.
    root7 = tmp_path / "valid_bucket_tar"
    _create_frozen_set(
        root7,
        set_id="20261128-141600",
        prebuilt_objects={"uploads/ws1/doc.pdf": b"pdf-content-7"},
        prebuilt_layout="bucket",
    )
    plan7 = offsite_sync.resolve_frozen_backup_set(
        backup_set_id="20261128-141600",
        backups_root=root7,
        remote="gdrive",
        drive_dir="DaltonBackup",
    )
    assert plan7.artifacts[-1].key == "minio_objects"
    assert plan7.artifacts[-1].kind == "file"
    assert len(plan7.artifacts[-1].source_sha256) == 64

    # 8. Manifest entry missing archived_sha256 is rejected (not silently dropped) in BOTH plan and pack.
    root8 = tmp_path / "missing_sha_manifest"
    created8 = _create_frozen_set(
        root8,
        set_id="20261128-141700",
        mirror_objects={"uploads/ws1/a.bin": b"hello"},
    )
    mdata8 = json.loads(created8["manifest"].read_text(encoding="utf-8"))
    mdata8["objects"].append({"bucket": "dalton-asr", "key": "uploads/ws1/bad.bin", "size": 5})
    created8["manifest"].write_text(json.dumps(mdata8), encoding="utf-8")
    sdata8 = json.loads(created8["set"].read_text(encoding="utf-8"))
    sdata8["manifest_sha256"] = _sha256_bytes(created8["manifest"].read_bytes())
    sdata8["object_count"] = len(mdata8["objects"])
    created8["set"].write_text(json.dumps(sdata8), encoding="utf-8")

    with pytest.raises(ValueError, match="archived_sha256"):
        offsite_sync.resolve_frozen_backup_set(
            backup_set_id="20261128-141700",
            backups_root=root8,
            remote="gdrive",
            drive_dir="DaltonBackup",
        )
    rc_pack = offsite_sync.main(
        [
            "pack-frozen-objects",
            "--manifest",
            str(created8["manifest"]),
            "--expected-set-id",
            "20261128-141700",
            "--mirror-objects-dir",
            str(root8 / "minio-mirror" / "objects"),
            "--out-tar",
            str(tmp_path / "should_not_exist.tar.gz"),
        ]
    )
    assert rc_pack != 0
    assert not (tmp_path / "should_not_exist.tar.gz").exists()

    # 9. Manifest belonging to another backup_set_id is rejected.
    root9 = tmp_path / "wrong_manifest_set"
    created9 = _create_frozen_set(root9, set_id="20261128-141800")
    mdata9 = json.loads(created9["manifest"].read_text(encoding="utf-8"))
    mdata9["backup_set_id"] = "20261128-000000"
    created9["manifest"].write_text(json.dumps(mdata9), encoding="utf-8")
    sdata9 = json.loads(created9["set"].read_text(encoding="utf-8"))
    sdata9["manifest_sha256"] = _sha256_bytes(created9["manifest"].read_bytes())
    created9["set"].write_text(json.dumps(sdata9), encoding="utf-8")
    with pytest.raises(ValueError, match="backup_set_id mismatch"):
        offsite_sync.resolve_frozen_backup_set(
            backup_set_id="20261128-141800",
            backups_root=root9,
            remote="gdrive",
            drive_dir="DaltonBackup",
        )

    # 10. stream_plain_hash_and_forward rejects in-stream tampering; update_frozen_manifest rejects mismatched plain_meta.
    meta_out = tmp_path / "plain_meta.json"
    ok_stream, total_s, sha_s, msg_s = offsite_sync.stream_plain_hash_and_forward(
        io.BytesIO(b"tampered-bytes"),
        io.BytesIO(),
        meta_out,
        expected_size=len(b"tampered-bytes"),
        expected_sha256=_sha256_bytes(b"original-bytes"),
    )
    assert ok_stream is False
    assert total_s == len(b"tampered-bytes")
    assert sha_s == _sha256_bytes(b"tampered-bytes")
    assert "SHA-256 mismatch" in msg_s


def test_frozen_upload_dry_run_default_no_encrypt_no_rclone(tmp_path: Path) -> None:
    key_secret = "DRYRUN_SECRET_KEY_VALUE_12345"
    service_token = "DRYRUN_SERVICE_TOKEN_VALUE_67890"
    env_ctx = _setup_offsite_env(
        tmp_path, key_secret=key_secret, service_token=service_token
    )
    set_id = "20261128-150000"
    _create_frozen_set(
        env_ctx["backups_root"],
        set_id=set_id,
        mirror_objects={"transcriptions/ws1/job1/source/clip.m4a": b"m4a-data"},
    )

    sub_env = {
        "PATH": f"{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(env_ctx["home_dir"]),
        "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
        "DALTON_REPO_PYTHON": sys.executable,
    }
    res = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id],
        env=sub_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    expected_prefix = f"gdrive:DaltonBackup/frozen/manual-pre-blob-inline-{set_id}/"
    assert "DRY-RUN" in res.stdout
    for fname in (
        f"dalton-pg-manual-pre-blob-inline-{set_id}.dump.enc",
        f"asr-jobs-manual-pre-blob-inline-{set_id}.tar.gz.enc",
        f"minio-manifest-{set_id}.json.enc",
        f"backup-set-{set_id}.json.enc",
        f"minio-objects-manual-pre-blob-inline-{set_id}.tar.gz.enc",
    ):
        assert expected_prefix + fname in res.stdout

    # Dry-run invokes neither openssl nor rclone and writes nothing to Drive.
    assert not env_ctx["enc_calls_log"].exists()
    assert not env_ctx["rcat_sizes_log"].exists()
    assert not (env_ctx["drive_store"] / "DaltonBackup" / "frozen").exists()
    _assert_no_secrets(
        (key_secret, service_token),
        stdout=res.stdout,
        stderr=res.stderr,
        files=(env_ctx["log_file"], env_ctx["alerts_file"]),
    )


def test_frozen_upload_apply_encrypt_verify_decrypt_and_rerun_skip(tmp_path: Path) -> None:
    key_secret = "APPLY_SECRET_PASSPHRASE_AAA111"
    service_token = "APPLY_INTERNAL_TOKEN_BBB222"
    env_ctx = _setup_offsite_env(
        tmp_path, key_secret=key_secret, service_token=service_token
    )
    set_id = "20261128-160000"
    mirror_payload = b"archived-asr-source-audio-payload-" * 20
    created = _create_frozen_set(
        env_ctx["backups_root"],
        set_id=set_id,
        mirror_objects={"transcriptions/ws1/j1/source/call.wav": mirror_payload},
    )

    sub_env = {
        "PATH": f"{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(env_ctx["home_dir"]),
        "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
        "DALTON_REPO_PYTHON": str(env_ctx["mounted_py"]),
        "http_proxy": "http://127.0.0.1:7890",
        "https_proxy": "http://127.0.0.1:7890",
        "ALL_PROXY": "socks5://127.0.0.1:7891",
    }

    # Run 1: --apply encrypts, uploads, verifies staged + final paths, publishes encrypted MANIFEST.json.enc, prints ok.
    res1 = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env=sub_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res1.returncode == 0, res1.stderr
    assert res1.stdout.strip() == "ok"
    assert not env_ctx["proxy_check_file"].exists()

    # Temp directory and plaintext object archive must be completely cleaned up.
    tmp_root = env_ctx["backups_root"] / ".tmp"
    assert list(tmp_root.glob("frozen-run-*")) == []

    remote_dir = (
        env_ctx["drive_store"]
        / "DaltonBackup"
        / "frozen"
        / f"manual-pre-blob-inline-{set_id}"
    )
    assert remote_dir.is_dir()
    assert list(remote_dir.glob("*.staged-*")) == []
    # Plaintext MANIFEST.json must NEVER exist on remote; only encrypted MANIFEST.json.enc.
    assert not (remote_dir / "MANIFEST.json").exists()
    manifest_enc_remote = remote_dir / "MANIFEST.json.enc"
    assert manifest_enc_remote.is_file()
    raw_manifest_enc = manifest_enc_remote.read_bytes()
    assert set_id.encode("utf-8") not in raw_manifest_enc
    assert _sha256_bytes(created["dump"].read_bytes()).encode("utf-8") not in raw_manifest_enc

    # Decrypt every static artifact back and assert exact byte identity.
    for key_name, src_path in (
        ("dump", created["dump"]),
        ("asr", created["asr"]),
        ("manifest", created["manifest"]),
        ("set", created["set"]),
    ):
        enc_file = remote_dir / f"{src_path.name}.enc"
        assert enc_file.is_file(), f"missing uploaded {key_name}"
        assert _decrypt_file(enc_file, env_ctx["key_file"]) == src_path.read_bytes()

    # Decrypt mirror-packed objects archive and verify archived object bytes inside.
    obj_enc = remote_dir / f"minio-objects-manual-pre-blob-inline-{set_id}.tar.gz.enc"
    assert obj_enc.is_file()
    dec_obj_bytes = _decrypt_file(obj_enc, env_ctx["key_file"])
    dec_tar = tmp_path / "decoded_objects.tar.gz"
    dec_tar.write_bytes(dec_obj_bytes)
    expected_sha = _sha256_bytes(mirror_payload)
    with tarfile.open(dec_tar, "r:gz") as tf:
        extracted = tf.extractfile(f"objects/{expected_sha[:2]}/{expected_sha}")
        assert extracted is not None
        assert extracted.read() == mirror_payload

    # Decrypt MANIFEST.json.enc and verify key_fingerprint + actual streamed source_sha256 for every artifact.
    manifest_json = json.loads(_decrypt_file(manifest_enc_remote, env_ctx["key_file"]).decode("utf-8"))
    expected_fp = backup_lib.key_fingerprint(env_ctx["key_file"])
    assert manifest_json["version"] == 2
    assert manifest_json["key_fingerprint"] == expected_fp
    assert set(manifest_json["artifacts"].keys()) == {
        "dump",
        "asr_jobs",
        "minio_manifest",
        "backup_set",
        "minio_objects",
    }
    assert manifest_json["artifacts"]["dump"]["source_sha256"] == _sha256_bytes(created["dump"].read_bytes())
    assert manifest_json["artifacts"]["minio_objects"]["source_sha256"] == _sha256_bytes(dec_obj_bytes)
    for art_entry in manifest_json["artifacts"].values():
        assert art_entry["key_fingerprint"] == expected_fp

    log_text_run1 = env_ctx["log_file"].read_text(encoding="utf-8")
    assert f"file={created['dump'].name}.enc" in log_text_run1
    assert "result=uploaded" in log_text_run1
    assert "uploaded=5 skipped=0 result=ok" in log_text_run1
    _assert_no_secrets(
        (key_secret, service_token),
        stdout=res1.stdout,
        stderr=res1.stderr,
        files=(env_ctx["log_file"], env_ctx["alerts_file"]),
    )

    # Run 2: Re-running same set with --apply decrypts MANIFEST.json.enc and skips all 5 files (0 encrypt, 0 rcat).
    env_ctx["enc_calls_log"].unlink(missing_ok=True)
    env_ctx["rcat_sizes_log"].unlink(missing_ok=True)
    res2 = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), "--apply", "--set-id", set_id],
        env=sub_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res2.returncode == 0, res2.stderr
    assert res2.stdout.strip() == "ok"
    assert env_ctx["enc_calls_log"].read_text(encoding="utf-8").splitlines() == ["decrypt"]
    assert not env_ctx["rcat_sizes_log"].exists()
    log_text_run2 = env_ctx["log_file"].read_text(encoding="utf-8")
    assert "result=skipped" in log_text_run2
    assert "uploaded=0 skipped=5 result=ok" in log_text_run2

    # Run 3: Corrupt one remote file -> rerun detects mismatch and re-uploads ONLY that file (+ MANIFEST.json.enc).
    env_ctx["rcat_sizes_log"].unlink(missing_ok=True)
    asr_enc = remote_dir / f"{created['asr'].name}.enc"
    corrupted = bytearray(asr_enc.read_bytes())
    corrupted[-1] ^= 0xFF
    asr_enc.write_bytes(bytes(corrupted))

    res3 = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env=sub_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res3.returncode == 0, res3.stderr
    assert res3.stdout.strip() == "ok"
    uploaded_run3 = _read_rcat_uploaded_names(env_ctx["rcat_sizes_log"])
    assert uploaded_run3 == [f"{created['asr'].name}.enc", "MANIFEST.json.enc"]
    assert "uploaded=1 skipped=4 result=ok" in env_ctx["log_file"].read_text(encoding="utf-8")
    assert _decrypt_file(asr_enc, env_ctx["key_file"]) == created["asr"].read_bytes()


def test_frozen_upload_verify_failures_cleanup_partial_resume_and_prebuilt_objects(
    tmp_path: Path,
) -> None:
    key_secret = "FAIL_SECRET_PASSPHRASE_CCC333"
    service_token = "FAIL_INTERNAL_TOKEN_DDD444"
    env_ctx = _setup_offsite_env(
        tmp_path, key_secret=key_secret, service_token=service_token
    )
    set_id = "20261128-170000"
    prebuilt_objs = {"uploads/ws2/memo.bin": b"PREBUILT-MINIO-OBJECT-BYTES-" * 16}
    created = _create_frozen_set(
        env_ctx["backups_root"],
        set_id=set_id,
        prebuilt_objects=prebuilt_objs,
        prebuilt_layout="sha",
    )
    prebuilt_bytes = created["prebuilt_objects"].read_bytes()
    remote_dir = (
        env_ctx["drive_store"]
        / "DaltonBackup"
        / "frozen"
        / f"manual-pre-blob-inline-{set_id}"
    )

    # Case A: Unmounted external drive refuses apply without touching Drive or /tmp.
    res_unmounted = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env={
            "PATH": f"{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(env_ctx["home_dir"]),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
            "DALTON_REPO_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_unmounted.returncode != 0
    assert res_unmounted.stdout.strip() == ""
    assert "EXTERNAL_DRIVE_UNMOUNTED" in env_ctx["alerts_file"].read_text(encoding="utf-8")
    assert not env_ctx["rcat_sizes_log"].exists()

    base_env = {
        "PATH": f"{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(env_ctx["home_dir"]),
        "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
        "DALTON_REPO_PYTHON": str(env_ctx["mounted_py"]),
    }

    # Case B: Remote hash mismatch fails non-zero, cleans staged object, leaves no final .enc.
    res_bad_hash = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env={**base_env, "FAKE_RCLONE_BAD_HASH": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_bad_hash.returncode != 0
    assert res_bad_hash.stdout.strip() == ""
    assert "FROZEN_HASH_MISMATCH" in env_ctx["alerts_file"].read_text(encoding="utf-8")
    if remote_dir.exists():
        assert list(remote_dir.glob("*.staged-*")) == []
        assert list(remote_dir.glob("*.enc")) == []

    # Case B2: Drive free space < 2x remaining upload size fails with DRIVE_LOW_SPACE.
    env_ctx["about_file"].write_text('{"free": 64}\n', encoding="utf-8")
    res_low_space = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_low_space.returncode != 0
    assert res_low_space.stdout.strip() == ""
    assert "DRIVE_LOW_SPACE" in env_ctx["alerts_file"].read_text(encoding="utf-8")
    env_ctx["about_file"].write_text('{"free": 1000000000}\n', encoding="utf-8")

    # Case B3: Remote size mismatch during lsjson verification fails with FROZEN_SIZE_MISMATCH and cleans staged.
    size_mismatch_bin = tmp_path / "size_mismatch_bin"
    size_mismatch_bin.mkdir()
    real_fake_rclone = env_ctx["fake_bin"] / "rclone"
    (size_mismatch_bin / "rclone").write_text(
        f"""#!/usr/bin/env python3
import json, subprocess, sys
proc = subprocess.run([{str(real_fake_rclone)!r}, *sys.argv[1:]], capture_output=True)
if len(sys.argv) > 1 and sys.argv[1] == "lsjson" and proc.returncode == 0:
    items = json.loads(proc.stdout.decode("utf-8"))
    for it in items:
        it["Size"] = max(1, int(it.get("Size", 10)) - 1)
    sys.stdout.write(json.dumps(items))
    sys.exit(0)
sys.stdout.buffer.write(proc.stdout)
sys.stderr.buffer.write(proc.stderr)
sys.exit(proc.returncode)
""",
        encoding="utf-8",
    )
    (size_mismatch_bin / "rclone").chmod(0o755)
    res_bad_size = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env={**base_env, "PATH": f"{size_mismatch_bin}:{base_env['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_bad_size.returncode != 0
    assert res_bad_size.stdout.strip() == ""
    assert "FROZEN_SIZE_MISMATCH" in env_ctx["alerts_file"].read_text(encoding="utf-8")
    if remote_dir.exists():
        assert list(remote_dir.glob("*.staged-*")) == []
        assert list(remote_dir.glob("*.enc")) == []

    # Case C: Mid-stream rcat failure on asr-jobs after dump succeeds -> dump + encrypted MANIFEST.json.enc preserved,
    # asr-jobs staged file cleaned up; rerun skips dump and finishes remaining files!
    env_ctx["rcat_sizes_log"].unlink(missing_ok=True)
    res_partial = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env={**base_env, "FAKE_RCLONE_FAIL_MATCH": "asr-jobs-"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_partial.returncode != 0
    assert res_partial.stdout.strip() == ""
    assert (remote_dir / f"{created['dump'].name}.enc").is_file()
    assert (remote_dir / "MANIFEST.json.enc").is_file()
    assert not (remote_dir / "MANIFEST.json").exists()
    assert not (remote_dir / f"{created['asr'].name}.enc").exists()
    assert list(remote_dir.glob("*.staged-*")) == []

    # Resume with --apply: dump MUST be skipped; remaining 4 files (including prebuilt objects tar) upload.
    env_ctx["rcat_sizes_log"].unlink(missing_ok=True)
    res_resume = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_resume.returncode == 0, res_resume.stderr
    assert res_resume.stdout.strip() == "ok"
    uploaded_resume = _read_rcat_uploaded_names(env_ctx["rcat_sizes_log"])
    assert f"{created['dump'].name}.enc" not in uploaded_resume
    assert {n for n in uploaded_resume if n != "MANIFEST.json.enc"} == {
        f"{created['asr'].name}.enc",
        f"{created['manifest'].name}.enc",
        f"{created['set'].name}.enc",
        f"{created['prebuilt_objects'].name}.enc",
    }
    assert "uploaded=4 skipped=1 result=ok" in env_ctx["log_file"].read_text(encoding="utf-8")

    # Verify prebuilt object archive decrypts cleanly.
    prebuilt_enc = remote_dir / f"{created['prebuilt_objects'].name}.enc"
    assert _decrypt_file(prebuilt_enc, env_ctx["key_file"]) == prebuilt_bytes

    _assert_no_secrets(
        (key_secret, service_token),
        stdout=res_bad_hash.stdout + res_partial.stdout + res_resume.stdout,
        stderr=res_bad_hash.stderr + res_partial.stderr + res_resume.stderr,
        files=(env_ctx["log_file"], env_ctx["alerts_file"]),
    )


def test_missing_key_refuses_generation_and_rotated_key_stops_resume(tmp_path: Path) -> None:
    env_ctx = _setup_offsite_env(tmp_path, key_secret="KEY_ALPHA_111")
    set_id = "20261128-180000"
    created = _create_frozen_set(env_ctx["backups_root"], set_id=set_id)
    base_env = {
        "PATH": f"{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(env_ctx["home_dir"]),
        "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
        "DALTON_REPO_PYTHON": str(env_ctx["mounted_py"]),
    }

    # Case 1: Missing backup key fails hard and NEVER auto-generates a new key file.
    env_ctx["key_file"].unlink()
    res_missing = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_missing.returncode != 0
    assert res_missing.stdout.strip() == ""
    assert not env_ctx["key_file"].exists()
    assert "MISSING_KEY_FILE" in env_ctx["alerts_file"].read_text(encoding="utf-8")

    # Restore Key A and perform partial upload (dump succeeds, asr fails).
    env_ctx["key_file"].write_text("KEY_ALPHA_111\n", encoding="utf-8")
    env_ctx["key_file"].chmod(0o600)
    res_partial = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env={**base_env, "FAKE_RCLONE_FAIL_MATCH": "asr-jobs-"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_partial.returncode != 0

    remote_dir = (
        env_ctx["drive_store"]
        / "DaltonBackup"
        / "frozen"
        / f"manual-pre-blob-inline-{set_id}"
    )
    assert (remote_dir / f"{created['dump'].name}.enc").is_file()
    assert (remote_dir / "MANIFEST.json.enc").is_file()

    # Case 2: Rotate key to Key B -> resume MUST stop immediately with FROZEN_MANIFEST_KEY_MISMATCH
    # and MUST NOT skip dump or upload any remaining files under Key B.
    env_ctx["key_file"].write_text("KEY_BETA_222\n", encoding="utf-8")
    env_ctx["key_file"].chmod(0o600)
    env_ctx["rcat_sizes_log"].unlink(missing_ok=True)
    res_rotated = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_rotated.returncode != 0
    assert res_rotated.stdout.strip() == ""
    assert "FROZEN_MANIFEST_KEY_MISMATCH" in env_ctx["alerts_file"].read_text(encoding="utf-8")
    assert not env_ctx["rcat_sizes_log"].exists()
    assert not (remote_dir / f"{created['asr'].name}.enc").exists()


def test_post_moveto_final_path_verification_defeats_size_only_skip(tmp_path: Path) -> None:
    env_ctx = _setup_offsite_env(tmp_path)
    set_id = "20261128-181000"
    created = _create_frozen_set(env_ctx["backups_root"], set_id=set_id)
    remote_dir = (
        env_ctx["drive_store"]
        / "DaltonBackup"
        / "frozen"
        / f"manual-pre-blob-inline-{set_id}"
    )
    remote_dir.mkdir(parents=True, exist_ok=True)

    # Pre-seed a same-size corrupted dump.enc at the final path.
    dump_enc_size = backup_lib.expected_openssl_enc_size(created["dump"].stat().st_size)
    corrupt_final = remote_dir / f"{created['dump'].name}.enc"
    corrupt_final.write_bytes(b"Z" * dump_enc_size)

    # Wrap fake rclone so moveto records flags/env and simulates a silent --size-only skip (leaves corrupt_final intact, exits 0).
    moveto_log = tmp_path / "moveto_calls.jsonl"
    wrap_bin = tmp_path / "size_only_wrap_bin"
    wrap_bin.mkdir()
    real_fake_rclone = env_ctx["fake_bin"] / "rclone"
    (wrap_bin / "rclone").write_text(
        f"""#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys

log_path = pathlib.Path({str(moveto_log)!r})
args = sys.argv[1:]
if args and args[0] == "moveto":
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({{
            "args": args,
            "rclone_size_only_env": os.environ.get("RCLONE_SIZE_ONLY"),
        }}) + "\\n")
    # Simulate rclone silently skipping overwrite of same-size destination and returning 0.
    sys.exit(0)
proc = subprocess.run([{str(real_fake_rclone)!r}, *args])
sys.exit(proc.returncode)
""",
        encoding="utf-8",
    )
    (wrap_bin / "rclone").chmod(0o755)

    res = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env={
            "PATH": f"{wrap_bin}:{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(env_ctx["home_dir"]),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
            "DALTON_REPO_PYTHON": str(env_ctx["mounted_py"]),
            "RCLONE_SIZE_ONLY": "true",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0
    assert res.stdout.strip() == ""
    assert "FROZEN_FINAL_VERIFY_FAILED" in env_ctx["alerts_file"].read_text(encoding="utf-8")
    # Confirm moveto explicitly unset RCLONE_SIZE_ONLY and passed --ignore-times.
    calls = [json.loads(ln) for ln in moveto_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(calls) == 1
    assert calls[0]["rclone_size_only_env"] is None
    assert "--ignore-times" in calls[0]["args"]
    # Staged file must be cleaned up and MANIFEST.json.enc must NOT record completion.
    assert list(remote_dir.glob("*.staged-*")) == []
    assert not (remote_dir / "MANIFEST.json.enc").exists()

    # Also verify MANIFEST.json.enc final-path verification catches a skipped moveto on MANIFEST.json.enc.
    manifest_skip_bin = tmp_path / "manifest_skip_bin"
    manifest_skip_bin.mkdir()
    (manifest_skip_bin / "rclone").write_text(
        f"""#!/usr/bin/env python3
import subprocess, sys
args = sys.argv[1:]
if args and args[0] == "moveto" and len(args) >= 3 and args[2].endswith("/MANIFEST.json.enc"):
    # Simulate silent skip leaving MANIFEST.json.enc absent/stale while returning 0.
    sys.exit(0)
proc = subprocess.run([{str(real_fake_rclone)!r}, *args])
sys.exit(proc.returncode)
""",
        encoding="utf-8",
    )
    (manifest_skip_bin / "rclone").chmod(0o755)
    corrupt_final.unlink()
    res_manifest_final = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env={
            "PATH": f"{manifest_skip_bin}:{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(env_ctx["home_dir"]),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
            "DALTON_REPO_PYTHON": str(env_ctx["mounted_py"]),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_manifest_final.returncode != 0
    assert res_manifest_final.stdout.strip() == ""
    assert "FROZEN_MANIFEST_FINAL_VERIFY_FAILED" in env_ctx["alerts_file"].read_text(encoding="utf-8")
    assert list(remote_dir.glob("*.staged-*")) == []


def test_bash_x_tracing_does_not_leak_tokens_on_alert_branch(tmp_path: Path) -> None:
    key_secret = "XTRACE_KEY_SECRET_555666"
    service_token = "XTRACE_SERVICE_TOKEN_777888"
    env_ctx = _setup_offsite_env(
        tmp_path, key_secret=key_secret, service_token=service_token
    )
    set_id = "20261128-182000"
    _create_frozen_set(env_ctx["backups_root"], set_id=set_id)

    # Enable gateway URL and provide a fake curl so _common.sh's emit_alert token branch executes.
    with env_ctx["env_file"].open("a", encoding="utf-8") as fh:
        fh.write('DALTON_CHANNEL_GATEWAY_URL="http://127.0.0.1:18962"\n')
    curl_seen = tmp_path / "curl_called.txt"
    fake_curl = env_ctx["fake_bin"] / "curl"
    fake_curl.write_text(
        f"""#!/usr/bin/env python3
import pathlib, sys
_ = sys.stdin.read()
pathlib.Path({str(curl_seen)!r}).write_text("called\\n", encoding="utf-8")
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    # Trigger failure (unmounted drive) while invoked with `bash -x`.
    res = subprocess.run(
        ["bash", "-x", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env={
            "PATH": f"{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(env_ctx["home_dir"]),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
            "DALTON_REPO_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0
    assert curl_seen.is_file()
    _assert_no_secrets(
        (key_secret, service_token),
        stdout=res.stdout,
        stderr=res.stderr,
        files=(env_ctx["log_file"], env_ctx["alerts_file"]),
    )


def test_tmp_cleanup_failure_fails_run_without_ok(tmp_path: Path) -> None:
    env_ctx = _setup_offsite_env(tmp_path)
    set_id = "20261128-183000"
    _create_frozen_set(
        env_ctx["backups_root"],
        set_id=set_id,
        mirror_objects={"uploads/ws3/audio.wav": b"audio-content-cleanup-test" * 10},
    )

    # Wrap `rm` so deleting anything under frozen-run-* fails (simulating read-only external drive).
    real_rm = shutil.which("rm") or "/bin/rm"
    rm_wrap_bin = tmp_path / "rm_fail_bin"
    rm_wrap_bin.mkdir()
    (rm_wrap_bin / "rm").write_text(
        f"""#!/usr/bin/env python3
import os, sys
if any("frozen-run-" in arg for arg in sys.argv[1:]):
    sys.stderr.write("rm: read-only filesystem\\n")
    sys.exit(1)
os.execv({real_rm!r}, [{real_rm!r}, *sys.argv[1:]])
""",
        encoding="utf-8",
    )
    (rm_wrap_bin / "rm").chmod(0o755)

    res = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env={
            "PATH": f"{rm_wrap_bin}:{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(env_ctx["home_dir"]),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
            "DALTON_REPO_PYTHON": str(env_ctx["mounted_py"]),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0
    assert "ok" not in res.stdout.splitlines()
    assert "FROZEN_TMP_CLEANUP_FAILED" in env_ctx["alerts_file"].read_text(encoding="utf-8")


def test_external_drive_low_space_before_object_pack_fails_without_tar(tmp_path: Path) -> None:
    env_ctx = _setup_offsite_env(tmp_path)
    set_id = "20261128-184000"
    _create_frozen_set(
        env_ctx["backups_root"],
        set_id=set_id,
        mirror_objects={"uploads/ws4/big.wav": b"W" * 4096},
    )

    # Wrapper over mounted_py that reports only 100 bytes free on shutil.disk_usage.
    low_space_py = tmp_path / "low_space_python.py"
    low_space_py.write_text(
        f"""#!{sys.executable}
import collections, os, pathlib, runpy, shutil, sys

mount = pathlib.Path({str(env_ctx['ext_mount'])!r}).resolve()
orig_ismount = os.path.ismount
os.path.ismount = lambda p: pathlib.Path(p).resolve() == mount or orig_ismount(p)
Usage = collections.namedtuple("usage", ["total", "used", "free"])
shutil.disk_usage = lambda _p: Usage(100000, 99900, 100)
target = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(target, run_name="__main__")
""",
        encoding="utf-8",
    )
    low_space_py.chmod(0o755)

    res = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env={
            "PATH": f"{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(env_ctx["home_dir"]),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
            "DALTON_REPO_PYTHON": str(low_space_py),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0
    assert res.stdout.strip() == ""
    assert "EXTERNAL_DRIVE_LOW_SPACE" in env_ctx["alerts_file"].read_text(encoding="utf-8")
    remote_dir = (
        env_ctx["drive_store"]
        / "DaltonBackup"
        / "frozen"
        / f"manual-pre-blob-inline-{set_id}"
    )
    assert not (remote_dir / f"minio-objects-manual-pre-blob-inline-{set_id}.tar.gz.enc").exists()


def test_corrupted_dump_same_length_fails_end_to_end(tmp_path: Path) -> None:
    env_ctx = _setup_offsite_env(tmp_path)
    set_id = "20261128-185000"
    created = _create_frozen_set(env_ctx["backups_root"], set_id=set_id)

    # Flip one byte in dump while keeping file size identical.
    raw = bytearray(created["dump"].read_bytes())
    raw[10] ^= 0x01
    created["dump"].write_bytes(bytes(raw))

    res = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env={
            "PATH": f"{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(env_ctx["home_dir"]),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
            "DALTON_REPO_PYTHON": str(env_ctx["mounted_py"]),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0
    assert res.stdout.strip() == ""
    assert "FROZEN_SET_INVALID" in env_ctx["alerts_file"].read_text(encoding="utf-8")
    assert not env_ctx["rcat_sizes_log"].exists()


def test_single_key_snapshot_survives_midrun_key_rotation(tmp_path: Path) -> None:
    """Item 1: Key file is snapshotted once (0600) into RUN_TMP_DIR; mid-run rotation cannot mix keys."""
    key_a = "KEY_SNAPSHOT_ORIGINAL_AAA111"
    key_b = "KEY_SNAPSHOT_ROTATED_BBB222"
    env_ctx = _setup_offsite_env(tmp_path, key_secret=key_a)
    set_id = "20261128-190100"
    created = _create_frozen_set(env_ctx["backups_root"], set_id=set_id)

    orig_key_copy = tmp_path / "key_a.copy"
    orig_key_copy.write_text(key_a + "\n", encoding="utf-8")
    expected_fp_a = backup_lib.key_fingerprint(orig_key_copy)

    # Wrap openssl: on first `enc`, assert `-pass file:` is a 0600 snapshot in frozen-run-*
    # and overwrite the original backup.key with Key B mid-run.
    real_openssl = shutil.which("openssl") or "/usr/bin/openssl"
    pass_log = tmp_path / "openssl_pass_args.jsonl"
    rotate_bin = tmp_path / "rotate_openssl_bin"
    rotate_bin.mkdir()
    (rotate_bin / "openssl").write_text(
        f"""#!/usr/bin/env python3
import json, os, pathlib, stat, sys

args = sys.argv[1:]
key_orig = pathlib.Path({str(env_ctx['key_file'])!r})
pass_log = pathlib.Path({str(pass_log)!r})
if args and args[0] == "enc":
    pass_arg = ""
    for idx, tok in enumerate(args):
        if tok == "-pass" and idx + 1 < len(args):
            pass_arg = args[idx + 1]
    key_used = pass_arg.split("file:", 1)[1] if pass_arg.startswith("file:") else ""
    mode = stat.S_IMODE(os.stat(key_used).st_mode) if key_used and os.path.exists(key_used) else 0
    with pass_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"key_used": key_used, "mode": mode}}) + "\\n")
    # Rotate the original key file on disk while encryption is already in progress.
    key_orig.write_text({(key_b + chr(10))!r}, encoding="utf-8")
    key_orig.chmod(0o600)
os.execv({real_openssl!r}, [{real_openssl!r}, *args])
""",
        encoding="utf-8",
    )
    (rotate_bin / "openssl").chmod(0o755)

    res = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env={
            "PATH": f"{rotate_bin}:{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(env_ctx["home_dir"]),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
            "DALTON_REPO_PYTHON": str(env_ctx["mounted_py"]),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "ok"

    # Confirm original key file really was rotated mid-run.
    assert env_ctx["key_file"].read_text(encoding="utf-8").strip() == key_b

    # Every openssl enc call used the 0600 snapshot inside RUN_TMP_DIR, never the original key path.
    calls = [json.loads(ln) for ln in pass_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(calls) >= 2
    for call in calls:
        assert "frozen-run-" in call["key_used"]
        assert call["key_used"] != str(env_ctx["key_file"])
        assert call["mode"] == 0o600

    # Every uploaded artifact AND MANIFEST.json.enc must decrypt with Key A and record Key A's fingerprint.
    remote_dir = (
        env_ctx["drive_store"]
        / "DaltonBackup"
        / "frozen"
        / f"manual-pre-blob-inline-{set_id}"
    )
    assert _decrypt_file(remote_dir / f"{created['dump'].name}.enc", orig_key_copy) == created["dump"].read_bytes()
    manifest_json = json.loads(
        _decrypt_file(remote_dir / "MANIFEST.json.enc", orig_key_copy).decode("utf-8")
    )
    assert manifest_json["key_fingerprint"] == expected_fp_a
    for entry in manifest_json["artifacts"].values():
        assert entry["key_fingerprint"] == expected_fp_a


def test_manifest_snapshot_used_for_upload_and_pack_and_fingerprint_checked(tmp_path: Path) -> None:
    """Item 2: MinIO manifest is snapshotted once in planning; pack fingerprint is verified before merge."""
    env_ctx = _setup_offsite_env(tmp_path)
    set_id = "20261128-190200"
    orig_payload = b"ORIGINAL-MIRROR-OBJECT-PAYLOAD-" * 16
    extra_payload = b"MUTATED-LATE-OBJECT-PAYLOAD-" * 16
    created = _create_frozen_set(
        env_ctx["backups_root"],
        set_id=set_id,
        mirror_objects={"uploads/ws1/original.wav": orig_payload},
    )
    orig_manifest_bytes = created["manifest"].read_bytes()
    extra_sha = _sha256_bytes(extra_payload)
    extra_mirror = env_ctx["backups_root"] / "minio-mirror" / "objects" / extra_sha[:2] / extra_sha
    extra_mirror.parent.mkdir(parents=True, exist_ok=True)
    extra_mirror.write_bytes(extra_payload)

    mutated_doc = json.loads(orig_manifest_bytes.decode("utf-8"))
    mutated_doc["objects"].append(
        {
            "bucket": "dalton-asr",
            "key": "uploads/ws1/mutated.wav",
            "version_id": "v1",
            "size": len(extra_payload),
            "archived_sha256": extra_sha,
        }
    )
    mutated_manifest_text = json.dumps(mutated_doc, indent=2, sort_keys=True) + "\n"

    # Mutate the on-disk MinIO manifest right when dump encryption begins (after plan stage).
    real_openssl = shutil.which("openssl") or "/usr/bin/openssl"
    mut_bin = tmp_path / "mutate_manifest_bin"
    mut_bin.mkdir()
    (mut_bin / "openssl").write_text(
        f"""#!/usr/bin/env python3
import os, pathlib, sys

args = sys.argv[1:]
if args and args[0] == "enc":
    pathlib.Path({str(created['manifest'])!r}).write_text({mutated_manifest_text!r}, encoding="utf-8")
os.execv({real_openssl!r}, [{real_openssl!r}, *args])
""",
        encoding="utf-8",
    )
    (mut_bin / "openssl").chmod(0o755)

    res = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env={
            "PATH": f"{mut_bin}:{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(env_ctx["home_dir"]),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
            "DALTON_REPO_PYTHON": str(env_ctx["mounted_py"]),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "ok"

    remote_dir = (
        env_ctx["drive_store"]
        / "DaltonBackup"
        / "frozen"
        / f"manual-pre-blob-inline-{set_id}"
    )
    # Uploaded manifest AND packed object archive must both match the ORIGINAL planned snapshot.
    uploaded_manifest = _decrypt_file(remote_dir / f"{created['manifest'].name}.enc", env_ctx["key_file"])
    assert uploaded_manifest == orig_manifest_bytes

    obj_tar_bytes = _decrypt_file(
        remote_dir / f"minio-objects-manual-pre-blob-inline-{set_id}.tar.gz.enc",
        env_ctx["key_file"],
    )
    dec_tar = tmp_path / "packed_snapshot.tar.gz"
    dec_tar.write_bytes(obj_tar_bytes)
    orig_sha = _sha256_bytes(orig_payload)
    with tarfile.open(dec_tar, "r:gz") as tf:
        names = {m.name for m in tf.getmembers() if m.isreg()}
        assert f"objects/{orig_sha[:2]}/{orig_sha}" in names
        assert f"objects/{extra_sha[:2]}/{extra_sha}" not in names

    # Direct unit check: update_frozen_manifest rejects a mismatched actual_pack_fingerprint.
    created["manifest"].write_bytes(orig_manifest_bytes)
    plan = offsite_sync.resolve_frozen_backup_set(
        backup_set_id=set_id,
        backups_root=env_ctx["backups_root"],
        remote="gdrive",
        drive_dir="DaltonBackup",
    )
    plan_dict = plan.to_dict()
    enc_size = backup_lib.expected_openssl_enc_size(len(obj_tar_bytes))
    with pytest.raises(ValueError, match="Packed object list fingerprint mismatch"):
        offsite_sync.update_frozen_manifest(
            plan=plan_dict,
            existing_manifest=None,
            artifact_key="minio_objects",
            plain_size=len(obj_tar_bytes),
            stream_meta={"size": enc_size, "md5": "d41d8cd98f00b204e9800998ecf8427e"},
            plain_meta={"size": len(obj_tar_bytes), "sha256": _sha256_bytes(obj_tar_bytes)},
            key_fingerprint="0123456789abcdef",
            actual_pack_fingerprint="f" * 64,
        )


def test_prebuilt_archive_without_registered_digest_refused_without_tar_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 3: Prebuilt object archive without pre-registered objects_archive_sha256 is refused immediately."""
    backups_root = tmp_path / "backups"
    set_id = "20261128-190300"
    _create_frozen_set(
        backups_root,
        set_id=set_id,
        prebuilt_objects={"uploads/ws1/file.bin": b"prebuilt-bytes"},
        include_prebuilt_sha=False,
    )

    backups_root_bad = tmp_path / "backups_bad_sha"
    created_bad = _create_frozen_set(
        backups_root_bad,
        set_id="20261128-190301",
        prebuilt_objects={"uploads/ws1/file.bin": b"prebuilt-bytes"},
        include_prebuilt_sha=True,
    )
    sdata = json.loads(created_bad["set"].read_text(encoding="utf-8"))
    sdata["objects_archive_sha256"] = "0" * 64
    created_bad["set"].write_text(json.dumps(sdata), encoding="utf-8")

    spy_tar_open = create_autospec(tarfile.open)
    monkeypatch.setattr(tarfile, "open", spy_tar_open)

    with pytest.raises(ValueError, match="objects_archive_sha256"):
        offsite_sync.resolve_frozen_backup_set(
            backup_set_id=set_id,
            backups_root=backups_root,
            remote="gdrive",
            drive_dir="DaltonBackup",
        )
    spy_tar_open.assert_not_called()

    # With a pre-registered digest that does NOT match on-disk bytes, it is rejected by digest check.
    with pytest.raises(RuntimeError, match="Objects archive SHA-256 mismatch"):
        offsite_sync.resolve_frozen_backup_set(
            backup_set_id="20261128-190301",
            backups_root=backups_root_bad,
            remote="gdrive",
            drive_dir="DaltonBackup",
        )
    spy_tar_open.assert_not_called()


def test_newline_only_key_file_rejected_without_key_generation(tmp_path: Path) -> None:
    """Item 4: Key file whose first line is empty after stripping newlines stops without generating a key."""
    env_ctx = _setup_offsite_env(tmp_path)
    set_id = "20261128-190400"
    _create_frozen_set(env_ctx["backups_root"], set_id=set_id)

    for bad_content in (b"\n", b"\r\n", b"\nsecond-line-ignored-by-openssl\n"):
        env_ctx["key_file"].write_bytes(bad_content)
        env_ctx["key_file"].chmod(0o600)
        env_ctx["alerts_file"].unlink(missing_ok=True)
        res = subprocess.run(
            ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
            env={
                "PATH": f"{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "HOME": str(env_ctx["home_dir"]),
                "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
                "DALTON_REPO_PYTHON": str(env_ctx["mounted_py"]),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode != 0
        assert res.stdout.strip() == ""
        assert "EMPTY_KEY_FILE" in env_ctx["alerts_file"].read_text(encoding="utf-8")
        # Must never overwrite or auto-generate a new key file.
        assert env_ctx["key_file"].read_bytes() == bad_content
        assert not env_ctx["rcat_sizes_log"].exists()


def test_missing_trusted_digests_refused_on_apply_and_annotated_on_dry_run(tmp_path: Path) -> None:
    """Item 5: Legacy ASR or missing manifest_sha256 fails --apply with reason; dry-run annotates 缺可信摘要."""
    env_ctx = _setup_offsite_env(tmp_path)
    base_env = {
        "PATH": f"{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(env_ctx["home_dir"]),
        "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
        "DALTON_REPO_PYTHON": str(env_ctx["mounted_py"]),
    }

    # Case A: Legacy ASR archive on disk without asr_archive_path/asr_archive_sha256 in backup-set JSON.
    set_asr = "20261128-190500"
    created_asr = _create_frozen_set(env_ctx["backups_root"], set_id=set_asr, include_asr=True)
    sdata_asr = json.loads(created_asr["set"].read_text(encoding="utf-8"))
    for k in ("asr_archive_path", "asr_archive_size_bytes", "asr_archive_sha256"):
        sdata_asr.pop(k, None)
    created_asr["set"].write_text(json.dumps(sdata_asr), encoding="utf-8")

    dry_asr = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_asr],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry_asr.returncode == 0, dry_asr.stderr
    assert "DRY-RUN" in dry_asr.stdout
    assert "缺可信摘要" in dry_asr.stdout
    assert f"asr-jobs-manual-pre-blob-inline-{set_asr}.tar.gz" in dry_asr.stdout

    apply_asr = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_asr, "--apply"],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert apply_asr.returncode != 0
    assert apply_asr.stdout.strip() == ""
    assert "asr_archive_sha256" in apply_asr.stderr
    assert not env_ctx["rcat_sizes_log"].exists()

    # Case B: Missing manifest_sha256 in backup-set JSON.
    set_man = "20261128-190501"
    created_man = _create_frozen_set(env_ctx["backups_root"], set_id=set_man, include_asr=False)
    sdata_man = json.loads(created_man["set"].read_text(encoding="utf-8"))
    sdata_man.pop("manifest_sha256", None)
    created_man["set"].write_text(json.dumps(sdata_man), encoding="utf-8")

    dry_man = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_man],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry_man.returncode == 0, dry_man.stderr
    assert "DRY-RUN" in dry_man.stdout
    assert "缺可信摘要" in dry_man.stdout
    assert f"minio-manifest-{set_man}.json" in dry_man.stdout

    apply_man = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_man, "--apply"],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert apply_man.returncode != 0
    assert apply_man.stdout.strip() == ""
    assert "manifest_sha256" in apply_man.stderr
    assert not env_ctx["rcat_sizes_log"].exists()


def test_metadata_backup_set_id_mismatch_rejected(tmp_path: Path) -> None:
    """Item 6: Metadata backup_set_id must equal requested ID or <tag>-<ID> alias."""
    env_ctx = _setup_offsite_env(tmp_path)
    set_a = "20261128-190600"
    set_b = "20261128-190699"
    created_a = _create_frozen_set(env_ctx["backups_root"], set_id=set_a)

    # Copy set A's metadata JSON under set B's filename while set A's backup files still exist.
    misnamed_b = env_ctx["backups_root"] / "sets" / f"backup-set-{set_b}.json"
    misnamed_b.write_bytes(created_a["set"].read_bytes())

    with pytest.raises(ValueError, match="backup_set_id mismatch"):
        offsite_sync.resolve_frozen_backup_set(
            backup_set_id=set_b,
            backups_root=env_ctx["backups_root"],
            remote="gdrive",
            drive_dir="DaltonBackup",
        )

    res = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_b, "--apply"],
        env={
            "PATH": f"{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(env_ctx["home_dir"]),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
            "DALTON_REPO_PYTHON": str(env_ctx["mounted_py"]),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0
    assert res.stdout.strip() == ""
    assert "backup_set_id mismatch" in res.stderr
    assert not env_ctx["rcat_sizes_log"].exists()

    # Canonical ID and <tag>-<ID> alias for set A still resolve cleanly.
    plan_alias = offsite_sync.resolve_frozen_backup_set(
        backup_set_id=f"manual-pre-blob-inline-{set_a}",
        backups_root=env_ctx["backups_root"],
        remote="gdrive",
        drive_dir="DaltonBackup",
    )
    assert plan_alias.backup_set_id == set_a


def test_per_target_lock_blocks_live_pid_and_reclaims_stale_pid(tmp_path: Path) -> None:
    """Item 7: Local mkdir lock per resolved remote target blocks live PID and clears stale PID."""
    env_ctx = _setup_offsite_env(tmp_path)
    set_id = "20261128-190700"
    _create_frozen_set(env_ctx["backups_root"], set_id=set_id)

    remote_dir = f"gdrive:DaltonBackup/frozen/manual-pre-blob-inline-{set_id}"
    lock_key = hashlib.sha256(remote_dir.encode("utf-8")).hexdigest()[:24]
    lock_dir = env_ctx["backups_root"] / ".tmp" / f"frozen-target-{lock_key}.lock"
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")

    base_env = {
        "PATH": f"{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(env_ctx["home_dir"]),
        "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
        "DALTON_REPO_PYTHON": str(env_ctx["mounted_py"]),
    }

    # Live PID lock must block concurrent run without removing holder's lock.
    res_blocked = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_blocked.returncode != 0
    assert res_blocked.stdout.strip() == ""
    assert "FROZEN_TARGET_LOCKED" in env_ctx["alerts_file"].read_text(encoding="utf-8")
    assert lock_dir.is_dir()
    assert (lock_dir / "pid").read_text(encoding="utf-8").strip() == str(os.getpid())
    assert not env_ctx["rcat_sizes_log"].exists()

    # Stale PID (already-exited child) must be cleared, allowing upload to proceed and release lock on exit.
    dead_proc = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid = dead_proc.pid
    dead_proc.wait()
    (lock_dir / "pid").write_text(f"{dead_pid}\n", encoding="utf-8")

    res_ok = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_ok.returncode == 0, res_ok.stderr
    assert res_ok.stdout.strip() == "ok"
    assert not lock_dir.exists()


def test_signal_cleanup_and_rclone_dump_suppressed(tmp_path: Path) -> None:
    """Item 8: INT/TERM/HUP wait for children, run cleanup, exit non-zero; all frozen rclone calls suppress dump."""
    env_ctx = _setup_offsite_env(tmp_path)
    set_id = "20261128-190800"
    _create_frozen_set(env_ctx["backups_root"], set_id=set_id)
    real_fake_rclone = env_ctx["fake_bin"] / "rclone"

    # Part A: RCLONE_DUMP / RCLONE_LOG_LEVEL suppressed across ALL rclone calls including deletefile cleanup.
    dump_audit_log = tmp_path / "rclone_dump_audit.jsonl"
    audit_bin = tmp_path / "dump_audit_bin"
    audit_bin.mkdir()
    (audit_bin / "rclone").write_text(
        f"""#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys

args = sys.argv[1:]
with pathlib.Path({str(dump_audit_log)!r}).open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({{
        "cmd": args[0] if args else "",
        "args": args,
        "RCLONE_DUMP": os.environ.get("RCLONE_DUMP"),
        "RCLONE_DUMP_HEADERS": os.environ.get("RCLONE_DUMP_HEADERS"),
        "RCLONE_DUMP_BODIES": os.environ.get("RCLONE_DUMP_BODIES"),
        "RCLONE_LOG_LEVEL": os.environ.get("RCLONE_LOG_LEVEL"),
    }}) + "\\n")
proc = subprocess.run([{str(real_fake_rclone)!r}, *args])
sys.exit(proc.returncode)
""",
        encoding="utf-8",
    )
    (audit_bin / "rclone").chmod(0o755)

    res_fail = subprocess.run(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env={
            "PATH": f"{audit_bin}:{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(env_ctx["home_dir"]),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
            "DALTON_REPO_PYTHON": str(env_ctx["mounted_py"]),
            "RCLONE_DUMP": "auth",
            "RCLONE_DUMP_HEADERS": "true",
            "RCLONE_DUMP_BODIES": "true",
            "RCLONE_LOG_LEVEL": "DEBUG",
            "FAKE_RCLONE_FAIL_MATCH": ".dump.enc",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_fail.returncode != 0
    audit_rows = [
        json.loads(ln) for ln in dump_audit_log.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    cmds_seen = {row["cmd"] for row in audit_rows}
    assert "rcat" in cmds_seen
    assert "deletefile" in cmds_seen
    for row in audit_rows:
        assert row["RCLONE_DUMP"] is None
        assert row["RCLONE_DUMP_HEADERS"] is None
        assert row["RCLONE_DUMP_BODIES"] is None
        assert row["RCLONE_LOG_LEVEL"] is None
        if row["cmd"] != "listremotes":
            assert "--dump=" in row["args"]
            assert "--log-level" in row["args"]
            lvl_idx = row["args"].index("--log-level")
            assert row["args"][lvl_idx + 1] == "INFO"

    # Part B: SIGTERM mid-upload runs cleanup (removes RUN_TMP_DIR, releases lock, deletes staged remote) and exits non-zero.
    started_marker = tmp_path / "rcat_started.txt"
    sig_bin = tmp_path / "sig_rclone_bin"
    sig_bin.mkdir()
    (sig_bin / "rclone").write_text(
        f"""#!/usr/bin/env python3
import pathlib, subprocess, sys, time

args = sys.argv[1:]
if args and args[0] == "rcat":
    rel = args[1].split(":", 1)[1].lstrip("/")
    dst = pathlib.Path({str(env_ctx['drive_store'])!r}) / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"in-flight-partial")
    pathlib.Path({str(started_marker)!r}).write_text(rel, encoding="utf-8")
    time.sleep(30)
    sys.exit(1)
proc = subprocess.run([{str(real_fake_rclone)!r}, *args])
sys.exit(proc.returncode)
""",
        encoding="utf-8",
    )
    (sig_bin / "rclone").chmod(0o755)

    proc = subprocess.Popen(
        ["bash", str(UPLOAD_SCRIPT), set_id, "--apply"],
        env={
            "PATH": f"{sig_bin}:{env_ctx['fake_bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(env_ctx["home_dir"]),
            "DALTON_OFFSITE_BACKUP_ENV_FILE": str(env_ctx["env_file"]),
            "DALTON_REPO_PYTHON": str(env_ctx["mounted_py"]),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 15.0
        while not started_marker.is_file() and time.monotonic() < deadline:
            assert proc.poll() is None, f"process exited early: {proc.communicate()}"
            time.sleep(0.05)
        assert started_marker.is_file(), "rcat did not start in time"

        tmp_root = env_ctx["backups_root"] / ".tmp"
        assert list(tmp_root.glob("frozen-run-*")) != []
        assert list(tmp_root.glob("frozen-target-*.lock")) != []

        os.killpg(proc.pid, signal.SIGTERM)
        stdout, _stderr = proc.communicate(timeout=15)
        assert proc.returncode != 0
        assert "ok" not in stdout.splitlines()
        assert list(tmp_root.glob("frozen-run-*")) == []
        assert list(tmp_root.glob("frozen-target-*.lock")) == []
        remote_dir = (
            env_ctx["drive_store"]
            / "DaltonBackup"
            / "frozen"
            / f"manual-pre-blob-inline-{set_id}"
        )
        if remote_dir.exists():
            assert list(remote_dir.glob("*.staged-*")) == []
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
