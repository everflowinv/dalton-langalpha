"""Shared consistent snapshot backup, version selection, ASR archiving & post-restore reconciliation.

Used by both Everflow production (``scripts/dalton/ops/prod/local_backup_set.py``,
``offsite_sync.py``) and Standalone Core (``scripts/standalone/consistent_backup.py``).
Zero profile-specific paths live here; callers supply their own sink target
(Everflow external mirror pool vs. Standalone Core ``minio-objects.tar.gz``).

Implements §3.1 of ``docs/dalton/features/workspace-file-storage.md``:
1. Acquire the two GC session advisory locks (bounded ``lock_timeout``).
2. Open a ``REPEATABLE READ READ ONLY`` transaction and ``pg_export_snapshot()``;
   hold it open while ``pg_dump -Fc --snapshot=...`` imports the snapshot and
   ``storage_refs.enumerate_snapshot_refs()`` collects live references + terminal ASR job IDs,
   then ``COMMIT`` (keeping the two session advisory locks held).
3. ``list_object_versions`` per active bucket and select versions by the two-rule union
   (present keys ∪ snapshot-referenced keys); pack only terminal ASR job directories +
   ``asr_usage.jsonl``.
4. Wait for ``pg_dump``, fetch pinned object versions computing ``archived_sha256``,
   release both session advisory locks, and run full-consumer ``storage_refs`` reconciliation.
5. Post-restore ``reconcile_restored_inflight_states`` following lock -> external IO outside lock ->
   re-lock & verify -> commit.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import hashlib
import io
import json
import logging
import os
import re
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from src.dalton.storage_refs import (
    ReconcileReport,
    SnapshotRefInventory,
    _apply_bucket_prefix,
    _extract_first_value,
    _extract_tuple,
    _table_exists,
    enumerate_snapshot_refs,
    reconcile_inventory_against_manifest,
)

logger = logging.getLogger(__name__)

WORKSPACE_FILE_BLOBS_GC_LOCK_KEY = "workspace_file_blobs:gc"
PROVENANCE_GC_LOCK_KEY = "provenance_gc_sweep"
DEFAULT_LOCK_TIMEOUT = "30s"
DEFAULT_SNAPSHOT_IMPORT_TIMEOUT_S = 120.0
STREAM_CHUNK_BYTES = 1024 * 1024  # 1 MiB

RESTORED_INFLIGHT_ERROR_MESSAGE = "恢复后请重新提交"
INFLIGHT_ASR_STATUSES: tuple[str, ...] = (
    "queued",
    "downloading",
    "probing",
    "splitting",
    "submitting",
    "transcribing",
    "assembling",
)

_SAFE_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_ASR_EXCLUDED_DIR_NAMES = frozenset({"segments", "tmp", ".tmp", "slices", "chunks"})
_ASR_EXCLUDED_FILE_SUFFIXES = (".lock", ".tmp", ".part", ".downloading")


class ConsistentBackupError(RuntimeError):
    """Base error raised by the consistent backup protocol."""


class MissingReferencedObjectsError(ConsistentBackupError):
    """Raised when one or more snapshot-referenced keys have no recoverable version in the bucket."""

    def __init__(self, bucket: str, missing_keys: Sequence[str]) -> None:
        self.bucket = bucket
        self.missing_keys = tuple(sorted(set(missing_keys)))
        sample = ", ".join(self.missing_keys[:10])
        suffix = f" (+{len(self.missing_keys) - 10} more)" if len(self.missing_keys) > 10 else ""
        super().__init__(
            f"Bucket {bucket!r} is missing {len(self.missing_keys)} snapshot-referenced object(s): "
            f"{sample}{suffix}"
        )


class BackupReconciliationError(ConsistentBackupError):
    """Raised when post-fetch full-consumer reconciliation reports one or more errors."""

    def __init__(self, report: ReconcileReport) -> None:
        self.report = report
        first = report.errors[0].detail if report.errors else "unknown reconciliation error"
        super().__init__(
            f"Backup reconciliation failed with {len(report.errors)} error(s): {first}"
        )


@dataclasses.dataclass(frozen=True)
class SelectedObjectVersion:
    """One pinned object version selected for archival during Step 3."""

    bucket: str
    key: str
    version_id: str | None  # None when bucket versioning is disabled; "null" preserved verbatim
    size: int | None = None
    content_type: str = "application/octet-stream"
    selected_from_delete_marker: bool = False


@dataclasses.dataclass(frozen=True)
class VersionSelectionResult:
    """Result of Step 3 version selection for one bucket."""

    bucket: str
    versioning_enabled: bool
    selected: tuple[SelectedObjectVersion, ...]
    skipped_pre_snapshot_deleted: int = 0
    warning: str | None = None


@dataclasses.dataclass(frozen=True)
class ArchivedObjectRecord:
    """One fetched & hashed object entry recorded in a backup manifest."""

    bucket: str
    key: str
    version_id: str | None
    size: int
    archived_sha256: str
    content_type: str = "application/octet-stream"

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "key": self.key,
            "version_id": self.version_id,
            "size": self.size,
            "archived_sha256": self.archived_sha256,
            "content_type": self.content_type,
        }


@dataclasses.dataclass(frozen=True)
class AsrArchiveSummary:
    """Metadata for a packaged terminal-only ``asr-jobs`` tar.gz archive."""

    archive_path: Path
    size_bytes: int
    sha256: str
    included_job_ids: tuple[str, ...]
    included_usage_ledger: bool


@dataclasses.dataclass(frozen=True)
class BucketArchiveSpec:
    """Target bucket definition passed to :func:`run_consistent_backup`."""

    bucket: str
    roles: tuple[str, ...]  # e.g. ("main",) or ("asr", "chat_upload")
    key_prefix: str = ""
    s3_client: Any = None
    versioning_enabled: bool | None = None  # None = auto-detect via get_bucket_versioning


@dataclasses.dataclass(frozen=True)
class ConsistentBackupResult:
    """Complete output of :func:`run_consistent_backup` (only returned after reconciliation passes)."""

    snapshot_id: str
    pg_dump_path: Path
    inventory: SnapshotRefInventory
    manifest_objects: tuple[ArchivedObjectRecord, ...]
    asr_archive: AsrArchiveSummary | None
    reconcile_report: ReconcileReport
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Step 1 & 2: Advisory locks + REPEATABLE READ snapshot export
# ---------------------------------------------------------------------------


async def acquire_gc_advisory_locks(
    conn: Any, *, lock_timeout: str = DEFAULT_LOCK_TIMEOUT
) -> None:
    """Acquire both GC session advisory locks with a bounded ``lock_timeout``."""
    async with conn.cursor() as cur:
        await cur.execute("SET idle_in_transaction_session_timeout = 0")
        await cur.execute("SELECT set_config('lock_timeout', %s, false)", (lock_timeout,))
        try:
            await cur.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0)), "
                "pg_advisory_lock(hashtextextended(%s, 0))",
                (WORKSPACE_FILE_BLOBS_GC_LOCK_KEY, PROVENANCE_GC_LOCK_KEY),
            )
            await cur.fetchone()
        finally:
            await cur.execute("RESET lock_timeout")


async def release_gc_advisory_locks(conn: Any) -> None:
    """Release both GC session advisory locks (best-effort safe for ``finally`` blocks)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0)), "
            "pg_advisory_unlock(hashtextextended(%s, 0))",
            (WORKSPACE_FILE_BLOBS_GC_LOCK_KEY, PROVENANCE_GC_LOCK_KEY),
        )
        await cur.fetchone()


