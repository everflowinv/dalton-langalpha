#!/usr/bin/env python3
"""Everflow production offsite weekly baseline + daily delta helper (§3.1 step 4).

Lives under ``scripts/dalton/ops/prod/`` (Everflow-only). Reuses
``src.dalton.consistent_backup`` for version selection and ``scripts.dalton.ops.backup_lib``
for weekly date-directory pruning.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import shutil
import sys
import tarfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.ops import backup_lib
from src.dalton.consistent_backup import (
    ArchivedObjectRecord,
    BucketArchiveSpec,
    TarGzObjectSink,
    VersionSelectionResult,
    deduplicate_selections,
    detect_bucket_versioning,
    list_all_current_objects,
    list_all_object_versions,
    select_bucket_object_versions,
)


def build_weekly_minio_baseline(
    *,
    bucket_specs: Sequence[BucketArchiveSpec],
    baseline_id: str,
    out_tar_path: Path,
    manifest_out_path: Path | None = None,
    tmp_dir: Path | None = None,
) -> tuple[tuple[ArchivedObjectRecord, ...], dict[str, Any]]:
    """Archive current normal object versions across active buckets into a weekly baseline tar.gz."""
    sink = TarGzObjectSink(out_tar_path, tmp_dir=tmp_dir)
    try:
        selections: list[tuple[BucketArchiveSpec, VersionSelectionResult]] = []
        for spec in bucket_specs:
            ver_enabled = (
                spec.versioning_enabled
                if spec.versioning_enabled is not None
                else detect_bucket_versioning(spec.s3_client, spec.bucket)
            )
            if ver_enabled:
                vers, dms = list_all_object_versions(
                    spec.s3_client, spec.bucket, prefix=spec.key_prefix
                )
            else:
                vers = list_all_current_objects(
                    spec.s3_client, spec.bucket, prefix=spec.key_prefix
                )
                dms = []
            sel = select_bucket_object_versions(
                bucket=spec.bucket,
                versions=vers,
                delete_markers=dms,
                snapshot_referenced_keys=(),
                versioning_enabled=ver_enabled,
            )
            selections.append((spec, sel))
        for spec, sel in deduplicate_selections(selections):
            for item in sel.selected:
                sink.archive_object(spec.s3_client, item)
        records = sink.finalize()
    except Exception:
        sink.abort()
        raise

    manifest_payload: dict[str, Any] = {
        "version": 2,
        "baseline_id": baseline_id,
        "object_count": len(records),
        "objects": [r.to_dict() for r in records],
    }
    if manifest_out_path is not None:
        manifest_out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = manifest_out_path.with_name(f".{manifest_out_path.name}.tmp.{os.getpid()}")
        tmp.write_text(
            json.dumps(manifest_payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, manifest_out_path)
    return records, manifest_payload


def compute_daily_minio_delta(
    daily_manifest_objects: Sequence[Mapping[str, Any]],
    baseline_manifest_objects: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return deduplicated objects whose ``archived_sha256`` is absent from the weekly baseline."""
    baseline_shas = {
        str(obj.get("archived_sha256") or obj.get("sha256") or "").strip().lower()
        for obj in baseline_manifest_objects
        if isinstance(obj, Mapping)
    }
    baseline_shas.discard("")

    seen_delta_shas: set[str] = set()
    delta: list[dict[str, Any]] = []
    for obj in daily_manifest_objects:
        if not isinstance(obj, Mapping):
            continue
        sha = str(obj.get("archived_sha256") or obj.get("sha256") or "").strip().lower()
        if not sha or sha in baseline_shas or sha in seen_delta_shas:
            continue
        seen_delta_shas.add(sha)
        delta.append(dict(obj))
    delta.sort(key=lambda item: (str(item.get("archived_sha256") or ""), str(item.get("key") or "")))
    return delta