async def begin_exported_snapshot(conn: Any) -> str:
    """Start a ``REPEATABLE READ READ ONLY`` transaction and return ``pg_export_snapshot()``."""
    async with conn.cursor() as cur:
        await cur.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        await cur.execute("SELECT pg_export_snapshot()")
        row = await cur.fetchone()
        snap_id = _extract_first_value(row)
        if not snap_id or not isinstance(snap_id, str):
            await cur.execute("ROLLBACK")
            raise ConsistentBackupError("pg_export_snapshot() returned empty snapshot id")
        return snap_id.strip()


async def commit_exported_snapshot(conn: Any) -> None:
    """Commit the ``REPEATABLE READ READ ONLY`` snapshot transaction (keeps session locks)."""
    async with conn.cursor() as cur:
        await cur.execute("COMMIT")


async def rollback_exported_snapshot(conn: Any) -> None:
    """Rollback the ``REPEATABLE READ READ ONLY`` snapshot transaction on failure."""
    try:
        async with conn.cursor() as cur:
            await cur.execute("ROLLBACK")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Step 3a: Two-rule object version selection
# ---------------------------------------------------------------------------


def _parse_last_modified(val: Any) -> float:
    if isinstance(val, dt.datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=dt.timezone.utc).timestamp()
        return val.timestamp()
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str) and val.strip():
        try:
            parsed = dt.datetime.fromisoformat(val.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return 0.0
    return 0.0


def detect_bucket_versioning(s3_client: Any, bucket: str) -> bool:
    """Return True when S3 ``get_bucket_versioning`` reports ``Status == 'Enabled'``."""
    try:
        resp = s3_client.get_bucket_versioning(Bucket=bucket)
    except Exception as exc:
        logger.warning(
            "get_bucket_versioning failed for bucket %s (%s); treating as unversioned",
            bucket,
            type(exc).__name__,
        )
        return False
    status = str((resp or {}).get("Status") or "").strip()
    return status == "Enabled"


def list_all_object_versions(
    s3_client: Any,
    bucket: str,
    *,
    prefix: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Paginate ``list_object_versions`` for ``bucket`` and return ``(versions, delete_markers)``."""
    versions: list[dict[str, Any]] = []
    delete_markers: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"Bucket": bucket}
    clean_prefix = prefix.strip().strip("/")
    if clean_prefix:
        kwargs["Prefix"] = f"{clean_prefix}/"

    while True:
        resp = s3_client.list_object_versions(**kwargs) or {}
        for item in resp.get("Versions") or []:
            if isinstance(item, Mapping):
                versions.append(dict(item))
        for item in resp.get("DeleteMarkers") or []:
            if isinstance(item, Mapping):
                delete_markers.append(dict(item))
        if not resp.get("IsTruncated"):
            break
        next_key = resp.get("NextKeyMarker")
        next_ver = resp.get("NextVersionIdMarker")
        if not next_key:
            break
        kwargs["KeyMarker"] = next_key
        if next_ver:
            kwargs["VersionIdMarker"] = next_ver
        else:
            kwargs.pop("VersionIdMarker", None)
    return versions, delete_markers


def list_all_current_objects(
    s3_client: Any,
    bucket: str,
    *,
    prefix: str = "",
) -> list[dict[str, Any]]:
    """Paginate ``list_objects_v2`` for an unversioned external bucket."""
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"Bucket": bucket}
    clean_prefix = prefix.strip().strip("/")
    if clean_prefix:
        kwargs["Prefix"] = f"{clean_prefix}/"

    while True:
        resp = s3_client.list_objects_v2(**kwargs) or {}
        for item in resp.get("Contents") or []:
            if isinstance(item, Mapping):
                items.append(dict(item))
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
        if not token:
            break
        kwargs["ContinuationToken"] = token
    return items


def select_bucket_object_versions(
    *,
    bucket: str,
    versions: Sequence[Mapping[str, Any]],
    delete_markers: Sequence[Mapping[str, Any]],
    snapshot_referenced_keys: Iterable[str],
    versioning_enabled: bool = True,
    target_logger: logging.Logger | None = None,
) -> VersionSelectionResult:
    """Apply the §3.1 Step 3 two-rule version selection over ``bucket``.

    - Rule 1: Keys whose current version is a normal object -> take current ``VersionId``
      (including ``"null"`` for pre-versioning objects).
    - Rule 2: Keys whose current version is a DeleteMarker AND which are referenced by
      the DB snapshot -> take the most recent non-delete-marker version before that DeleteMarker.
    - All other deleted keys (current is DeleteMarker AND not referenced by snapshot) are skipped.
    - Any snapshot-referenced key with no non-delete-marker version raises
      :class:`MissingReferencedObjectsError`.
    - When ``versioning_enabled=False``, emits a ``WARNING`` and selects current objects
      without ``VersionId``, still failing on any missing snapshot-referenced key.
    """
    log = target_logger or logger
    snap_keys = {str(k).strip().lstrip("/") for k in snapshot_referenced_keys if str(k).strip()}

    if not versioning_enabled:
        warn_msg = (
            f"Bucket {bucket!r} does not have versioning enabled; proceeding without "
            "VersionId pinning (external unversioned bucket mode)"
        )
        log.warning(warn_msg)
        by_key: dict[str, Mapping[str, Any]] = {}
        for item in versions:
            key = str(item.get("Key") or "").lstrip("/")
            if not key:
                continue
            # Prefer IsLatest if present, else overwrite/keep newest
            if key not in by_key or item.get("IsLatest"):
                by_key[key] = item
        missing = [k for k in snap_keys if k not in by_key]
        if missing:
            raise MissingReferencedObjectsError(bucket, missing)
        selected = tuple(
            SelectedObjectVersion(
                bucket=bucket,
                key=key,
                version_id=None,
                size=int(item["Size"]) if item.get("Size") is not None else None,
            )
            for key, item in sorted(by_key.items())
        )
        return VersionSelectionResult(
            bucket=bucket,
            versioning_enabled=False,
            selected=selected,
            skipped_pre_snapshot_deleted=0,
            warning=warn_msg,
        )

    versions_by_key: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for idx, item in enumerate(versions):
        key = str(item.get("Key") or "").lstrip("/")
        if key:
            versions_by_key.setdefault(key, []).append((idx, item))

    dms_by_key: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for idx, item in enumerate(delete_markers):
        key = str(item.get("Key") or "").lstrip("/")
        if key:
            dms_by_key.setdefault(key, []).append((idx, item))

    all_keys = sorted(set(versions_by_key) | set(dms_by_key) | snap_keys)
    selected_list: list[SelectedObjectVersion] = []
    missing_keys: list[str] = []
    skipped_deleted = 0

    for key in all_keys:
        key_versions = versions_by_key.get(key, [])
        key_dms = dms_by_key.get(key, [])

        latest_ver = next((item for _i, item in key_versions if item.get("IsLatest")), None)
        latest_dm = next((item for _i, item in key_dms if item.get("IsLatest")), None)

        if latest_ver is not None and latest_dm is None:
            # Rule 1: current version is a normal object
            vid = str(latest_ver.get("VersionId") if latest_ver.get("VersionId") is not None else "null")
            selected_list.append(
                SelectedObjectVersion(
                    bucket=bucket,
                    key=key,
                    version_id=vid,
                    size=int(latest_ver["Size"]) if latest_ver.get("Size") is not None else None,
                    selected_from_delete_marker=False,
                )
            )
            continue

        if latest_dm is not None:
            if key not in snap_keys:
                # Deleted before snapshot and unreferenced by snapshot -> skip
                skipped_deleted += 1
                continue
            # Rule 2: deleted after snapshot (current is DeleteMarker, snapshot still references key)
            if not key_versions:
                missing_keys.append(key)
                continue
            dm_ts = _parse_last_modified(latest_dm.get("LastModified"))
            # Choose the most recent non-delete-marker version before (or at) the delete marker.
            # When timestamps are identical/missing, preserve S3 newest-first list order (smaller idx).
            before_dm = [
                (idx, v)
                for idx, v in key_versions
                if _parse_last_modified(v.get("LastModified")) <= dm_ts or dm_ts == 0.0
            ]
            pool = before_dm if before_dm else key_versions
            pool.sort(key=lambda pair: (-_parse_last_modified(pair[1].get("LastModified")), pair[0]))
            chosen = pool[0][1]
            vid = str(chosen.get("VersionId") if chosen.get("VersionId") is not None else "null")
            selected_list.append(
                SelectedObjectVersion(
                    bucket=bucket,
                    key=key,
                    version_id=vid,
                    size=int(chosen["Size"]) if chosen.get("Size") is not None else None,
                    selected_from_delete_marker=True,
                )
            )
            continue

        # Neither latest_ver nor latest_dm marked IsLatest (e.g., snapshot references a missing key)
        if key in snap_keys:
            if key_versions:
                pool = sorted(
                    key_versions,
                    key=lambda pair: (-_parse_last_modified(pair[1].get("LastModified")), pair[0]),
                )
                chosen = pool[0][1]
                vid = str(chosen.get("VersionId") if chosen.get("VersionId") is not None else "null")
                selected_list.append(
                    SelectedObjectVersion(
                        bucket=bucket,
                        key=key,
                        version_id=vid,
                        size=int(chosen["Size"]) if chosen.get("Size") is not None else None,
                        selected_from_delete_marker=False,
                    )
                )
            else:
                missing_keys.append(key)

    if missing_keys:
        raise MissingReferencedObjectsError(bucket, missing_keys)

    return VersionSelectionResult(
        bucket=bucket,
        versioning_enabled=True,
        selected=tuple(selected_list),
        skipped_pre_snapshot_deleted=skipped_deleted,
    )


# ---------------------------------------------------------------------------
# Step 3b: Terminal-only ASR jobs archiving
# ---------------------------------------------------------------------------


def _is_excluded_asr_file(name: str) -> bool:
    lower = name.lower()
    return any(lower.endswith(sfx) for sfx in _ASR_EXCLUDED_FILE_SUFFIXES)


def archive_terminal_asr_jobs(
    asr_jobs_dir: Path,
    terminal_job_ids: Sequence[str],
    output_path: Path,
) -> AsrArchiveSummary:
    """Package ONLY terminal snapshot ASR job directories + top-level ``asr_usage.jsonl``.

    Excludes ``*.lock``, ``*.tmp``, ``*.part``, ``*.downloading``, and temporary
    segment/slice directories so restored ASR services never auto-resume post-snapshot
    or in-flight jobs.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(f".{output_path.name}.tmp.{os.getpid()}")
    included_jobs: list[str] = []
    included_usage = False

    try:
        with tarfile.open(tmp_path, "w:gz") as tf:
            usage_file = asr_jobs_dir / "asr_usage.jsonl"
            if usage_file.is_file() and not usage_file.is_symlink():
                tf.add(usage_file, arcname="asr_usage.jsonl", recursive=False)
                included_usage = True

            for raw_id in sorted(set(terminal_job_ids)):
                job_id = str(raw_id or "").strip()
                if not _SAFE_JOB_ID_RE.fullmatch(job_id):
                    continue
                job_dir = asr_jobs_dir / job_id
                if not job_dir.is_dir() or job_dir.is_symlink():
                    continue
                included_jobs.append(job_id)
                for root, dirs, files in os.walk(job_dir, followlinks=False):
                    dirs[:] = sorted(
                        d
                        for d in dirs
                        if d not in _ASR_EXCLUDED_DIR_NAMES
                        and not _is_excluded_asr_file(d)
                        and not (Path(root) / d).is_symlink()
                    )
                    root_path = Path(root)
                    for fname in sorted(files):
                        if _is_excluded_asr_file(fname):
                            continue
                        fpath = root_path / fname
                        if fpath.is_symlink() or not fpath.is_file():
                            continue
                        rel = fpath.relative_to(asr_jobs_dir).as_posix()
                        tf.add(fpath, arcname=rel, recursive=False)

        hasher = hashlib.sha256()
        total_size = 0
        with tmp_path.open("rb") as fh:
            while True:
                chunk = fh.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                total_size += len(chunk)
                hasher.update(chunk)
        os.replace(tmp_path, output_path)
        return AsrArchiveSummary(
            archive_path=output_path,
            size_bytes=total_size,
            sha256=hasher.hexdigest(),
            included_job_ids=tuple(included_jobs),
            included_usage_ledger=included_usage,
        )
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Step 4: Object fetching sinks (Everflow mirror pool & Standalone tar.gz)
# ---------------------------------------------------------------------------


def _fetch_s3_object_stream(
    s3_client: Any,
    bucket: str,
    key: str,
    version_id: str | None,
) -> tuple[Any, str]:
    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if version_id is not None:
        kwargs["VersionId"] = version_id
    resp = s3_client.get_object(**kwargs)
    body = resp["Body"]
    content_type = str(resp.get("ContentType") or "application/octet-stream")
    return body, content_type


class MirrorPoolSink:
    """Everflow production sink: writes deduplicated objects into ``<mirror_root>/objects/<aa>/<sha256>``."""

    def __init__(self, mirror_root: Path) -> None:
        self.mirror_root = mirror_root
        self.objects_dir = mirror_root / "objects"
        self.tmp_dir = mirror_root / ".tmp"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.records: list[ArchivedObjectRecord] = []

    def object_path(self, sha256_hex: str) -> Path:
        return self.objects_dir / sha256_hex[:2] / sha256_hex

    def archive_object(
        self,
        s3_client: Any,
        item: SelectedObjectVersion,
    ) -> ArchivedObjectRecord:
        body, content_type = _fetch_s3_object_stream(
            s3_client, item.bucket, item.key, item.version_id
        )
        hasher = hashlib.sha256()
        total = 0
        fd, tmp_name = tempfile.mkstemp(prefix="obj-", dir=str(self.tmp_dir))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out_fh:
                try:
                    while True:
                        chunk = body.read(STREAM_CHUNK_BYTES)
                        if not chunk:
                            break
                        total += len(chunk)
                        hasher.update(chunk)
                        out_fh.write(chunk)
                finally:
                    close_fn = getattr(body, "close", None)
                    if callable(close_fn):
                        close_fn()
            digest = hasher.hexdigest()
            dst = self.object_path(digest)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_file() and dst.stat().st_size == total:
                tmp_path.unlink(missing_ok=True)
            else:
                os.replace(tmp_path, dst)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

        rec = ArchivedObjectRecord(
            bucket=item.bucket,
            key=item.key,
            version_id=item.version_id,
            size=total,
            archived_sha256=digest,
            content_type=content_type,
        )
        self.records.append(rec)
        return rec

    def finalize(self) -> tuple[ArchivedObjectRecord, ...]:
        return tuple(self.records)


class TarGzObjectSink:
    """Standalone Core sink: writes objects + ``_minio_manifest.json`` into ``minio-objects.tar.gz``."""

    def __init__(self, tar_path: Path, *, tmp_dir: Path | None = None) -> None:
        self.tar_path = tar_path
        self.tar_path.parent.mkdir(parents=True, exist_ok=True)
        self._stage_dir = Path(
            tempfile.mkdtemp(prefix="minio-tar-stage-", dir=str(tmp_dir or tar_path.parent))
        )
        self._tmp_tar = tar_path.with_name(f".{tar_path.name}.tmp.{os.getpid()}")
        self._tf = tarfile.open(self._tmp_tar, "w:gz")
        self.records: list[ArchivedObjectRecord] = []
        self._closed = False

    def archive_object(
        self,
        s3_client: Any,
        item: SelectedObjectVersion,
    ) -> ArchivedObjectRecord:
        body, content_type = _fetch_s3_object_stream(
            s3_client, item.bucket, item.key, item.version_id
        )
        hasher = hashlib.sha256()
        total = 0
        tmp_obj = self._stage_dir / f"obj-{len(self.records)}.bin"
        try:
            with tmp_obj.open("wb") as out_fh:
                try:
                    while True:
                        chunk = body.read(STREAM_CHUNK_BYTES)
                        if not chunk:
                            break
                        total += len(chunk)
                        hasher.update(chunk)
                        out_fh.write(chunk)
                finally:
                    close_fn = getattr(body, "close", None)
                    if callable(close_fn):
                        close_fn()
            digest = hasher.hexdigest()
            arcname = f"objects/{item.bucket}/{item.key.lstrip('/')}"
            info = tarfile.TarInfo(name=arcname)
            info.size = total
            info.mtime = 0
            info.mode = 0o600
            with tmp_obj.open("rb") as in_fh:
                self._tf.addfile(info, in_fh)
        finally:
            tmp_obj.unlink(missing_ok=True)

        rec = ArchivedObjectRecord(
            bucket=item.bucket,
            key=item.key,
            version_id=item.version_id,
            size=total,
            archived_sha256=digest,
            content_type=content_type,
        )
        self.records.append(rec)
        return rec

    def finalize(self) -> tuple[ArchivedObjectRecord, ...]:
        if self._closed:
            return tuple(self.records)
        manifest_bytes = (
            json.dumps(
                {
                    "version": 2,
                    "objects": [r.to_dict() for r in self.records],
                },
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        info = tarfile.TarInfo(name="_minio_manifest.json")
        info.size = len(manifest_bytes)
        info.mtime = 0
        info.mode = 0o600
        self._tf.addfile(info, io.BytesIO(manifest_bytes))
        self._tf.close()
        self._closed = True
        os.replace(self._tmp_tar, self.tar_path)
        try:
            self._stage_dir.rmdir()
        except OSError:
            pass
        return tuple(self.records)

    def abort(self) -> None:
        if not self._closed:
            try:
                self._tf.close()
            except Exception:
                pass
            self._closed = True
        self._tmp_tar.unlink(missing_ok=True)
        try:
            for child in self._stage_dir.iterdir():
                child.unlink(missing_ok=True)
            self._stage_dir.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# End-to-end Step 1-4 orchestrator
# ---------------------------------------------------------------------------


async def run_consistent_backup(
    *,
    conn: Any,
    pg_dump_path: Path,
    pg_dump_fn: Callable[[str, Path], Any],
    bucket_specs: Sequence[BucketArchiveSpec],
    object_sink: MirrorPoolSink | TarGzObjectSink,
    asr_jobs_dir: Path | None = None,
    asr_archive_path: Path | None = None,
    lock_timeout: str = DEFAULT_LOCK_TIMEOUT,
    env: Mapping[str, str] | None = None,
    after_snapshot_commit_hook: Callable[[str, SnapshotRefInventory], Any] | None = None,
    wait_pg_dump_imported_fn: Callable[[Any], Any] | None = None,
    wait_pg_dump_finished_fn: Callable[[Any], Any] | None = None,
) -> ConsistentBackupResult:
    """Execute the full §3.1 Steps 1-4 consistent backup protocol.

    ``pg_dump_fn(snapshot_id, pg_dump_path)`` may either run synchronously/async to completion
    (when ``wait_pg_dump_imported_fn`` / ``wait_pg_dump_finished_fn`` are omitted) or start a
    background dump handle whose snapshot import is awaited before ``COMMIT`` and whose
    completion is awaited before Step 4 finishes.
    """
    locks_held = False
    snapshot_open = False
    warnings: list[str] = []
    dump_handle: Any = None

    try:
        # Step 1: hold both GC session advisory locks (bounded lock_timeout)
        await acquire_gc_advisory_locks(conn, lock_timeout=lock_timeout)
        locks_held = True

        # Step 2: REPEATABLE READ READ ONLY + pg_export_snapshot()
        snapshot_id = await begin_exported_snapshot(conn)
        snapshot_open = True

        # Enumerate live references & terminal ASR job IDs inside the exact snapshot
        has_main = any("main" in spec.roles for spec in bucket_specs)
        has_aux = any(
            role in ("asr", "chat_upload") for spec in bucket_specs for role in spec.roles
        )
        inventory = await enumerate_snapshot_refs(
            conn,
            env=env,
            include_main=has_main,
            include_aux=has_aux,
        )

        # Start pg_dump --snapshot=<snapshot_id> while the snapshot txn is still open
        dump_res = pg_dump_fn(snapshot_id, pg_dump_path)
        if asyncio.iscoroutine(dump_res):
            dump_handle = await dump_res
        else:
            dump_handle = dump_res

        if wait_pg_dump_imported_fn is not None:
            wait_imp = wait_pg_dump_imported_fn(dump_handle)
            if asyncio.iscoroutine(wait_imp):
                await wait_imp

        # Snapshot imported & refs enumerated -> COMMIT snapshot txn (session locks stay held!)
        await commit_exported_snapshot(conn)
        snapshot_open = False

        if after_snapshot_commit_hook is not None:
            hook_res = after_snapshot_commit_hook(snapshot_id, inventory)
            if asyncio.iscoroutine(hook_res):
                await hook_res

        # Step 3a: list_object_versions per bucket & select versions under the two rules
        keys_by_role = inventory.keys_by_bucket_role()
        bucket_targets: dict[str, tuple[str, str]] = {}
        selections: list[tuple[BucketArchiveSpec, VersionSelectionResult]] = []

        for spec in bucket_specs:
            snap_keys_for_bucket: set[str] = set()
            for role in spec.roles:
                bucket_targets[role] = (spec.bucket, spec.key_prefix.strip("/"))
                for logical_key in keys_by_role.get(role, ()):
                    snap_keys_for_bucket.add(_apply_bucket_prefix(logical_key, spec.key_prefix))

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
                snapshot_referenced_keys=snap_keys_for_bucket,
                versioning_enabled=ver_enabled,
            )
            if sel.warning:
                warnings.append(sel.warning)
            selections.append((spec, sel))

        # Step 3b: pack terminal ASR jobs only + asr_usage.jsonl
        asr_summary: AsrArchiveSummary | None = None
        if asr_jobs_dir is not None and asr_archive_path is not None:
            asr_summary = archive_terminal_asr_jobs(
                asr_jobs_dir,
                inventory.terminal_asr_job_ids,
                asr_archive_path,
            )

        # Step 4: wait for pg_dump to finish + fetch pinned objects computing archived_sha256
        if wait_pg_dump_finished_fn is not None:
            wait_fin = wait_pg_dump_finished_fn(dump_handle)
            if asyncio.iscoroutine(wait_fin):
                await wait_fin

        for spec, sel in selections:
            for obj in sel.selected:
                object_sink.archive_object(spec.s3_client, obj)
        manifest_records = object_sink.finalize()
    except Exception:
        if isinstance(object_sink, TarGzObjectSink):
            object_sink.abort()
        if snapshot_open:
            await rollback_exported_snapshot(conn)
            snapshot_open = False
        raise
    finally:
        if locks_held:
            try:
                await release_gc_advisory_locks(conn)
            except Exception as exc:
                logger.warning("Failed releasing GC advisory locks: %s", exc)

    # Step 4 (end): release locks DONE -> run full-consumer reconciliation
    reconcile_report = reconcile_inventory_against_manifest(
        inventory,
        [r.to_dict() for r in manifest_records],
        bucket_targets=bucket_targets,
        asr_jobs_archive=asr_summary.archive_path if asr_summary is not None else None,
        source_asr_jobs_dir=asr_jobs_dir,
    )
    if not reconcile_report.ok:
        raise BackupReconciliationError(reconcile_report)

    return ConsistentBackupResult(
        snapshot_id=snapshot_id,
        pg_dump_path=pg_dump_path,
        inventory=inventory,
        manifest_objects=manifest_records,
        asr_archive=asr_summary,
        reconcile_report=reconcile_report,
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Authoritative manifest restore helper (§3.2 step 1)
# ---------------------------------------------------------------------------


def build_archived_sha256_index(
    *,
    mirror_objects_dir: Path | None = None,
    tar_archives: Sequence[Path] = (),
) -> dict[str, Callable[[], bytes]]:
    """Build a lazy ``archived_sha256 -> loader()`` index from mirror pool and/or tar.gz archives."""
    index: dict[str, Callable[[], bytes]] = {}

    if mirror_objects_dir is not None and mirror_objects_dir.is_dir():
        for sub in sorted(mirror_objects_dir.iterdir()):
            if not sub.is_dir():
                continue
            for obj_file in sorted(sub.iterdir()):
                if obj_file.is_file() and not obj_file.is_symlink():
                    sha = obj_file.name.strip().lower()
                    index[sha] = lambda p=obj_file: p.read_bytes()

    for tar_path in tar_archives:
        if not tar_path.is_file():
            continue
        with tarfile.open(tar_path, "r:gz") as tf:
            manifest_member = None
            try:
                manifest_member = tf.getmember("_minio_manifest.json")
            except KeyError:
                pass
            if manifest_member is not None:
                fh = tf.extractfile(manifest_member)
                manifest = json.loads(fh.read().decode("utf-8")) if fh is not None else {}
                for entry in manifest.get("objects") or []:
                    sha = str(entry.get("archived_sha256") or entry.get("sha256") or "").lower()
                    bucket = str(entry.get("bucket") or "")
                    key = str(entry.get("key") or "").lstrip("/")
                    if not sha or sha in index:
                        continue
                    arc_candidates = (
                        f"objects/{bucket}/{key}",
                        f"objects/{sha[:2]}/{sha}",
                        f"by-sha256/{sha[:2]}/{sha}",
                    )
                    member_name = next(
                        (c for c in arc_candidates if c in tf.getnames()),
                        None,
                    )
                    if member_name is not None:
                        def _make_loader(tp: Path = tar_path, mn: str = member_name) -> bytes:
                            with tarfile.open(tp, "r:gz") as inner:
                                f = inner.extractfile(mn)
                                if f is None:
                                    raise FileNotFoundError(f"{mn} not found in {tp}")
                                return f.read()

                        index[sha] = _make_loader
            else:
                # Delta archive keyed directly as objects/<sha[:2]>/<sha>
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    name = member.name.lstrip("./")
                    parts = name.split("/")
                    if len(parts) >= 2 and re.fullmatch(r"[0-9a-f]{64}", parts[-1]):
                        sha = parts[-1].lower()
                        if sha not in index:
                            def _make_delta_loader(tp: Path = tar_path, mn: str = member.name) -> bytes:
                                with tarfile.open(tp, "r:gz") as inner:
                                    f = inner.extractfile(mn)
                                    if f is None:
                                        raise FileNotFoundError(f"{mn} not found in {tp}")
                                    return f.read()

                            index[sha] = _make_delta_loader
    return index


def restore_objects_from_manifest(
    manifest_objects: Sequence[Mapping[str, Any]],
    *,
    sha_index: Mapping[str, Callable[[], bytes]],
    s3_clients_by_bucket: Mapping[str, Any],
) -> dict[str, set[str]]:
    """Restore ONLY the keys listed in the target-day authoritative manifest and assert exact key equality."""
    expected_by_bucket: dict[str, set[str]] = {}
    for entry in manifest_objects:
        bucket = str(entry["bucket"])
        key = str(entry["key"]).lstrip("/")
        sha = str(entry["archived_sha256"]).lower()
        content_type = str(entry.get("content_type") or "application/octet-stream")
        client = s3_clients_by_bucket.get(bucket)
        if client is None:
            raise ConsistentBackupError(f"No S3 client configured for restore bucket {bucket!r}")
        loader = sha_index.get(sha)
        if loader is None:
            raise ConsistentBackupError(
                f"Missing archived content for {bucket}/{key} (archived_sha256={sha})"
            )
        payload = loader()
        actual_sha = hashlib.sha256(payload).hexdigest()
        if actual_sha != sha:
            raise ConsistentBackupError(
                f"Archived content corrupted for {bucket}/{key}: got {actual_sha}, expected {sha}"
            )
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType=content_type,
        )
        expected_by_bucket.setdefault(bucket, set()).add(key)

    # Assert exact bucket key set equality against target-day manifest
    for bucket, client in s3_clients_by_bucket.items():
        listed = list_all_current_objects(client, bucket)
        actual_keys = {str(item["Key"]).lstrip("/") for item in listed if item.get("Key")}
        expected_keys = expected_by_bucket.get(bucket, set())
        if actual_keys != expected_keys:
            extra = sorted(actual_keys - expected_keys)
            missing = sorted(expected_keys - actual_keys)
            raise ConsistentBackupError(
                f"Restored bucket {bucket!r} key set mismatch: "
                f"missing={len(missing)} ({missing[:5]}), extra={len(extra)} ({extra[:5]})"
            )
    return expected_by_bucket


# ---------------------------------------------------------------------------
# Post-restore inflight state convergence (`reconcile-restored-inflight-states`)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class InflightReconcileSummary:
    """Row counts converged by :func:`reconcile_restored_inflight_states`."""

    drive_uploads_aborted: int = 0
    chat_attachments_aborted: int = 0
    chat_uploads_promoted_ready: int = 0
    chat_uploads_aborted: int = 0
    transcriptions_cancelled: int = 0
    transcriptions_failed: int = 0

    def to_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


async def reconcile_restored_inflight_states(
    conn: Any,
    *,
    inspect_chat_upload_object_fn: Callable[[str], tuple[int, str] | None] | None = None,
) -> InflightReconcileSummary:
    """Converge all inflight DB rows after a restore per §3.1 matrix.

    Follows the unified concurrency pattern:
    - Reads ``completing`` ``dalton_chat_file_uploads`` rows first without holding a DB lock;
    - Performs object-storage size + SHA-256 verification OUTSIDE any DB transaction/lock;
    - Re-locks rows ``FOR UPDATE`` inside a DB transaction, re-checks state, and commits.
    """
    drive_aborted = 0
    att_aborted = 0
    chat_promoted = 0
    chat_aborted = 0
    asr_cancelled = 0
    asr_failed = 0

    # Phase 1: discover `completing` chat uploads without holding a long DB transaction
    completing_candidates: list[tuple[str, str, int, str | None, str | None]] = []
    has_chat_uploads = False
    async with conn.transaction():
        async with conn.cursor() as cur:
            has_chat_uploads = await _table_exists(cur, "public.dalton_chat_file_uploads")
            if has_chat_uploads:
                await cur.execute(
                    "SELECT upload_id, object_key, size_bytes, expected_sha256, actual_sha256 "
                    "FROM dalton_chat_file_uploads "
                    "WHERE cleaned_at IS NULL AND state = 'completing'"
                )
                for row in await cur.fetchall():
                    uid, okey, sz, exp_sha, act_sha = _extract_tuple(row, 5)
                    completing_candidates.append(
                        (
                            str(uid),
                            str(okey or "").strip(),
                            int(sz),
                            str(exp_sha).strip().lower() if exp_sha else None,
                            str(act_sha).strip().lower() if act_sha else None,
                        )
                    )

    # Phase 2: external S3 inspection strictly OUTSIDE any DB lock/transaction
    verified_ready_sha: dict[str, str] = {}
    if completing_candidates and inspect_chat_upload_object_fn is not None:
        for uid, okey, expected_size, expected_sha, actual_sha in completing_candidates:
            if not okey:
                continue
            res = inspect_chat_upload_object_fn(okey)
            if asyncio.iscoroutine(res):
                res = await res
            if res is None:
                continue
            obj_size, obj_sha = int(res[0]), str(res[1]).strip().lower()
            if obj_size != expected_size:
                continue
            want_sha = actual_sha or expected_sha
            if want_sha and obj_sha != want_sha:
                continue
            verified_ready_sha[uid] = obj_sha

    # Phase 3: re-lock & re-verify inside a clean transaction and commit
    async with conn.transaction():
        async with conn.cursor() as cur:
            # 1. drive_uploads
            if await _table_exists(cur, "public.drive_uploads"):
                await cur.execute(
                    """
                    UPDATE drive_uploads
                       SET state = 'aborted',
                           cleaned = TRUE,
                           lease_until = NULL,
                           lease_token = NULL,
                           updated_at = NOW()
                     WHERE state IN ('uploading', 'verifying')
                    """
                )
                drive_aborted = max(0, int(cur.rowcount or 0))

            # 2. chat_attachments
            if await _table_exists(cur, "public.chat_attachments"):
                await cur.execute(
                    """
                    UPDATE chat_attachments
                       SET state = 'aborted',
                           lease_until = NULL,
                           updated_at = NOW()
                     WHERE state = 'uploading'
                    """
                )
                att_aborted = max(0, int(cur.rowcount or 0))

            # 3. dalton_chat_file_uploads
            if has_chat_uploads:
                # Re-lock & promote verified `completing` uploads
                for uid, verified_sha in verified_ready_sha.items():
                    await cur.execute(
                        """
                        SELECT upload_id FROM dalton_chat_file_uploads
                         WHERE upload_id = %s
                           AND cleaned_at IS NULL
                           AND state = 'completing'
                           FOR UPDATE
                        """,
                        (uid,),
                    )
                    if await cur.fetchone() is not None:
                        await cur.execute(
                            """
                            UPDATE dalton_chat_file_uploads
                               SET state = 'ready',
                                   actual_sha256 = COALESCE(actual_sha256, %s),
                                   claim_token = NULL,
                                   lease_until = NULL,
                                   pending_claims = '{}'::jsonb,
                                   error = NULL,
                                   updated_at = NOW()
                             WHERE upload_id = %s
                               AND cleaned_at IS NULL
                               AND state = 'completing'
                            """,
                            (verified_sha, uid),
                        )
                        chat_promoted += max(0, int(cur.rowcount or 0))

                # Abort all remaining uploading/completing rows
                await cur.execute(
                    """
                    UPDATE dalton_chat_file_uploads
                       SET state = 'aborted',
                           claim_token = NULL,
                           lease_until = NULL,
                           pending_claims = '{}'::jsonb,
                           error = COALESCE(error, 'aborted_after_restore'),
                           updated_at = NOW()
                     WHERE cleaned_at IS NULL
                       AND state IN ('uploading', 'completing')
                    """
                )
                chat_aborted = max(0, int(cur.rowcount or 0))

            # 4. dalton_transcription_jobs
            if await _table_exists(cur, "public.dalton_transcription_jobs"):
                await cur.execute(
                    """
                    UPDATE dalton_transcription_jobs
                       SET status = 'cancelled',
                           stage = 'cancelled',
                           completed_at = COALESCE(completed_at, NOW()),
                           updated_at = NOW(),
                           version = version + 1
                     WHERE status = 'awaiting_upload'
                       AND COALESCE((usage -> 'upload' ->> 'completed')::boolean, FALSE) IS NOT TRUE
                    """
                )
                asr_cancelled = max(0, int(cur.rowcount or 0))

                await cur.execute(
                    """
                    UPDATE dalton_transcription_jobs
                       SET status = 'failed',
                           stage = 'failed',
                           error_category = COALESCE(error_category, 'restored_inflight'),
                           error_message = %s,
                           completed_at = COALESCE(completed_at, NOW()),
                           updated_at = NOW(),
                           version = version + 1
                     WHERE status = ANY(%s)
                    """,
                    (RESTORED_INFLIGHT_ERROR_MESSAGE, list(INFLIGHT_ASR_STATUSES)),
                )
                asr_failed = max(0, int(cur.rowcount or 0))

    return InflightReconcileSummary(
        drive_uploads_aborted=drive_aborted,
        chat_attachments_aborted=att_aborted,
        chat_uploads_promoted_ready=chat_promoted,
        chat_uploads_aborted=chat_aborted,
        transcriptions_cancelled=asr_cancelled,
        transcriptions_failed=asr_failed,
    )


def _default_chat_upload_inspector(object_key: str) -> tuple[int, str] | None:
    """Inspect a chat upload object using ``src.dalton.chat_upload_storage``."""
    from src.dalton import chat_upload_storage

    if not chat_upload_storage.is_chat_upload_storage_configured():
        return None
    size = chat_upload_storage.object_size(object_key)
    if size is None:
        return None
    sha = chat_upload_storage.sha256_object(object_key)
    if not sha:
        return None
    return int(size), str(sha).strip().lower()


# ---------------------------------------------------------------------------
# CLI entrypoint (`python -m src.dalton.consistent_backup ...`)
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m src.dalton.consistent_backup",
        description="Shared consistent backup & post-restore convergence CLI",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_rec = sub.add_parser(
        "reconcile-restored-inflight-states",
        help="Converge inflight uploads and active transcription jobs after a database restore.",
    )
    p_rec.add_argument(
        "--skip-object-check",
        action="store_true",
        help="Skip object-storage verification for completing chat uploads (abort them directly).",
    )

    p_asr = sub.add_parser(
        "archive-asr-jobs",
        help="Pack terminal ASR job directories and asr_usage.jsonl into a tar.gz archive.",
    )
    p_asr.add_argument("--asr-jobs-dir", required=True)
    p_asr.add_argument("--job-ids-json", required=True)
    p_asr.add_argument("--output", required=True)

    return ap


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.cmd == "reconcile-restored-inflight-states":
        from src.server.database.pool import open_session_connection

        inspector = None if args.skip_object_check else _default_chat_upload_inspector

        async def _run() -> InflightReconcileSummary:
            conn = await open_session_connection()
            try:
                return await reconcile_restored_inflight_states(
                    conn,
                    inspect_chat_upload_object_fn=inspector,
                )
            finally:
                await conn.close()

        summary = asyncio.run(_run())
        print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "archive-asr-jobs":
        raw_ids = json.loads(Path(args.job_ids_json).read_text(encoding="utf-8"))
        if isinstance(raw_ids, dict):
            job_ids = raw_ids.get("terminal_asr_job_ids") or []
        elif isinstance(raw_ids, list):
            job_ids = raw_ids
        else:
            raise ValueError("job-ids JSON must be a list or object with 'terminal_asr_job_ids'")
        summary = archive_terminal_asr_jobs(
            Path(args.asr_jobs_dir),
            [str(x) for x in job_ids],
            Path(args.output),
        )
        print(
            json.dumps(
                {
                    "archive_path": str(summary.archive_path),
                    "size_bytes": summary.size_bytes,
                    "sha256": summary.sha256,
                    "included_job_ids": list(summary.included_job_ids),
                    "included_usage_ledger": summary.included_usage_ledger,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