def build_daily_minio_delta_tar(
    delta_entries: Sequence[Mapping[str, Any]],
    *,
    mirror_objects_dir: Path,
    out_tar_path: Path,
) -> tuple[int, str, int]:
    """Pack delta objects (by ``archived_sha256``) from the local mirror pool into ``minio-delta-*.tar.gz``."""
    out_tar_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_tar = out_tar_path.with_name(f".{out_tar_path.name}.tmp.{os.getpid()}")
    packed: list[dict[str, Any]] = []
    try:
        with tarfile.open(tmp_tar, "w:gz") as tf:
            for entry in delta_entries:
                sha = str(entry.get("archived_sha256") or "").strip().lower()
                if not sha:
                    continue
                src = mirror_objects_dir / sha[:2] / sha
                if not src.is_file():
                    raise FileNotFoundError(
                        f"Mirror pool object missing for archived_sha256={sha}: {src}"
                    )
                data = src.read_bytes()
                actual_sha = hashlib.sha256(data).hexdigest()
                if actual_sha != sha:
                    raise RuntimeError(
                        f"Mirror pool object corrupted for {sha}: actual={actual_sha}"
                    )
                arcname = f"objects/{sha[:2]}/{sha}"
                info = tarfile.TarInfo(name=arcname)
                info.size = len(data)
                info.mtime = 0
                info.mode = 0o600
                tf.addfile(info, io.BytesIO(data))
                packed.append(
                    {
                        "archived_sha256": sha,
                        "size": len(data),
                        "bucket": str(entry.get("bucket") or ""),
                        "key": str(entry.get("key") or ""),
                    }
                )
            meta_bytes = (
                json.dumps(
                    {"version": 1, "object_count": len(packed), "objects": packed},
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
            minfo = tarfile.TarInfo(name="_delta_manifest.json")
            minfo.size = len(meta_bytes)
            minfo.mtime = 0
            minfo.mode = 0o600
            tf.addfile(minfo, io.BytesIO(meta_bytes))

        h = hashlib.sha256()
        total = 0
        with tmp_tar.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                h.update(chunk)
        os.replace(tmp_tar, out_tar_path)
        return total, h.hexdigest(), len(packed)
    finally:
        tmp_tar.unlink(missing_ok=True)


_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:$|-)")


def _baseline_date_dir(baseline_id: str) -> str:
    m = _DATE_PREFIX_RE.match(str(baseline_id or "").strip().rstrip("/"))
    return m.group(1) if m else str(baseline_id or "").strip().rstrip("/")


def weekly_baseline_manifest_filename(baseline_id: str) -> str:
    """Return the immutable per-baseline remote checksum manifest filename."""
    clean = str(baseline_id or "").strip().rstrip("/")
    return f"baseline-{clean}.json" if clean else ""


def weekly_baseline_manifest_relpath(baseline_id: str) -> str:
    """Return the immutable remote path ``weekly/<YYYY-MM-DD>/baseline-<baseline_id>.json``."""
    clean = str(baseline_id or "").strip().rstrip("/")
    if not clean:
        return ""
    return f"weekly/{_baseline_date_dir(clean)}/{weekly_baseline_manifest_filename(clean)}"


def select_weekly_baselines_to_prune(
    weekly_dirs: Iterable[str],
    *,
    retain_weeks: int,
    referenced_baseline_ids: Iterable[str] = (),
) -> list[str]:
    """Select expired ``weekly/<YYYY-MM-DD>`` dirs while exempting any ``baseline_id`` referenced by retained daily sets."""
    referenced: set[str] = set()
    for bid in referenced_baseline_ids:
        clean = str(bid or "").strip().rstrip("/")
        if clean:
            referenced.add(clean)
            referenced.add(_baseline_date_dir(clean))
    candidates = backup_lib.select_weekly_dirs_to_prune(weekly_dirs, retain_weeks)
    return [name for name in candidates if name.strip().rstrip("/") not in referenced]


def build_daily_latest_json(
    *,
    run_id: str,
    date_stamp: str,
    backup_set_id: str,
    baseline_id: str,
    artifacts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the authoritative ``db/<YYYY-MM-DD>/LATEST.json`` manifest for offsite restore."""
    return {
        "version": 2,
        "run_id": run_id,
        "date": date_stamp,
        "backup_set_id": backup_set_id,
        "baseline_id": baseline_id,
        "baseline_dir": _baseline_date_dir(baseline_id) if baseline_id else "",
        "baseline_manifest": weekly_baseline_manifest_filename(baseline_id),
        "baseline_manifest_path": weekly_baseline_manifest_relpath(baseline_id),
        "artifacts": {k: dict(v) for k, v in artifacts.items()},
    }


def build_weekly_latest_json(
    *,
    run_id: str,
    date_stamp: str,
    baseline_id: str,
    minio_mode: str,
    artifacts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the authoritative weekly checksum manifest published per ``baseline_id`` and as ``LATEST.json``."""
    return {
        "version": 2,
        "run_id": run_id,
        "date": date_stamp,
        "baseline_id": baseline_id,
        "baseline_manifest": weekly_baseline_manifest_filename(baseline_id),
        "baseline_manifest_path": weekly_baseline_manifest_relpath(baseline_id),
        "minio_mode": minio_mode,
        "artifacts": {k: dict(v) for k, v in artifacts.items()},
    }


def publish_weekly_baseline_pointer(
    *,
    staged_manifest_path: Path,
    sets_dir: Path,
    baseline_id: str,
) -> Path:
    """Atomically promote a remotely verified weekly baseline manifest and flip ``latest-weekly-baseline.json``."""
    data = json.loads(staged_manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("objects"), list):
        raise ValueError(f"Invalid staged weekly baseline manifest: {staged_manifest_path}")
    sets_dir.mkdir(parents=True, exist_ok=True)
    final_manifest = sets_dir / f"weekly-baseline-{baseline_id}.json"
    tmp_manifest = final_manifest.with_name(f".{final_manifest.name}.tmp.{os.getpid()}")
    tmp_manifest.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(tmp_manifest, 0o600)
    os.replace(tmp_manifest, final_manifest)

    pointer_path = sets_dir / "latest-weekly-baseline.json"
    pointer_tmp = pointer_path.with_name(f".{pointer_path.name}.tmp.{os.getpid()}")
    pointer_payload = {
        "version": 2,
        "baseline_id": baseline_id,
        "baseline_dir": _baseline_date_dir(baseline_id),
        "baseline_manifest": weekly_baseline_manifest_filename(baseline_id),
        "baseline_manifest_path": weekly_baseline_manifest_relpath(baseline_id),
        "manifest_path": str(final_manifest),
        "object_count": len(data["objects"]),
        "published_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    pointer_tmp.write_text(
        json.dumps(pointer_payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(pointer_tmp, 0o600)
    os.replace(pointer_tmp, pointer_path)
    return final_manifest


def find_latest_weekly_baseline_manifest(sets_dir: Path) -> tuple[str, list[dict[str, Any]]]:
    """Locate the published local weekly baseline manifest (preferring ``latest-weekly-baseline.json``)."""
    if not sets_dir.is_dir():
        return "", []
    pointer_path = sets_dir / "latest-weekly-baseline.json"
    if pointer_path.is_file():
        try:
            ptr = json.loads(pointer_path.read_text(encoding="utf-8"))
            if isinstance(ptr, dict):
                mpath_str = str(ptr.get("manifest_path") or "").strip()
                bid = str(ptr.get("baseline_id") or "").strip()
                mpath = Path(mpath_str) if mpath_str else (sets_dir / f"weekly-baseline-{bid}.json")
                if mpath.is_file():
                    data = json.loads(mpath.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and isinstance(data.get("objects"), list):
                        return bid or str(data.get("baseline_id") or ""), data["objects"]
        except Exception:
            pass
    candidates = sorted(sets_dir.glob("weekly-baseline-*.json"), reverse=True)
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("objects"), list):
            bid = str(data.get("baseline_id") or path.stem.removeprefix("weekly-baseline-"))
            return bid, data["objects"]
    return "", []


def validate_verified_set_window(
    set_data: Mapping[str, Any],
    *,
    expected_date: str | None = None,
    local_tz: dt.tzinfo | None = None,
) -> tuple[bool, str]:
    """Verify ``reconcile_ok`` and (when ``expected_date`` is given) ``created_at`` falls in that day's window."""
    if not set_data.get("reconcile_ok"):
        return False, "latest-verified-set.json is not reconciled"
    if not expected_date:
        return True, "ok"
    raw_created = str(set_data.get("created_at") or "").strip()
    if not raw_created:
        return False, f"latest-verified-set.json is missing created_at (expected date {expected_date})"
    try:
        parsed = dt.datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
    except ValueError:
        return False, f"latest-verified-set.json has invalid created_at={raw_created!r}"
    tz = local_tz if local_tz is not None else dt.datetime.now().astimezone().tzinfo
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    local_date = parsed.astimezone(tz).strftime("%Y-%m-%d")
    if local_date != expected_date:
        return (
            False,
            f"latest-verified-set.json backup_set_id={set_data.get('backup_set_id')} "
            f"created_at={raw_created} (local_date={local_date}) is outside today's window ({expected_date})",
        )
    return True, "ok"


_FROZEN_SET_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_FP_RE = re.compile(r"^[0-9a-f]{16,64}$")
DEFAULT_FROZEN_TAG = "manual-pre-blob-inline"
FROZEN_MANIFEST_FILENAME = "MANIFEST.json.enc"
DEFAULT_LOCAL_PACK_SPACE_MULTIPLIER = 1.1


@dataclasses.dataclass(frozen=True)
class FrozenArtifactPlan:
    """One artifact belonging to a frozen backup set slated for encrypted Drive upload."""

    key: str
    kind: str  # "file" or "mirror_pack"
    filename: str
    remote_filename: str
    remote_path: str
    plain_size: int
    expected_enc_size: int | None
    source_sha256: str
    local_path: str = ""
    object_count: int = 0
    pack_fingerprint: str = ""
    missing_trusted_digest: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "filename": self.filename,
            "remote_filename": self.remote_filename,
            "remote_path": self.remote_path,
            "plain_size": self.plain_size,
            "expected_enc_size": self.expected_enc_size,
            "source_sha256": self.source_sha256,
            "local_path": self.local_path,
            "object_count": self.object_count,
            "pack_fingerprint": self.pack_fingerprint,
            "missing_trusted_digest": self.missing_trusted_digest,
        }


@dataclasses.dataclass(frozen=True)
class FrozenBackupSetPlan:
    """Resolved upload plan for one verified local frozen backup set."""

    backup_set_id: str
    tag: str
    remote_dir_name: str
    remote_dir: str
    set_file: str
    manifest_file: str
    artifacts: tuple[FrozenArtifactPlan, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "backup_set_id": self.backup_set_id,
            "tag": self.tag,
            "remote_dir_name": self.remote_dir_name,
            "remote_dir": self.remote_dir,
            "target_lock_key": hashlib.sha256(self.remote_dir.encode("utf-8")).hexdigest()[:24],
            "set_file": self.set_file,
            "manifest_file": self.manifest_file,
            "artifacts": [a.to_dict() for a in self.artifacts],
        }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _sha256_stream(stream: Any) -> tuple[int, str]:
    h = hashlib.sha256()
    total = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        h.update(chunk)
    return total, h.hexdigest()


def _mirror_pack_fingerprint(entries: Sequence[Mapping[str, Any]]) -> str:
    canonical = [
        {
            "archived_sha256": str(item.get("archived_sha256") or "").strip().lower(),
            "size": int(item.get("size") or 0),
            "bucket": str(item.get("bucket") or ""),
            "key": str(item.get("key") or ""),
        }
        for item in entries
    ]
    canonical.sort(key=lambda item: (item["archived_sha256"], item["bucket"], item["key"]))
    payload = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _require_nonempty_regular_file(path: Path, label: str) -> int:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"Frozen backup set missing {label}: {path}")
    size = path.stat().st_size
    if size <= 0:
        raise ValueError(f"Frozen backup set {label} is empty: {path}")
    return size


def _require_valid_sha256(raw: Any, label: str) -> str:
    sha = str(raw or "").strip().lower()
    if not _HEX64_RE.fullmatch(sha):
        raise ValueError(f"Invalid or missing SHA-256 for {label}: {raw!r}")
    return sha


def validate_frozen_manifest_objects(
    manifest_data: Mapping[str, Any],
    *,
    expected_set_id: str,
    expected_object_count: int | None = None,
    expected_snapshot_id: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Strictly validate a frozen set's MinIO manifest and every object entry.

    Returns ``(all_entries, deduped_entries_by_sha)``. Never silently skips
    malformed entries or entries missing ``archived_sha256``.
    """
    if not isinstance(manifest_data, Mapping):
        raise ValueError("MinIO manifest must be a JSON object")

    manifest_set_id = str(manifest_data.get("backup_set_id") or "").strip()
    if not manifest_set_id or manifest_set_id != expected_set_id:
        raise ValueError(
            f"MinIO manifest backup_set_id mismatch: got={manifest_set_id!r}, expected={expected_set_id!r}"
        )

    if expected_snapshot_id:
        manifest_snap = str(manifest_data.get("snapshot_id") or "").strip()
        if manifest_snap and manifest_snap != expected_snapshot_id:
            raise ValueError(
                f"MinIO manifest snapshot_id mismatch: got={manifest_snap!r}, expected={expected_snapshot_id!r}"
            )

    raw_objects = manifest_data.get("objects")
    if not isinstance(raw_objects, list):
        raise ValueError("MinIO manifest is missing a valid 'objects' list")

    if expected_object_count is not None:
        if isinstance(expected_object_count, bool) or len(raw_objects) != int(expected_object_count):
            raise ValueError(
                f"MinIO manifest object_count mismatch: manifest={len(raw_objects)}, metadata={expected_object_count}"
            )

    seen_pairs: set[tuple[str, str]] = set()
    sha_sizes: dict[str, int] = {}
    all_entries: list[dict[str, Any]] = []
    deduped_entries: list[dict[str, Any]] = []

    for idx, item in enumerate(raw_objects):
        if not isinstance(item, Mapping):
            raise ValueError(f"MinIO manifest objects[{idx}] is not a JSON object")
        bucket = str(item.get("bucket") or "").strip()
        key = str(item.get("key") or "").strip().lstrip("/")
        if not bucket or not key:
            raise ValueError(f"MinIO manifest objects[{idx}] is missing bucket or key")
        pair = (bucket, key)
        if pair in seen_pairs:
            raise ValueError(f"MinIO manifest contains duplicate object {bucket}/{key}")
        seen_pairs.add(pair)

        sha = _require_valid_sha256(
            item.get("archived_sha256"),
            f"manifest objects[{idx}] ({bucket}/{key}) archived_sha256",
        )
        raw_size = item.get("size")
        if isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size < 0:
            raise ValueError(
                f"MinIO manifest objects[{idx}] ({bucket}/{key}) has invalid size: {raw_size!r}"
            )
        norm = {
            "bucket": bucket,
            "key": key,
            "archived_sha256": sha,
            "size": raw_size,
        }
        if item.get("version_id") is not None:
            norm["version_id"] = str(item.get("version_id") or "")
        all_entries.append(norm)

        if sha in sha_sizes:
            if sha_sizes[sha] != raw_size:
                raise ValueError(
                    f"MinIO manifest conflicting size for archived_sha256={sha}: "
                    f"{sha_sizes[sha]} vs {raw_size}"
                )
        else:
            sha_sizes[sha] = raw_size
            deduped_entries.append(dict(norm))

    deduped_entries.sort(key=lambda e: (e["archived_sha256"], e["bucket"], e["key"]))
    return all_entries, deduped_entries


def _normalize_tar_member_name(raw_name: str) -> str:
    if "\x00" in raw_name or "\\" in raw_name:
        raise RuntimeError(f"Invalid tar member path: {raw_name!r}")
    name = raw_name
    while name.startswith("./"):
        name = name[2:]
    if not name or name.startswith("/"):
        raise RuntimeError(f"Unsafe tar member path: {raw_name!r}")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RuntimeError(f"Unsafe tar member path segment: {raw_name!r}")
    return name


def verify_prebuilt_objects_archive(
    archive_path: Path,
    *,
    manifest_objects: Sequence[Mapping[str, Any]],
    deduped_objects: Sequence[Mapping[str, Any]],
    expected_set_id: str,
    expected_archive_sha256: str = "",
    expected_archive_size: int | None = None,
) -> tuple[int, str]:
    """Verify a prebuilt ``minio-objects-*.tar.gz`` using its pre-registered digest and manifest."""
    if not str(expected_archive_sha256 or "").strip():
        raise ValueError(
            f"Prebuilt objects archive {archive_path.name} is missing a pre-registered "
            f"objects_archive_sha256 digest; refusing unverified prebuilt archive"
        )
    want_sha = _require_valid_sha256(expected_archive_sha256, "objects_archive_sha256")
    archive_size = _require_nonempty_regular_file(archive_path, "objects_archive")
    if expected_archive_size is not None:
        if isinstance(expected_archive_size, bool) or int(expected_archive_size) != archive_size:
            raise RuntimeError(
                f"Objects archive size mismatch for {archive_path.name}: "
                f"on-disk={archive_size}, recorded={expected_archive_size}"
            )
    actual_archive_sha = _sha256_file(archive_path)
    if actual_archive_sha != want_sha:
        raise RuntimeError(
            f"Objects archive SHA-256 mismatch for {archive_path.name}: "
            f"on-disk={actual_archive_sha}, recorded={want_sha}"
        )

    expected_sha_layout = {
        f"objects/{e['archived_sha256'][:2]}/{e['archived_sha256']}": (
            str(e["archived_sha256"]),
            int(e["size"]),
        )
        for e in deduped_objects
    }
    expected_bucket_layout = {
        f"objects/{e['bucket']}/{str(e['key']).lstrip('/')}": (
            str(e["archived_sha256"]),
            int(e["size"]),
        )
        for e in manifest_objects
    }

    allowed_meta = {"_delta_manifest.json", "_minio_manifest.json"}
    try:
        with tarfile.open(archive_path, "r:gz") as tf:
            file_members: dict[str, tarfile.TarInfo] = {}
            for member in tf.getmembers():
                if member.isdir():
                    continue
                if not member.isreg():
                    raise RuntimeError(
                        f"Prebuilt objects archive contains non-regular member: {member.name!r}"
                    )
                norm_name = _normalize_tar_member_name(member.name)
                if norm_name in file_members:
                    raise RuntimeError(
                        f"Prebuilt objects archive contains duplicate member: {norm_name!r}"
                    )
                file_members[norm_name] = member

            object_members: dict[str, tarfile.TarInfo] = {}
            for norm_name, member in file_members.items():
                if norm_name in allowed_meta:
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        raise RuntimeError(f"Cannot read metadata member {norm_name} in {archive_path.name}")
                    try:
                        meta_doc = json.loads(extracted.read().decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                        raise RuntimeError(
                            f"Invalid JSON in archive metadata {norm_name}: {exc}"
                        ) from exc
                    if not isinstance(meta_doc, dict):
                        raise RuntimeError(f"Archive metadata {norm_name} is not a JSON object")
                    meta_set_id = str(meta_doc.get("backup_set_id") or "").strip()
                    if meta_set_id and meta_set_id != expected_set_id:
                        raise RuntimeError(
                            f"Prebuilt objects archive belongs to another backup set: "
                            f"archive={meta_set_id!r}, expected={expected_set_id!r}"
                        )
                elif norm_name.startswith("objects/"):
                    object_members[norm_name] = member
                else:
                    raise RuntimeError(
                        f"Prebuilt objects archive contains unexpected member: {norm_name!r}"
                    )

            found_keys = set(object_members.keys())
            if not manifest_objects:
                if found_keys:
                    raise RuntimeError(
                        f"Prebuilt objects archive has {len(found_keys)} unexpected object(s) "
                        f"for empty manifest"
                    )
                selected_expected: dict[str, tuple[str, int]] = {}
            elif found_keys == set(expected_sha_layout.keys()):
                selected_expected = expected_sha_layout
            elif found_keys == set(expected_bucket_layout.keys()):
                selected_expected = expected_bucket_layout
            else:
                raise RuntimeError(
                    f"Prebuilt objects archive member set does not match manifest for set {expected_set_id} "
                    f"(archive_objects={len(found_keys)}, expected_unique={len(expected_sha_layout)}, "
                    f"expected_total={len(expected_bucket_layout)})"
                )

            for norm_name, (exp_sha, exp_size) in selected_expected.items():
                member = object_members[norm_name]
                if member.size != exp_size:
                    raise RuntimeError(
                        f"Prebuilt objects archive member size mismatch for {norm_name}: "
                        f"archive={member.size}, expected={exp_size}"
                    )
                extracted = tf.extractfile(member)
                if extracted is None:
                    raise RuntimeError(f"Cannot extract archive member {norm_name}")
                got_size, got_sha = _sha256_stream(extracted)
                if got_size != exp_size:
                    raise RuntimeError(
                        f"Prebuilt objects archive extracted size mismatch for {norm_name}: "
                        f"actual={got_size}, expected={exp_size}"
                    )
                if got_sha != exp_sha:
                    raise RuntimeError(
                        f"Prebuilt objects archive member SHA-256 mismatch for {norm_name}: "
                        f"actual={got_sha}, expected={exp_sha}"
                    )
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise RuntimeError(
            f"Invalid prebuilt objects archive {archive_path.name}: {exc}"
        ) from exc

    return archive_size, actual_archive_sha


def build_frozen_objects_tar(
    deduped_entries: Sequence[Mapping[str, Any]],
    *,
    backup_set_id: str,
    mirror_objects_dir: Path,
    out_tar_path: Path,
) -> tuple[int, str, int, str]:
    """Strictly pack validated frozen set objects from the mirror pool into ``minio-objects-*.tar.gz``."""
    out_tar_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_tar = out_tar_path.with_name(f".{out_tar_path.name}.tmp.{os.getpid()}")
    packed: list[dict[str, Any]] = []
    try:
        with tarfile.open(tmp_tar, "w:gz") as tf:
            for idx, entry in enumerate(deduped_entries):
                sha = _require_valid_sha256(
                    entry.get("archived_sha256"),
                    f"frozen pack entry[{idx}] archived_sha256",
                )
                raw_size = entry.get("size")
                if isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size < 0:
                    raise ValueError(f"frozen pack entry[{idx}] invalid size: {raw_size!r}")
                src = mirror_objects_dir / sha[:2] / sha
                if src.is_symlink() or not src.is_file():
                    raise FileNotFoundError(
                        f"Mirror pool object missing for archived_sha256={sha}: {src}"
                    )
                data = src.read_bytes()
                if len(data) != raw_size:
                    raise RuntimeError(
                        f"Mirror pool object size mismatch for {sha}: actual={len(data)}, expected={raw_size}"
                    )
                actual_sha = hashlib.sha256(data).hexdigest()
                if actual_sha != sha:
                    raise RuntimeError(
                        f"Mirror pool object corrupted for {sha}: actual={actual_sha}"
                    )
                arcname = f"objects/{sha[:2]}/{sha}"
                info = tarfile.TarInfo(name=arcname)
                info.size = len(data)
                info.mtime = 0
                info.mode = 0o600
                tf.addfile(info, io.BytesIO(data))
                packed.append(
                    {
                        "archived_sha256": sha,
                        "size": len(data),
                        "bucket": str(entry.get("bucket") or ""),
                        "key": str(entry.get("key") or ""),
                    }
                )
            meta_bytes = (
                json.dumps(
                    {
                        "version": 1,
                        "backup_set_id": backup_set_id,
                        "object_count": len(packed),
                        "objects": packed,
                    },
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
            minfo = tarfile.TarInfo(name="_delta_manifest.json")
            minfo.size = len(meta_bytes)
            minfo.mtime = 0
            minfo.mode = 0o600
            tf.addfile(minfo, io.BytesIO(meta_bytes))

        if len(packed) != len(deduped_entries):
            raise RuntimeError(
                f"Frozen objects pack count mismatch: packed={len(packed)}, expected={len(deduped_entries)}"
            )
        h = hashlib.sha256()
        total = 0
        with tmp_tar.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                h.update(chunk)
        os.chmod(tmp_tar, 0o600)
        os.replace(tmp_tar, out_tar_path)
        actual_pack_fp = _mirror_pack_fingerprint(packed)
        return total, h.hexdigest(), len(packed), actual_pack_fp
    finally:
        tmp_tar.unlink(missing_ok=True)


def check_local_pack_free_space(
    target_dir: Path,
    raw_bytes: int,
    *,
    multiplier: float = DEFAULT_LOCAL_PACK_SPACE_MULTIPLIER,
    disk_usage_fn: Callable[[Path], Any] = shutil.disk_usage,
) -> tuple[bool, str]:
    """Verify ``target_dir`` has at least ``ceil(raw_bytes * multiplier)`` free bytes before packing."""
    if raw_bytes < 0:
        raise ValueError("raw_bytes must be non-negative")
    if multiplier < 1.0:
        raise ValueError("multiplier must be >= 1.0")
    required = math.ceil(raw_bytes * multiplier)
    usage = disk_usage_fn(target_dir)
    free_bytes = int(usage.free)
    if free_bytes < required:
        return (
            False,
            f"Local free space {free_bytes} bytes on {target_dir} is below "
            f"{multiplier:.2f}x raw_bytes ({required} bytes)",
        )
    return (
        True,
        f"Local free space {free_bytes} bytes >= {required} bytes",
    )


def stream_plain_hash_and_forward(
    src_stream: Any,
    dst_stream: Any,
    meta_out: Path,
    *,
    expected_size: int | None = None,
    expected_sha256: str = "",
    chunk_size: int = 1024 * 1024,
) -> tuple[bool, int, str, str]:
    """Forward plaintext bytes from ``src_stream`` to ``dst_stream`` while computing exact size & SHA-256."""
    meta_out.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    total = 0
    while True:
        chunk = src_stream.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        h.update(chunk)
        dst_stream.write(chunk)
    dst_stream.flush()
    sha_hex = h.hexdigest()

    tmp_meta = meta_out.with_name(f".{meta_out.name}.tmp.{os.getpid()}")
    tmp_meta.write_text(
        json.dumps({"size": total, "sha256": sha_hex}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(tmp_meta, 0o600)
    os.replace(tmp_meta, meta_out)

    if expected_size is not None and total != int(expected_size):
        return (
            False,
            total,
            sha_hex,
            f"plaintext stream size mismatch: actual={total}, expected={expected_size}",
        )
    if expected_sha256:
        want = expected_sha256.strip().lower()
        if not _HEX64_RE.fullmatch(want):
            return False, total, sha_hex, f"invalid expected sha256: {expected_sha256!r}"
        if sha_hex != want:
            return (
                False,
                total,
                sha_hex,
                f"plaintext stream SHA-256 mismatch: actual={sha_hex}, expected={want}",
            )
    return True, total, sha_hex, "ok"


def resolve_frozen_backup_set(
    *,
    backup_set_id: str,
    backups_root: Path,
    remote: str,
    drive_dir: str,
    sets_dir: Path | None = None,
    mirror_objects_dir: Path | None = None,
    verify_source_hashes: bool = True,
    require_trusted_digests: bool = True,
    manifest_snapshot_out: Path | None = None,
) -> FrozenBackupSetPlan:
    """Resolve and strictly validate all files belonging to a local frozen backup set."""
    raw_id = str(backup_set_id or "").strip()
    if (
        not raw_id
        or not _FROZEN_SET_ID_RE.fullmatch(raw_id)
        or raw_id in {".", ".."}
        or ".." in raw_id
    ):
        raise ValueError(f"Invalid backup set ID: {backup_set_id!r}")

    eff_sets_dir = sets_dir if sets_dir is not None else (backups_root / "sets")
    eff_mirror_dir = (
        mirror_objects_dir
        if mirror_objects_dir is not None
        else (backups_root / "minio-mirror" / "objects")
    )
    frozen_dir = backups_root / "frozen"

    set_file = eff_sets_dir / f"backup-set-{raw_id}.json"
    if not set_file.is_file() and eff_sets_dir.is_dir():
        for candidate in sorted(eff_sets_dir.glob("backup-set-*.json")):
            if candidate.is_symlink():
                continue
            try:
                cdata = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            if not isinstance(cdata, dict):
                continue
            cid = str(cdata.get("backup_set_id") or "").strip()
            ctag = str(cdata.get("tag") or DEFAULT_FROZEN_TAG).strip() or DEFAULT_FROZEN_TAG
            if cid and f"{ctag}-{cid}" == raw_id:
                set_file = candidate
                break

    if set_file.is_symlink() or not set_file.is_file():
        raise FileNotFoundError(f"Backup set metadata not found for set_id={raw_id}: {set_file}")

    set_data = json.loads(set_file.read_text(encoding="utf-8"))
    if not isinstance(set_data, dict):
        raise ValueError(f"Backup set metadata is not a JSON object: {set_file}")
    if set_data.get("reconcile_ok") is not True:
        raise ValueError(f"Backup set {raw_id} failed reconciliation (reconcile_ok != true)")

    resolved_id = str(set_data.get("backup_set_id") or "").strip()
    if not resolved_id or not _FROZEN_SET_ID_RE.fullmatch(resolved_id) or ".." in resolved_id:
        raise ValueError(f"Invalid or missing backup_set_id in metadata: {resolved_id!r}")

    tag = str(set_data.get("tag") or DEFAULT_FROZEN_TAG).strip() or DEFAULT_FROZEN_TAG
    if not _FROZEN_SET_ID_RE.fullmatch(tag) or ".." in tag:
        raise ValueError(f"Invalid tag in backup set metadata: {tag!r}")

    if raw_id not in {resolved_id, f"{tag}-{resolved_id}"}:
        raise ValueError(
            f"Backup set metadata backup_set_id mismatch: requested={raw_id!r}, "
            f"metadata={resolved_id!r} (tag={tag!r})"
        )

    remote_dir_name = (
        resolved_id if resolved_id.startswith(f"{tag}-") else f"{tag}-{resolved_id}"
    )
    remote_prefix = remote.strip().rstrip(":")
    drive_subdir = drive_dir.strip().strip("/")
    remote_dir = (
        f"{remote_prefix}:{drive_subdir}/frozen/{remote_dir_name}"
        if drive_subdir
        else f"{remote_prefix}:frozen/{remote_dir_name}"
    )

    artifacts: list[FrozenArtifactPlan] = []

    def _add_file_artifact(
        *,
        key: str,
        path: Path,
        plain_size: int,
        source_sha256: str,
        filename: str | None = None,
        missing_trusted_digest: bool = False,
    ) -> None:
        verified_sha = _require_valid_sha256(source_sha256, f"artifact {key}")
        eff_filename = filename or path.name
        rname = f"{eff_filename}.enc"
        artifacts.append(
            FrozenArtifactPlan(
                key=key,
                kind="file",
                filename=eff_filename,
                remote_filename=rname,
                remote_path=f"{remote_dir}/{rname}",
                plain_size=plain_size,
                expected_enc_size=backup_lib.expected_openssl_enc_size(plain_size),
                source_sha256=verified_sha,
                local_path=str(path),
                missing_trusted_digest=missing_trusted_digest,
            )
        )

    # 1. Database dump (required; SHA-256 verified against backup-set metadata)
    dump_raw = str(set_data.get("dump_path") or "").strip()
    if not dump_raw:
        raise ValueError(f"Backup set {resolved_id} is missing dump_path")
    recorded_dump_sha = _require_valid_sha256(set_data.get("dump_sha256"), "dump_sha256")
    dump_path = Path(dump_raw)
    dump_size = _require_nonempty_regular_file(dump_path, "dump_path")
    recorded_dump_size = set_data.get("dump_size_bytes")
    if recorded_dump_size is not None and (
        isinstance(recorded_dump_size, bool) or int(recorded_dump_size) != dump_size
    ):
        raise RuntimeError(
            f"Dump size mismatch for {dump_path.name}: on-disk={dump_size}, recorded={recorded_dump_size}"
        )
    actual_dump_sha = _sha256_file(dump_path) if verify_source_hashes else recorded_dump_sha
    if actual_dump_sha != recorded_dump_sha:
        raise RuntimeError(
            f"Dump SHA-256 mismatch for {dump_path.name}: on-disk={actual_dump_sha}, recorded={recorded_dump_sha}"
        )
    _add_file_artifact(
        key="dump",
        path=dump_path,
        plain_size=dump_size,
        source_sha256=actual_dump_sha,
    )

    # 2. ASR jobs archive (optional; SHA-256 verified against backup-set metadata when present)
    asr_raw = str(set_data.get("asr_archive_path") or "").strip()
    asr_sha_raw = str(set_data.get("asr_archive_sha256") or "").strip()
    asr_size_raw = set_data.get("asr_archive_size_bytes")
    if asr_raw or asr_sha_raw or asr_size_raw is not None:
        if not asr_raw:
            raise ValueError(
                f"Backup set {resolved_id} has ASR digest/size metadata without asr_archive_path"
            )
        recorded_asr_sha = _require_valid_sha256(asr_sha_raw, "asr_archive_sha256")
        asr_path = Path(asr_raw)
        asr_size = _require_nonempty_regular_file(asr_path, "asr_archive_path")
        if asr_size_raw is not None and (
            isinstance(asr_size_raw, bool) or int(asr_size_raw) != asr_size
        ):
            raise RuntimeError(
                f"ASR archive size mismatch for {asr_path.name}: on-disk={asr_size}, recorded={asr_size_raw}"
            )
        actual_asr_sha = _sha256_file(asr_path) if verify_source_hashes else recorded_asr_sha
        if actual_asr_sha != recorded_asr_sha:
            raise RuntimeError(
                f"ASR archive SHA-256 mismatch for {asr_path.name}: on-disk={actual_asr_sha}, recorded={recorded_asr_sha}"
            )
        _add_file_artifact(
            key="asr_jobs",
            path=asr_path,
            plain_size=asr_size,
            source_sha256=actual_asr_sha,
        )
    elif "asr_archive_path" not in set_data:
        fallback_asr = frozen_dir / f"asr-jobs-{tag}-{resolved_id}.tar.gz"
        if fallback_asr.is_file() and not fallback_asr.is_symlink() and fallback_asr.stat().st_size > 0:
            if require_trusted_digests:
                raise ValueError(
                    f"Legacy ASR archive {fallback_asr.name} is missing a trusted "
                    f"pre-registered asr_archive_sha256 digest; verify and record it before --apply"
                )
            _add_file_artifact(
                key="asr_jobs",
                path=fallback_asr,
                plain_size=fallback_asr.stat().st_size,
                source_sha256=_sha256_file(fallback_asr),
                missing_trusted_digest=True,
            )

    # 3. MinIO object manifest (required; read once, validate strictly, optionally snapshot)
    manifest_raw = str(set_data.get("manifest_path") or "").strip()
    manifest_path = (
        Path(manifest_raw) if manifest_raw else (eff_sets_dir / f"minio-manifest-{resolved_id}.json")
    )
    _require_nonempty_regular_file(manifest_path, "manifest_path")
    manifest_bytes = manifest_path.read_bytes()
    manifest_size = len(manifest_bytes)
    if manifest_size <= 0:
        raise ValueError(f"Frozen backup set manifest_path is empty: {manifest_path}")
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    try:
        manifest_data = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Invalid MinIO manifest JSON {manifest_path}: {exc}") from exc
    if not isinstance(manifest_data, dict):
        raise ValueError(f"MinIO manifest is not a JSON object: {manifest_path}")

    manifest_missing_digest = False
    recorded_manifest_sha_raw = str(set_data.get("manifest_sha256") or "").strip()
    if recorded_manifest_sha_raw:
        recorded_manifest_sha = _require_valid_sha256(recorded_manifest_sha_raw, "manifest_sha256")
        if recorded_manifest_sha != manifest_sha:
            raise RuntimeError(
                f"MinIO manifest SHA-256 mismatch for {manifest_path.name}: "
                f"on-disk={manifest_sha}, recorded={recorded_manifest_sha}"
            )
    else:
        if require_trusted_digests:
            raise ValueError(
                f"Backup set {resolved_id} is missing a trusted pre-registered "
                f"manifest_sha256 digest; verify and record it before --apply"
            )
        manifest_missing_digest = True

    all_manifest_objs, deduped_objs = validate_frozen_manifest_objects(
        manifest_data,
        expected_set_id=resolved_id,
        expected_object_count=set_data.get("object_count"),
        expected_snapshot_id=str(set_data.get("snapshot_id") or "").strip(),
    )

    plan_manifest_path = manifest_path
    if manifest_snapshot_out is not None:
        manifest_snapshot_out.parent.mkdir(parents=True, exist_ok=True)
        tmp_snap = manifest_snapshot_out.with_name(f".{manifest_snapshot_out.name}.tmp.{os.getpid()}")
        try:
            tmp_snap.write_bytes(manifest_bytes)
            os.chmod(tmp_snap, 0o600)
            os.replace(tmp_snap, manifest_snapshot_out)
        finally:
            tmp_snap.unlink(missing_ok=True)
        plan_manifest_path = manifest_snapshot_out

    _add_file_artifact(
        key="minio_manifest",
        path=plan_manifest_path,
        filename=manifest_path.name,
        plain_size=manifest_size,
        source_sha256=manifest_sha,
        missing_trusted_digest=manifest_missing_digest,
    )

    # 4. Backup set manifest (required)
    set_size = _require_nonempty_regular_file(set_file, "backup_set")
    _add_file_artifact(
        key="backup_set",
        path=set_file,
        plain_size=set_size,
        source_sha256=_sha256_file(set_file),
    )

    # 5. Object archive (explicit pre-built archive, frozen/ archive, or mirror-pool pack when objects exist)
    explicit_obj_raw = str(
        set_data.get("objects_archive_path") or set_data.get("minio_archive_path") or ""
    ).strip()
    if explicit_obj_raw:
        obj_arch = Path(explicit_obj_raw)
        obj_size, obj_sha = verify_prebuilt_objects_archive(
            obj_arch,
            manifest_objects=all_manifest_objs,
            deduped_objects=deduped_objs,
            expected_set_id=resolved_id,
            expected_archive_sha256=str(
                set_data.get("objects_archive_sha256") or set_data.get("minio_archive_sha256") or ""
            ).strip(),
            expected_archive_size=set_data.get("objects_archive_size_bytes"),
        )
        _add_file_artifact(
            key="minio_objects",
            path=obj_arch,
            plain_size=obj_size,
            source_sha256=obj_sha,
        )
    else:
        prebuilt_candidates = (
            frozen_dir / f"minio-objects-{tag}-{resolved_id}.tar.gz",
            frozen_dir / f"minio-objects-{resolved_id}.tar.gz",
        )
        prebuilt_found = next(
            (p for p in prebuilt_candidates if p.is_file() and not p.is_symlink() and p.stat().st_size > 0),
            None,
        )
        if prebuilt_found is not None:
            obj_size, obj_sha = verify_prebuilt_objects_archive(
                prebuilt_found,
                manifest_objects=all_manifest_objs,
                deduped_objects=deduped_objs,
                expected_set_id=resolved_id,
                expected_archive_sha256=str(
                    set_data.get("objects_archive_sha256") or set_data.get("minio_archive_sha256") or ""
                ).strip(),
                expected_archive_size=set_data.get("objects_archive_size_bytes"),
            )
            _add_file_artifact(
                key="minio_objects",
                path=prebuilt_found,
                plain_size=obj_size,
                source_sha256=obj_sha,
            )
        elif deduped_objs:
            raw_total = 0
            for entry in deduped_objs:
                sha = str(entry["archived_sha256"])
                exp_size = int(entry["size"])
                src = eff_mirror_dir / sha[:2] / sha
                if src.is_symlink() or not src.is_file():
                    raise FileNotFoundError(
                        f"Mirror pool object missing for archived_sha256={sha}: {src}"
                    )
                actual_size = src.stat().st_size
                if actual_size != exp_size:
                    raise RuntimeError(
                        f"Mirror pool object size mismatch for {sha}: "
                        f"on-disk={actual_size}, expected={exp_size}"
                    )
                if verify_source_hashes:
                    actual_sha = _sha256_file(src)
                    if actual_sha != sha:
                        raise RuntimeError(
                            f"Mirror pool object SHA-256 mismatch for {sha}: on-disk={actual_sha}"
                        )
                raw_total += actual_size
            pack_name = f"minio-objects-{tag}-{resolved_id}.tar.gz"
            rname = f"{pack_name}.enc"
            artifacts.append(
                FrozenArtifactPlan(
                    key="minio_objects",
                    kind="mirror_pack",
                    filename=pack_name,
                    remote_filename=rname,
                    remote_path=f"{remote_dir}/{rname}",
                    plain_size=raw_total,
                    expected_enc_size=None,
                    source_sha256="",
                    local_path="",
                    object_count=len(deduped_objs),
                    pack_fingerprint=_mirror_pack_fingerprint(deduped_objs),
                )
            )

    return FrozenBackupSetPlan(
        backup_set_id=resolved_id,
        tag=tag,
        remote_dir_name=remote_dir_name,
        remote_dir=remote_dir,
        set_file=str(set_file),
        manifest_file=str(plan_manifest_path),
        artifacts=tuple(artifacts),
    )


def format_frozen_dry_run_lines(plan: Mapping[str, Any]) -> list[str]:
    """Render concise dry-run plan lines (filenames, sizes, target remote paths)."""
    lines = [
        f"DRY-RUN set={plan['backup_set_id']} tag={plan['tag']} target={plan['remote_dir']}/",
    ]
    missing_digest_count = 0
    for art in plan.get("artifacts") or ():
        digest_note = ""
        if art.get("missing_trusted_digest"):
            digest_note = " [缺可信摘要]"
            missing_digest_count += 1
        if art.get("kind") == "mirror_pack":
            lines.append(
                f"  [DRY-RUN] {art['filename']} "
                f"(objects={art.get('object_count', 0)} raw_bytes={art['plain_size']}) "
                f"-> {art['remote_path']}{digest_note}"
            )
        else:
            lines.append(
                f"  [DRY-RUN] {art['filename']} "
                f"(plain_size={art['plain_size']} enc_size={art['expected_enc_size']}) "
                f"-> {art['remote_path']}{digest_note}"
            )
    if missing_digest_count:
        lines.append(
            f"DRY-RUN notice: {missing_digest_count} file(s) 缺可信摘要 (--apply will refuse until verified digests are recorded)."
        )
    lines.append(
        f"DRY-RUN complete: {len(plan.get('artifacts') or ())} file(s); pass --apply to encrypt and upload."
    )
    return lines


def validate_frozen_manifest(
    manifest: Mapping[str, Any] | None,
    *,
    plan: Mapping[str, Any],
    current_key_fingerprint: str,
) -> tuple[bool, str]:
    """Validate that a decrypted remote frozen manifest belongs to ``plan`` and ``current_key_fingerprint``."""
    want_fp = str(current_key_fingerprint or "").strip().lower()
    if not _KEY_FP_RE.fullmatch(want_fp):
        return False, "invalid current key fingerprint"
    if not isinstance(manifest, Mapping):
        return False, "remote manifest is not a JSON object"
    if str(manifest.get("backup_set_id") or "") != str(plan.get("backup_set_id") or ""):
        return False, "remote manifest backup_set_id mismatch"
    if str(manifest.get("tag") or "") != str(plan.get("tag") or ""):
        return False, "remote manifest tag mismatch"
    if str(manifest.get("remote_dir_name") or "") != str(plan.get("remote_dir_name") or ""):
        return False, "remote manifest remote_dir_name mismatch"

    manifest_fp = str(manifest.get("key_fingerprint") or "").strip().lower()
    if not _KEY_FP_RE.fullmatch(manifest_fp) or manifest_fp != want_fp:
        return False, "remote manifest key_fingerprint mismatch"

    arts = manifest.get("artifacts")
    if not isinstance(arts, Mapping):
        return False, "remote manifest missing artifacts map"
    for art_key, entry in arts.items():
        if not isinstance(entry, Mapping):
            return False, f"remote manifest artifact {art_key!r} is not an object"
        entry_fp = str(entry.get("key_fingerprint") or "").strip().lower()
        if not _KEY_FP_RE.fullmatch(entry_fp) or entry_fp != want_fp:
            return False, f"remote manifest artifact {art_key!r} key_fingerprint mismatch"
    return True, "ok"


def is_frozen_artifact_verified_on_remote(
    *,
    artifact: Mapping[str, Any],
    remote_manifest: Mapping[str, Any] | None,
    lsjson_out: str,
    hashsum_out: str,
    current_key_fingerprint: str,
    expected_backup_set_id: str = "",
) -> tuple[bool, str]:
    """Return ``(True, msg)`` only when remote artifact matches key-bound manifest, size, and MD5."""
    want_fp = str(current_key_fingerprint or "").strip().lower()
    if not _KEY_FP_RE.fullmatch(want_fp):
        return False, "invalid current key fingerprint"
    if not isinstance(remote_manifest, Mapping):
        return False, "remote manifest absent"
    if expected_backup_set_id and str(remote_manifest.get("backup_set_id") or "") != expected_backup_set_id:
        return False, "remote manifest backup_set_id mismatch"
    manifest_fp = str(remote_manifest.get("key_fingerprint") or "").strip().lower()
    if not _KEY_FP_RE.fullmatch(manifest_fp) or manifest_fp != want_fp:
        return False, "remote manifest key_fingerprint mismatch"

    arts = remote_manifest.get("artifacts")
    if not isinstance(arts, Mapping):
        return False, "remote manifest missing artifacts map"
    entry = arts.get(str(artifact.get("key") or ""))
    if not isinstance(entry, Mapping):
        return False, "artifact absent from remote manifest"

    entry_fp = str(entry.get("key_fingerprint") or "").strip().lower()
    if not _KEY_FP_RE.fullmatch(entry_fp) or entry_fp != want_fp:
        return False, "remote manifest artifact key_fingerprint mismatch"

    remote_name = str(artifact.get("remote_filename") or "")
    if str(entry.get("filename") or "") != remote_name:
        return False, "remote manifest filename mismatch"

    try:
        rec_enc_size = int(entry.get("size") or 0)
        rec_plain_size = int(entry.get("plain_size") if entry.get("plain_size") is not None else -1)
    except (TypeError, ValueError):
        return False, "invalid size in remote manifest"
    rec_md5 = str(entry.get("md5") or "").strip().lower()
    rec_src_sha = str(entry.get("source_sha256") or "").strip().lower()
    if rec_enc_size <= 0 or not rec_md5 or not _HEX64_RE.fullmatch(rec_src_sha):
        return False, "incomplete remote manifest entry"

    kind = str(artifact.get("kind") or "file")
    if kind == "file":
        art_src_sha = str(artifact.get("source_sha256") or "").strip().lower()
        expected_plain = int(artifact.get("plain_size") or -1)
        expected_enc = int(artifact.get("expected_enc_size") or -1)
        if rec_plain_size != expected_plain or rec_enc_size != expected_enc:
            return False, "recorded size differs from local artifact"
        if not _HEX64_RE.fullmatch(art_src_sha) or rec_src_sha != art_src_sha:
            return False, "recorded source_sha256 differs from local artifact"
    elif kind == "mirror_pack":
        art_pack_fp = str(artifact.get("pack_fingerprint") or "").strip().lower()
        rec_pack_fp = str(entry.get("pack_fingerprint") or "").strip().lower()
        if not _HEX64_RE.fullmatch(art_pack_fp) or rec_pack_fp != art_pack_fp:
            return False, "recorded mirror-pack fingerprint differs"
        if rec_plain_size <= 0 or rec_enc_size != backup_lib.expected_openssl_enc_size(rec_plain_size):
            return False, "recorded mirror-pack size mismatch"
    else:
        return False, f"unknown artifact kind: {kind}"

    size_ok, size_msg = backup_lib.verify_rclone_object_size(lsjson_out, remote_name, rec_enc_size)
    if not size_ok:
        return False, size_msg
    hash_ok, hash_msg = backup_lib.verify_rclone_hashsum(hashsum_out, remote_name, rec_md5)
    if not hash_ok:
        return False, hash_msg
    return True, f"verified size={rec_enc_size}"


def update_frozen_manifest(
    *,
    plan: Mapping[str, Any],
    existing_manifest: Mapping[str, Any] | None,
    artifact_key: str,
    plain_size: int,
    stream_meta: Mapping[str, Any],
    plain_meta: Mapping[str, Any],
    key_fingerprint: str,
    actual_pack_fingerprint: str = "",
) -> dict[str, Any]:
    """Merge one verified encrypted artifact entry into the key-bound frozen remote manifest."""
    want_fp = str(key_fingerprint or "").strip().lower()
    if not _KEY_FP_RE.fullmatch(want_fp):
        raise ValueError(f"Invalid key_fingerprint: {key_fingerprint!r}")

    art_map = {str(a["key"]): a for a in (plan.get("artifacts") or ())}
    if artifact_key not in art_map:
        raise KeyError(f"Artifact key {artifact_key!r} not in frozen plan")
    art = art_map[artifact_key]

    actual_plain_size = int(plain_meta["size"])
    actual_plain_sha = _require_valid_sha256(plain_meta.get("sha256"), f"plain_meta[{artifact_key}]")
    if actual_plain_size <= 0 or actual_plain_size != int(plain_size):
        raise ValueError(
            f"Plaintext size mismatch for {artifact_key}: streamed={actual_plain_size}, expected={plain_size}"
        )
    kind = str(art.get("kind") or "file")
    if kind == "file":
        expected_plan_sha = _require_valid_sha256(art.get("source_sha256"), f"plan[{artifact_key}].source_sha256")
        if actual_plain_sha != expected_plan_sha:
            raise ValueError(
                f"Streamed plaintext SHA-256 mismatch for {artifact_key}: "
                f"streamed={actual_plain_sha}, expected={expected_plan_sha}"
            )

    enc_size = int(stream_meta["size"])
    expected_enc = backup_lib.expected_openssl_enc_size(actual_plain_size)
    if enc_size != expected_enc:
        raise ValueError(
            f"Ciphertext size mismatch for {artifact_key}: actual={enc_size}, expected={expected_enc}"
        )

    merged_artifacts: dict[str, Any] = {}
    if isinstance(existing_manifest, Mapping):
        ok, reason = validate_frozen_manifest(
            existing_manifest,
            plan=plan,
            current_key_fingerprint=want_fp,
        )
        if not ok:
            raise ValueError(f"Cannot merge into incompatible existing manifest: {reason}")
        prev_arts = existing_manifest.get("artifacts")
        if isinstance(prev_arts, Mapping):
            for k, v in prev_arts.items():
                if isinstance(v, Mapping):
                    merged_artifacts[str(k)] = dict(v)

    entry: dict[str, Any] = {
        "filename": str(art["remote_filename"]),
        "plain_size": actual_plain_size,
        "size": enc_size,
        "md5": str(stream_meta["md5"]).strip().lower(),
        "source_sha256": actual_plain_sha,
        "key_fingerprint": want_fp,
    }
    if kind == "mirror_pack":
        expected_pack_fp = _require_valid_sha256(
            art.get("pack_fingerprint"), f"plan[{artifact_key}].pack_fingerprint"
        )
        got_pack_fp = _require_valid_sha256(
            actual_pack_fingerprint, f"actual[{artifact_key}].pack_fingerprint"
        )
        if got_pack_fp != expected_pack_fp:
            raise ValueError(
                f"Packed object list fingerprint mismatch for {artifact_key}: "
                f"actual={got_pack_fp}, expected={expected_pack_fp}"
            )
        entry["pack_fingerprint"] = got_pack_fp
    if stream_meta.get("sha256"):
        entry["sha256"] = str(stream_meta["sha256"]).strip().lower()
    merged_artifacts[artifact_key] = entry
    return {
        "version": 2,
        "backup_set_id": str(plan["backup_set_id"]),
        "tag": str(plan["tag"]),
        "remote_dir_name": str(plan["remote_dir_name"]),
        "key_fingerprint": want_fp,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "artifacts": merged_artifacts,
    }


def _cmd_build_delta(args: argparse.Namespace) -> int:
    daily_manifest = json.loads(Path(args.daily_manifest).read_text(encoding="utf-8"))
    daily_objects = daily_manifest.get("objects") or []
    baseline_id = args.baseline_id or ""
    baseline_objects: list[dict[str, Any]] = []
    if args.baseline_manifest and Path(args.baseline_manifest).is_file():
        bdata = json.loads(Path(args.baseline_manifest).read_text(encoding="utf-8"))
        baseline_id = baseline_id or str(bdata.get("baseline_id") or "")
        baseline_objects = list(bdata.get("objects") or [])
    elif args.sets_dir:
        baseline_id, baseline_objects = find_latest_weekly_baseline_manifest(Path(args.sets_dir))

    delta = compute_daily_minio_delta(daily_objects, baseline_objects)
    size, sha, count = build_daily_minio_delta_tar(
        delta,
        mirror_objects_dir=Path(args.mirror_objects_dir),
        out_tar_path=Path(args.out_tar),
    )
    print(
        json.dumps(
            {
                "baseline_id": baseline_id,
                "delta_object_count": count,
                "tar_size": size,
                "tar_sha256": sha,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_select_weekly_prune(args: argparse.Namespace) -> int:
    dirs = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
    referenced: list[str] = list(args.referenced_baseline or ())
    if args.referenced_file and Path(args.referenced_file).is_file():
        referenced.extend(
            line.strip()
            for line in Path(args.referenced_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    for name in select_weekly_baselines_to_prune(
        dirs,
        retain_weeks=int(args.retain),
        referenced_baseline_ids=referenced,
    ):
        print(name)
    return 0


def _cmd_weekly_minio_baseline(args: argparse.Namespace) -> int:
    from scripts.dalton.ops.prod.local_backup_set import resolve_everflow_bucket_specs

    specs = resolve_everflow_bucket_specs(os.environ)
    if not specs:
        print(
            "No active MinIO/S3 buckets resolved from environment; refusing to build empty weekly baseline",
            file=sys.stderr,
        )
        return 1
    records, payload = build_weekly_minio_baseline(
        bucket_specs=specs,
        baseline_id=args.baseline_id,
        out_tar_path=Path(args.out_tar),
        manifest_out_path=Path(args.manifest_out) if args.manifest_out else None,
        tmp_dir=Path(args.tmp_dir) if args.tmp_dir else None,
    )
    print(
        json.dumps(
            {
                "baseline_id": payload["baseline_id"],
                "object_count": len(records),
                "out_tar": args.out_tar,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_publish_weekly_baseline_pointer(args: argparse.Namespace) -> int:
    final_manifest = publish_weekly_baseline_pointer(
        staged_manifest_path=Path(args.staged_manifest),
        sets_dir=Path(args.sets_dir),
        baseline_id=args.baseline_id,
    )
    print(str(final_manifest))
    return 0


def _cmd_build_weekly_latest_json(args: argparse.Namespace) -> int:
    def _meta(path_str: str) -> dict[str, Any]:
        return json.loads(Path(path_str).read_text(encoding="utf-8"))

    arts: dict[str, Any] = {
        "minio": {"filename": args.minio_name, **_meta(args.minio_meta)},
        "redis": {"filename": args.redis_name, **_meta(args.redis_meta)},
        "var_dalton": {"filename": args.var_name, **_meta(args.var_meta)},
    }
    if args.sandboxes_json and Path(args.sandboxes_json).is_file():
        sb_list = json.loads(Path(args.sandboxes_json).read_text(encoding="utf-8"))
        if isinstance(sb_list, list):
            arts["sandboxes"] = sb_list
    payload = build_weekly_latest_json(
        run_id=args.run_id,
        date_stamp=args.date_stamp,
        baseline_id=args.baseline_id,
        minio_mode=args.minio_mode,
        artifacts=arts,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0


def _cmd_build_latest_json(args: argparse.Namespace) -> int:
    artifacts = json.loads(Path(args.artifacts_json).read_text(encoding="utf-8"))
    payload = build_daily_latest_json(
        run_id=args.run_id,
        date_stamp=args.date_stamp,
        backup_set_id=args.backup_set_id,
        baseline_id=args.baseline_id,
        artifacts=artifacts,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


def _cmd_read_verified_set(args: argparse.Namespace) -> int:
    data = json.loads(Path(args.set_file).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        print("latest-verified-set.json is not a JSON object", file=sys.stderr)
        return 1
    expected_date = "" if args.allow_stale else (args.expected_date or "").strip()
    ok, msg = validate_verified_set_window(data, expected_date=expected_date or None)
    if not ok:
        print(msg, file=sys.stderr)
        return 1
    print(str(data["backup_set_id"]))
    print(str(data["dump_path"]))
    print(str(data["manifest_path"]))
    print(str(data.get("asr_archive_path") or ""))
    return 0


def _cmd_read_json_field(args: argparse.Namespace) -> int:
    raw = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    data = json.loads(raw)
    if not isinstance(data, dict):
        return 1
    for field in args.field:
        val = data.get(field)
        print("" if val is None else str(val))
    return 0


def _cmd_write_artifacts_json(args: argparse.Namespace) -> int:
    def _meta(path_str: str) -> dict[str, Any]:
        return json.loads(Path(path_str).read_text(encoding="utf-8"))

    arts: dict[str, Any] = {
        "dump": {"filename": args.dump_name, **_meta(args.dump_meta)},
        "minio_manifest": {"filename": args.manifest_name, **_meta(args.manifest_meta)},
        "minio_delta": {"filename": args.delta_name, **_meta(args.delta_meta)},
    }
    if args.asr_name and args.asr_meta and Path(args.asr_meta).is_file():
        arts["asr_jobs"] = {"filename": args.asr_name, **_meta(args.asr_meta)}
    Path(args.out).write_text(json.dumps(arts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


def _cmd_plan_frozen_set(args: argparse.Namespace) -> int:
    try:
        plan = resolve_frozen_backup_set(
            backup_set_id=args.set_id,
            backups_root=Path(args.backups_root),
            remote=args.remote,
            drive_dir=args.drive_dir,
            sets_dir=Path(args.sets_dir) if args.sets_dir else None,
            mirror_objects_dir=Path(args.mirror_objects_dir) if args.mirror_objects_dir else None,
            require_trusted_digests=not bool(args.dry_run),
            manifest_snapshot_out=(
                Path(args.manifest_snapshot_out) if args.manifest_snapshot_out else None
            ),
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    payload = plan.to_dict()
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if args.dry_run:
        for line in format_frozen_dry_run_lines(payload):
            print(line)
    elif not args.out:
        print(json.dumps(payload, ensure_ascii=False))
    return 0


def _cmd_frozen_plan_tsv(args: argparse.Namespace) -> int:
    plan = json.loads(Path(args.plan_json).read_text(encoding="utf-8"))
    for art in plan.get("artifacts") or ():
        fields = [
            str(art.get("key") or ""),
            str(art.get("kind") or ""),
            str(art.get("filename") or ""),
            str(art.get("remote_filename") or ""),
            str(art.get("remote_path") or ""),
            str(art.get("plain_size") if art.get("plain_size") is not None else "0"),
            str(art.get("expected_enc_size") if art.get("expected_enc_size") is not None else "0"),
            str(art.get("local_path") or ""),
            str(art.get("source_sha256") or ""),
            str(art.get("pack_fingerprint") or ""),
        ]
        if any("|" in f or "\n" in f or "\r" in f for f in fields):
            print("Invalid delimiter or control character in frozen plan field", file=sys.stderr)
            return 1
        print("|".join(fields))
    return 0


def _cmd_check_local_pack_space(args: argparse.Namespace) -> int:
    try:
        ok, msg = check_local_pack_free_space(
            Path(args.dir),
            int(args.raw_bytes),
            multiplier=float(args.multiplier),
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(msg)
    return 0 if ok else 1


def _cmd_stream_plain_meta(args: argparse.Namespace) -> int:
    try:
        ok, _total, _sha, msg = stream_plain_hash_and_forward(
            sys.stdin.buffer,
            sys.stdout.buffer,
            Path(args.meta_out),
            expected_size=args.expected_size,
            expected_sha256=args.expected_sha256 or "",
        )
    except BrokenPipeError:
        return 1
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not ok:
        print(msg, file=sys.stderr)
        return 3
    return 0


def _cmd_pack_frozen_objects(args: argparse.Namespace) -> int:
    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("MinIO manifest is not a JSON object")
        expected_set_id = (
            str(args.expected_set_id or "").strip()
            or str(manifest.get("backup_set_id") or "").strip()
        )
        if not expected_set_id:
            raise ValueError("MinIO manifest is missing backup_set_id")
        _all_objs, deduped = validate_frozen_manifest_objects(
            manifest,
            expected_set_id=expected_set_id,
        )
        raw_total = sum(int(item["size"]) for item in deduped)
        out_tar = Path(args.out_tar)
        out_tar.parent.mkdir(parents=True, exist_ok=True)
        space_ok, space_msg = check_local_pack_free_space(
            out_tar.parent,
            raw_total,
            multiplier=float(args.min_free_multiplier),
        )
        if not space_ok:
            print(space_msg, file=sys.stderr)
            return 2
        size, sha, count, actual_pack_fp = build_frozen_objects_tar(
            deduped,
            backup_set_id=expected_set_id,
            mirror_objects_dir=Path(args.mirror_objects_dir),
            out_tar_path=out_tar,
        )
        if args.expected_pack_fingerprint:
            want_fp = _require_valid_sha256(
                args.expected_pack_fingerprint, "expected_pack_fingerprint"
            )
            if actual_pack_fp != want_fp:
                out_tar.unlink(missing_ok=True)
                raise RuntimeError(
                    f"Packed object list fingerprint mismatch: actual={actual_pack_fp}, expected={want_fp}"
                )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "tar_size": size,
                "tar_sha256": sha,
                "object_count": count,
                "pack_fingerprint": actual_pack_fp,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_validate_frozen_manifest(args: argparse.Namespace) -> int:
    try:
        plan = json.loads(Path(args.plan_json).read_text(encoding="utf-8"))
        manifest_raw = Path(args.manifest_file).read_text(encoding="utf-8").strip()
        manifest = json.loads(manifest_raw) if manifest_raw else None
        key_fp = backup_lib.key_fingerprint(Path(args.key_file))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    ok, msg = validate_frozen_manifest(
        manifest,
        plan=plan,
        current_key_fingerprint=key_fp,
    )
    print(msg)
    return 0 if ok else 1


def _cmd_probe_lsjson_file(args: argparse.Namespace) -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 1
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"invalid rclone lsjson output: {exc}", file=sys.stderr)
        return 2
    if not isinstance(items, list):
        print("rclone lsjson output is not a JSON list", file=sys.stderr)
        return 2
    target = args.filename
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("Name") == target or item.get("Path") == target:
            size = item.get("Size")
            if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
                print(str(size))
                return 0
            print(f"invalid Size in lsjson for {target}: {size!r}", file=sys.stderr)
            return 2
    return 1


def _cmd_check_frozen_remote(args: argparse.Namespace) -> int:
    plan = json.loads(Path(args.plan_json).read_text(encoding="utf-8"))
    art_map = {str(a["key"]): a for a in (plan.get("artifacts") or ())}
    artifact = art_map.get(args.key)
    if artifact is None:
        print(f"Artifact key not found in plan: {args.key}", file=sys.stderr)
        return 2
    try:
        key_fp = backup_lib.key_fingerprint(Path(args.key_file))
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    remote_manifest = None
    if args.remote_manifest_file and Path(args.remote_manifest_file).is_file():
        try:
            raw = Path(args.remote_manifest_file).read_text(encoding="utf-8").strip()
            if raw:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    remote_manifest = parsed
        except (OSError, json.JSONDecodeError, ValueError):
            remote_manifest = None
    lsjson_out = (
        Path(args.lsjson_file).read_text(encoding="utf-8")
        if args.lsjson_file and Path(args.lsjson_file).is_file()
        else ""
    )
    hashsum_out = (
        Path(args.hashsum_file).read_text(encoding="utf-8")
        if args.hashsum_file and Path(args.hashsum_file).is_file()
        else ""
    )
    ok, msg = is_frozen_artifact_verified_on_remote(
        artifact=artifact,
        remote_manifest=remote_manifest,
        lsjson_out=lsjson_out,
        hashsum_out=hashsum_out,
        current_key_fingerprint=key_fp,
        expected_backup_set_id=str(plan.get("backup_set_id") or ""),
    )
    print(msg)
    return 0 if ok else 1


def _cmd_merge_frozen_manifest(args: argparse.Namespace) -> int:
    plan = json.loads(Path(args.plan_json).read_text(encoding="utf-8"))
    existing = None
    if args.existing_manifest_file and Path(args.existing_manifest_file).is_file():
        raw = Path(args.existing_manifest_file).read_text(encoding="utf-8").strip()
        if raw:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("Existing manifest is not a JSON object")
            existing = parsed
    stream_meta = json.loads(Path(args.meta_file).read_text(encoding="utf-8"))
    plain_meta = json.loads(Path(args.plain_meta_file).read_text(encoding="utf-8"))
    key_fp = backup_lib.key_fingerprint(Path(args.key_file))
    updated = update_frozen_manifest(
        plan=plan,
        existing_manifest=existing,
        artifact_key=args.key,
        plain_size=int(args.plain_size),
        stream_meta=stream_meta,
        plain_meta=plain_meta,
        key_fingerprint=key_fp,
        actual_pack_fingerprint=args.actual_pack_fingerprint or "",
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.tmp.{os.getpid()}")
    tmp.write_text(
        json.dumps(updated, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, out)
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Everflow offsite weekly baseline & daily delta helper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_delta = sub.add_parser("build-delta")
    p_delta.add_argument("--daily-manifest", required=True)
    p_delta.add_argument("--baseline-manifest", default="")
    p_delta.add_argument("--sets-dir", default="")
    p_delta.add_argument("--baseline-id", default="")
    p_delta.add_argument("--mirror-objects-dir", required=True)
    p_delta.add_argument("--out-tar", required=True)
    p_delta.set_defaults(func=_cmd_build_delta)

    p_prune = sub.add_parser("select-weekly-prune")
    p_prune.add_argument("--retain", type=int, required=True)
    p_prune.add_argument("--referenced-baseline", action="append", default=[])
    p_prune.add_argument("--referenced-file", default="")
    p_prune.set_defaults(func=_cmd_select_weekly_prune)

    p_wk = sub.add_parser("weekly-minio-baseline")
    p_wk.add_argument("--baseline-id", required=True)
    p_wk.add_argument("--out-tar", required=True)
    p_wk.add_argument("--manifest-out", default="")
    p_wk.add_argument("--tmp-dir", default="")
    p_wk.set_defaults(func=_cmd_weekly_minio_baseline)

    p_pub = sub.add_parser("publish-weekly-baseline-pointer")
    p_pub.add_argument("--staged-manifest", required=True)
    p_pub.add_argument("--sets-dir", required=True)
    p_pub.add_argument("--baseline-id", required=True)
    p_pub.set_defaults(func=_cmd_publish_weekly_baseline_pointer)

    p_wlat = sub.add_parser("build-weekly-latest-json")
    p_wlat.add_argument("--run-id", required=True)
    p_wlat.add_argument("--date-stamp", required=True)
    p_wlat.add_argument("--baseline-id", required=True)
    p_wlat.add_argument("--minio-mode", default="versioned")
    p_wlat.add_argument("--minio-name", required=True)
    p_wlat.add_argument("--minio-meta", required=True)
    p_wlat.add_argument("--redis-name", required=True)
    p_wlat.add_argument("--redis-meta", required=True)
    p_wlat.add_argument("--var-name", required=True)
    p_wlat.add_argument("--var-meta", required=True)
    p_wlat.add_argument("--sandboxes-json", default="")
    p_wlat.add_argument("--out", required=True)
    p_wlat.set_defaults(func=_cmd_build_weekly_latest_json)

    p_lat = sub.add_parser("build-latest-json")
    p_lat.add_argument("--run-id", required=True)
    p_lat.add_argument("--date-stamp", required=True)
    p_lat.add_argument("--backup-set-id", required=True)
    p_lat.add_argument("--baseline-id", default="")
    p_lat.add_argument("--artifacts-json", required=True)
    p_lat.add_argument("--out", required=True)
    p_lat.set_defaults(func=_cmd_build_latest_json)

    p_rvs = sub.add_parser("read-verified-set")
    p_rvs.add_argument("--set-file", required=True)
    p_rvs.add_argument("--expected-date", default="")
    p_rvs.add_argument("--allow-stale", action="store_true")
    p_rvs.set_defaults(func=_cmd_read_verified_set)

    p_rjf = sub.add_parser("read-json-field")
    p_rjf.add_argument("--file", default="")
    p_rjf.add_argument("--field", action="append", required=True)
    p_rjf.set_defaults(func=_cmd_read_json_field)

    p_wa = sub.add_parser("write-artifacts-json")
    p_wa.add_argument("--dump-name", required=True)
    p_wa.add_argument("--dump-meta", required=True)
    p_wa.add_argument("--manifest-name", required=True)
    p_wa.add_argument("--manifest-meta", required=True)
    p_wa.add_argument("--delta-name", required=True)
    p_wa.add_argument("--delta-meta", required=True)
    p_wa.add_argument("--asr-name", default="")
    p_wa.add_argument("--asr-meta", default="")
    p_wa.add_argument("--out", required=True)
    p_wa.set_defaults(func=_cmd_write_artifacts_json)

    p_pfs = sub.add_parser("plan-frozen-set")
    p_pfs.add_argument("--set-id", required=True)
    p_pfs.add_argument("--backups-root", required=True)
    p_pfs.add_argument("--sets-dir", default="")
    p_pfs.add_argument("--mirror-objects-dir", default="")
    p_pfs.add_argument("--remote", required=True)
    p_pfs.add_argument("--drive-dir", required=True)
    p_pfs.add_argument("--out", default="")
    p_pfs.add_argument("--manifest-snapshot-out", default="")
    p_pfs.add_argument("--dry-run", action="store_true")
    p_pfs.set_defaults(func=_cmd_plan_frozen_set)

    p_ftsv = sub.add_parser("frozen-plan-tsv")
    p_ftsv.add_argument("--plan-json", required=True)
    p_ftsv.set_defaults(func=_cmd_frozen_plan_tsv)

    p_clps = sub.add_parser("check-local-pack-space")
    p_clps.add_argument("--dir", required=True)
    p_clps.add_argument("--raw-bytes", type=int, required=True)
    p_clps.add_argument("--multiplier", type=float, default=DEFAULT_LOCAL_PACK_SPACE_MULTIPLIER)
    p_clps.set_defaults(func=_cmd_check_local_pack_space)

    p_spm = sub.add_parser("stream-plain-meta")
    p_spm.add_argument("--meta-out", required=True)
    p_spm.add_argument("--expected-size", type=int, default=None)
    p_spm.add_argument("--expected-sha256", default="")
    p_spm.set_defaults(func=_cmd_stream_plain_meta)

    p_pfo = sub.add_parser("pack-frozen-objects")
    p_pfo.add_argument("--manifest", required=True)
    p_pfo.add_argument("--expected-set-id", default="")
    p_pfo.add_argument("--mirror-objects-dir", required=True)
    p_pfo.add_argument("--out-tar", required=True)
    p_pfo.add_argument("--expected-pack-fingerprint", default="")
    p_pfo.add_argument("--min-free-multiplier", type=float, default=DEFAULT_LOCAL_PACK_SPACE_MULTIPLIER)
    p_pfo.set_defaults(func=_cmd_pack_frozen_objects)

    p_vfm = sub.add_parser("validate-frozen-manifest")
    p_vfm.add_argument("--plan-json", required=True)
    p_vfm.add_argument("--manifest-file", required=True)
    p_vfm.add_argument("--key-file", required=True)
    p_vfm.set_defaults(func=_cmd_validate_frozen_manifest)

    p_plf = sub.add_parser("probe-lsjson-file")
    p_plf.add_argument("--filename", required=True)
    p_plf.set_defaults(func=_cmd_probe_lsjson_file)

    p_cfr = sub.add_parser("check-frozen-remote")
    p_cfr.add_argument("--plan-json", required=True)
    p_cfr.add_argument("--key", required=True)
    p_cfr.add_argument("--key-file", required=True)
    p_cfr.add_argument("--remote-manifest-file", default="")
    p_cfr.add_argument("--lsjson-file", default="")
    p_cfr.add_argument("--hashsum-file", default="")
    p_cfr.set_defaults(func=_cmd_check_frozen_remote)

    p_mfm = sub.add_parser("merge-frozen-manifest")
    p_mfm.add_argument("--plan-json", required=True)
    p_mfm.add_argument("--key", required=True)
    p_mfm.add_argument("--key-file", required=True)
    p_mfm.add_argument("--plain-size", type=int, required=True)
    p_mfm.add_argument("--meta-file", required=True)
    p_mfm.add_argument("--plain-meta-file", required=True)
    p_mfm.add_argument("--actual-pack-fingerprint", default="")
    p_mfm.add_argument("--existing-manifest-file", default="")
    p_mfm.add_argument("--out", required=True)
    p_mfm.set_defaults(func=_cmd_merge_frozen_manifest)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
