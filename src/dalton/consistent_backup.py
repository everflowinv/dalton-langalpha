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
import inspect
import io
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from src.dalton.storage_config import resolve as resolve_storage_config
from src.dalton.storage_refs import (
    TERMINAL_ASR_ARCHIVE_STATUSES,
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
DEFAULT_PG_DUMP_TIMEOUT_S = 7200.0
DEFAULT_LOCK_PHASE_TIMEOUT_S = 7200.0
DEFAULT_MIRROR_TIMEOUT_S = 14400.0
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
RESTORED_NONTERMINAL_ASR_STATUSES: tuple[str, ...] = (
    *INFLIGHT_ASR_STATUSES,
    "indeterminate",
)

_SAFE_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_ASR_EXCLUDED_DIR_NAMES = frozenset({"segments", "tmp", ".tmp", "slices", "chunks"})
_ASR_EXCLUDED_FILE_SUFFIXES = (".lock", ".tmp", ".part", ".downloading")
_VALID_ADDRESSING_STYLES = frozenset({"virtual", "path", "auto"})


class ConsistentBackupError(RuntimeError):
    """Base error raised by the consistent backup protocol."""


class BackupTimeoutError(ConsistentBackupError, TimeoutError):
    """Raised when snapshot import, pg_dump, GC lock phase, or mirroring exceeds its hard deadline."""


class MissingBucketConfigError(ConsistentBackupError):
    """Raised when the DB snapshot contains references for a bucket role without configured storage."""

    def __init__(self, role: str, ref_count: int) -> None:
        self.role = role
        self.ref_count = ref_count
        super().__init__(
            f"Database snapshot contains {ref_count} reference(s) for bucket role {role!r}, "
            "but no object-storage bucket configuration was resolved for that role"
        )


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
            "sha256": self.archived_sha256,
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
    endpoint: str = ""


def _spec_endpoint_identity(spec: BucketArchiveSpec) -> str:
    """Normalize endpoint identity for deduplicating overlapping specs against one bucket."""
    ep = (spec.endpoint or "").strip().rstrip("/").lower()
    if ep:
        return ep
    client_ep = getattr(getattr(spec.s3_client, "_endpoint", None), "host", None)
    if isinstance(client_ep, str) and client_ep.strip():
        return client_ep.strip().rstrip("/").lower()
    return ""


def deduplicate_selections(
    selections: Sequence[tuple[BucketArchiveSpec, VersionSelectionResult]],
) -> list[tuple[BucketArchiveSpec, VersionSelectionResult]]:
    """Deduplicate selected objects by ``(endpoint, bucket, full_key)`` before archiving."""
    seen: set[tuple[str, str, str]] = set()
    deduped: list[tuple[BucketArchiveSpec, VersionSelectionResult]] = []
    for spec, sel in selections:
        ep_id = _spec_endpoint_identity(spec)
        unique_items: list[SelectedObjectVersion] = []
        for obj in sel.selected:
            full_key = str(obj.key).lstrip("/")
            dedup_key = (ep_id, str(obj.bucket), full_key)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            unique_items.append(obj)
        deduped.append((spec, dataclasses.replace(sel, selected=tuple(unique_items))))
    return deduped


_deduplicate_selections = deduplicate_selections


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
# Shared runtime bucket resolution & timeout / pg_dump process helpers
# ---------------------------------------------------------------------------


def _env_float(env: Mapping[str, str] | None, key: str, default: float) -> float:
    em = os.environ if env is None else env
    raw = (em.get(key) or "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    return val if val > 0 else default


def _remaining_or_raise(deadline: float, phase: str) -> float:
    rem = deadline - time.monotonic()
    if rem <= 0:
        raise BackupTimeoutError(f"Consistent backup exceeded hard deadline during {phase}")
    return rem


def _rewrite_container_minio_endpoint(endpoint_url: str, env: Mapping[str, str]) -> str:
    """Map container-internal ``http://minio:9000`` to host loopback ``http://127.0.0.1:9000``."""
    raw = (endpoint_url or "").strip().rstrip("/")
    if not raw:
        return ""
    host_override = (env.get("DALTON_HOST_STORAGE_ENDPOINT_URL") or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if (parsed.hostname or "").lower() in ("minio", "minio-s3", "host.docker.internal"):
        if host_override:
            return host_override
        port = parsed.port or 9000
        return urlunsplit((parsed.scheme or "http", f"127.0.0.1:{port}", parsed.path, "", ""))
    return raw


def build_backup_s3_client(
    *,
    endpoint_url: str | None,
    access_key: str | None,
    secret_key: str | None,
    region: str = "us-east-1",
    addressing_style: str = "virtual",
    session_token: str | None = None,
) -> Any:
    """Build a boto3 S3 client honoring runtime endpoint/credential/addressing semantics."""
    import boto3
    from botocore.config import Config

    style = (addressing_style or "virtual").strip().lower()
    if style not in _VALID_ADDRESSING_STYLES:
        style = "virtual"
    kwargs: dict[str, Any] = {
        "region_name": (region or "us-east-1").strip() or "us-east-1",
        "config": Config(
            signature_version="s3v4",
            s3={"addressing_style": style},
            connect_timeout=10,
            read_timeout=120,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    }
    ep = (endpoint_url or "").strip().rstrip("/")
    if ep:
        kwargs["endpoint_url"] = ep
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
        if session_token:
            kwargs["aws_session_token"] = session_token
    return boto3.client("s3", **kwargs)


def resolve_runtime_bucket_specs(
    env: Mapping[str, str] | None = None,
    *,
    apply_core_defaults: bool = False,
    rewrite_container_minio: bool = False,
    client_factory: Callable[..., Any] = build_backup_s3_client,
) -> list[BucketArchiveSpec]:
    """Resolve active object-storage buckets using runtime storage rules (supporting ``AWS_*``).

    Shared single source of truth for Everflow production (``local_backup_set.py``,
    ``offsite_sync.py``) and Standalone Core (``scripts/standalone/consistent_backup.py``).
    """
    em = os.environ if env is None else env
    specs_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _norm_ep(raw_ep: str) -> str:
        cleaned = (raw_ep or "").strip().rstrip("/")
        if rewrite_container_minio and cleaned:
            return _rewrite_container_minio_endpoint(cleaned, em)
        return cleaned

    cfg = resolve_storage_config(env=em, apply_core_defaults=apply_core_defaults)
    if cfg.is_storage_enabled and cfg.bucket_name:
        if cfg.provider != "s3":
            raise ConsistentBackupError(
                f"Consistent S3 backup does not support main storage provider {cfg.provider!r}"
            )
        ep = _norm_ep(cfg.endpoint_url or "")
        ak = (
            em.get("STORAGE_ACCESS_KEY_ID")
            or em.get("S3_ACCESS_KEY_ID")
            or em.get("AWS_ACCESS_KEY_ID")
            or em.get("MINIO_ROOT_USER")
            or ""
        ).strip()
        sk = (
            em.get("STORAGE_SECRET_ACCESS_KEY")
            or em.get("S3_SECRET_ACCESS_KEY")
            or em.get("AWS_SECRET_ACCESS_KEY")
            or em.get("MINIO_ROOT_PASSWORD")
            or ""
        ).strip()
        tok = (em.get("AWS_SESSION_TOKEN") or "").strip() or None
        region = (
            em.get("STORAGE_REGION")
            or em.get("S3_REGION")
            or em.get("AWS_DEFAULT_REGION")
            or em.get("AWS_REGION")
            or "us-east-1"
        ).strip()
        default_style = "path" if ep else "virtual"
        raw_style = (em.get("STORAGE_ADDRESSING_STYLE") or default_style).strip().lower()
        style = raw_style if raw_style in _VALID_ADDRESSING_STYLES else default_style
        prefix = (em.get("STORAGE_KEY_PREFIX") or "").strip().strip("/")
        if ep and (not ak or not sk):
            raise ConsistentBackupError(
                f"Main storage bucket {cfg.bucket_name!r} at {ep!r} is enabled but credentials "
                "are missing (checked STORAGE_*, S3_*, AWS_*, MINIO_ROOT_*)"
            )
        key = (ep, cfg.bucket_name, prefix)
        specs_by_key[key] = {
            "endpoint": ep or None,
            "bucket": cfg.bucket_name,
            "prefix": prefix,
            "roles": ["main"],
            "ak": ak or None,
            "sk": sk or None,
            "tok": tok if (ak and sk) else None,
            "region": region,
            "style": style,
        }

    # ASR bucket (matches src/dalton/transcription_storage.py)
    asr_bucket = (em.get("DALTON_ASR_STORAGE_BUCKET_NAME") or "").strip()
    asr_resolved_key: tuple[str, str, str] | None = None
    if asr_bucket:
        asr_ep = _norm_ep(em.get("DALTON_ASR_STORAGE_ENDPOINT_URL") or "http://minio:9000")
        asr_ak = (
            em.get("DALTON_ASR_STORAGE_ACCESS_KEY_ID")
            or em.get("AWS_ACCESS_KEY_ID")
            or em.get("MINIO_ROOT_USER")
            or ""
        ).strip()
        asr_sk = (
            em.get("DALTON_ASR_STORAGE_SECRET_ACCESS_KEY")
            or em.get("AWS_SECRET_ACCESS_KEY")
            or em.get("MINIO_ROOT_PASSWORD")
            or ""
        ).strip()
        asr_region = (em.get("DALTON_ASR_STORAGE_REGION") or "us-east-1").strip()
        asr_style = (em.get("DALTON_ASR_STORAGE_ADDRESSING_STYLE") or "path").strip().lower()
        asr_prefix = (em.get("DALTON_ASR_STORAGE_KEY_PREFIX") or "").strip().strip("/")
        if not asr_ak or not asr_sk:
            raise ConsistentBackupError(
                f"ASR storage bucket {asr_bucket!r} is configured but credentials are missing"
            )
        asr_resolved_key = (asr_ep, asr_bucket, asr_prefix)
        if asr_resolved_key in specs_by_key:
            specs_by_key[asr_resolved_key]["roles"].append("asr")
        else:
            specs_by_key[asr_resolved_key] = {
                "endpoint": asr_ep,
                "bucket": asr_bucket,
                "prefix": asr_prefix,
                "roles": ["asr"],
                "ak": asr_ak,
                "sk": asr_sk,
                "tok": None,
                "region": asr_region,
                "style": asr_style,
            }

    # Chat upload bucket (matches src/dalton/chat_upload_storage.py:_has_dedicated_env)
    dedicated_chat_vars = (
        "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL",
        "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME",
        "DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID",
        "DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY",
    )
    chat_bucket = (em.get("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME") or "").strip()
    has_full_chat = all((em.get(v) or "").strip() for v in dedicated_chat_vars)
    if has_full_chat or (chat_bucket and not asr_bucket):
        chat_ep = _norm_ep(
            em.get("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL") or "http://minio:9000"
        )
        chat_ak = (
            em.get("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID")
            or em.get("AWS_ACCESS_KEY_ID")
            or em.get("MINIO_ROOT_USER")
            or ""
        ).strip()
        chat_sk = (
            em.get("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY")
            or em.get("AWS_SECRET_ACCESS_KEY")
            or em.get("MINIO_ROOT_PASSWORD")
            or ""
        ).strip()
        chat_region = (em.get("DALTON_CHAT_UPLOAD_STORAGE_REGION") or "us-east-1").strip()
        chat_style = (
            em.get("DALTON_CHAT_UPLOAD_STORAGE_ADDRESSING_STYLE") or "path"
        ).strip().lower()
        chat_prefix = (em.get("DALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX") or "").strip().strip("/")
        if not chat_ak or not chat_sk:
            raise ConsistentBackupError(
                f"Chat upload storage bucket {chat_bucket!r} is configured but credentials are missing"
            )
        ckey = (chat_ep, chat_bucket, chat_prefix)
        if ckey in specs_by_key:
            specs_by_key[ckey]["roles"].append("chat_upload")
        else:
            specs_by_key[ckey] = {
                "endpoint": chat_ep,
                "bucket": chat_bucket,
                "prefix": chat_prefix,
                "roles": ["chat_upload"],
                "ak": chat_ak,
                "sk": chat_sk,
                "tok": None,
                "region": chat_region,
                "style": chat_style,
            }
    elif asr_resolved_key is not None and asr_resolved_key in specs_by_key:
        if "chat_upload" not in specs_by_key[asr_resolved_key]["roles"]:
            specs_by_key[asr_resolved_key]["roles"].append("chat_upload")

    out: list[BucketArchiveSpec] = []
    for item in specs_by_key.values():
        factory_kwargs: dict[str, Any] = {
            "endpoint_url": item["endpoint"] or "",
            "access_key": item["ak"] or "",
            "secret_key": item["sk"] or "",
            "region": item["region"],
            "addressing_style": item["style"],
        }
        if item.get("tok") and client_factory is build_backup_s3_client:
            factory_kwargs["session_token"] = item["tok"]
        client = client_factory(**factory_kwargs)
        out.append(
            BucketArchiveSpec(
                bucket=item["bucket"],
                roles=tuple(dict.fromkeys(item["roles"])),
                key_prefix=item["prefix"],
                s3_client=client,
                endpoint=item["endpoint"] or "",
            )
        )
    return out


class _StreamDrain:
    """Continuously drain a subprocess pipe in a daemon thread so output never fills OS pipe buffers."""

    def __init__(self, stream: Any, *, max_bytes: int | None = None) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._buf = bytearray()
        self._thread = threading.Thread(
            target=self._run,
            name="consistent-backup-pipe-drain",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(65536)
                if not chunk:
                    break
                if self._max_bytes is None:
                    self._buf.extend(chunk)
                else:
                    rem = self._max_bytes - len(self._buf)
                    if rem > 0:
                        self._buf.extend(chunk[:rem])
        except Exception:
            pass
        finally:
            try:
                self._stream.close()
            except Exception:
                pass

    def close(self) -> None:
        try:
            self._stream.close()
        except Exception:
            pass

    def read_all(self, timeout: float = 2.0) -> bytes:
        self._thread.join(timeout=timeout)
        return bytes(self._buf)


@dataclasses.dataclass
class PgDumpProcess:
    """Managed non-blocking ``pg_dump --snapshot`` subprocess with process-group termination."""

    popen: subprocess.Popen[bytes]
    out_path: Path
    out_fh: Any
    cmd_label: str = "pg_dump"
    stderr_drain: _StreamDrain | None = None
    closed: bool = False


def start_pg_dump_process(
    cmd: Sequence[str],
    out_path: Path,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    cmd_label: str = "pg_dump",
) -> PgDumpProcess:
    """Spawn ``pg_dump`` in its own session/process group writing custom-format bytes to ``out_path``."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_fh = out_path.open("wb")
    try:
        popen = subprocess.Popen(
            list(cmd),
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            stdout=out_fh,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except Exception:
        out_fh.close()
        raise
    stderr_drain = (
        _StreamDrain(popen.stderr, max_bytes=1024 * 1024)
        if popen.stderr is not None
        else None
    )
    return PgDumpProcess(
        popen=popen,
        out_path=out_path,
        out_fh=out_fh,
        cmd_label=cmd_label,
        stderr_drain=stderr_drain,
    )


def terminate_pg_dump_process(proc: PgDumpProcess) -> None:
    """Terminate ``pg_dump`` (and its process group) and close file descriptors cleanly."""
    if proc.closed:
        return
    try:
        if proc.popen.poll() is None:
            try:
                os.killpg(proc.popen.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    proc.popen.terminate()
                except OSError:
                    pass
            try:
                proc.popen.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.popen.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    try:
                        proc.popen.kill()
                    except OSError:
                        pass
                try:
                    proc.popen.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
    finally:
        if proc.stderr_drain is not None:
            proc.stderr_drain.close()
        elif proc.popen.stderr is not None:
            try:
                proc.popen.stderr.close()
            except Exception:
                pass
        try:
            proc.out_fh.close()
        except Exception:
            pass
        proc.closed = True


def _read_pg_dump_stderr(proc: PgDumpProcess) -> bytes:
    if proc.stderr_drain is not None:
        return proc.stderr_drain.read_all()
    if proc.popen.stderr is not None:
        try:
            return proc.popen.stderr.read()
        except (OSError, ValueError):
            return b""
    return b""


async def wait_pg_dump_imported(
    proc: PgDumpProcess,
    *,
    timeout_s: float = DEFAULT_SNAPSHOT_IMPORT_TIMEOUT_S,
    poll_interval_s: float = 0.05,
) -> None:
    """Wait until ``pg_dump -Fc --snapshot=...`` starts emitting output (snapshot imported) or finishes."""
    deadline = time.monotonic() + max(0.05, float(timeout_s))
    while True:
        if proc.closed:
            raise BackupTimeoutError(f"{proc.cmd_label} was terminated while waiting to import snapshot")
        rc = proc.popen.poll()
        if rc is not None:
            if rc != 0:
                err_bytes = _read_pg_dump_stderr(proc)
                terminate_pg_dump_process(proc)
                err = err_bytes.decode("utf-8", errors="replace").strip()
                raise ConsistentBackupError(
                    f"{proc.cmd_label} failed before importing snapshot (rc={rc}): {err}"
                )
            return
        try:
            if proc.out_path.stat().st_size > 0:
                return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            terminate_pg_dump_process(proc)
            raise BackupTimeoutError(
                f"{proc.cmd_label} timed out after {timeout_s:.1f}s waiting to import snapshot"
            )
        await asyncio.sleep(poll_interval_s)


async def wait_pg_dump_finished(
    proc: PgDumpProcess,
    *,
    timeout_s: float = DEFAULT_PG_DUMP_TIMEOUT_S,
    poll_interval_s: float = 0.1,
) -> None:
    """Wait for ``pg_dump`` to finish within ``timeout_s``; terminate and raise on timeout or failure."""
    deadline = time.monotonic() + max(0.05, float(timeout_s))
    while True:
        if proc.closed:
            raise BackupTimeoutError(f"{proc.cmd_label} was terminated before completing dump")
        rc = proc.popen.poll()
        if rc is not None:
            err_bytes = _read_pg_dump_stderr(proc)
            try:
                proc.out_fh.flush()
            except Exception:
                pass
            terminate_pg_dump_process(proc)
            size = proc.out_path.stat().st_size if proc.out_path.exists() else 0
            if rc != 0 or size == 0:
                err = err_bytes.decode("utf-8", errors="replace").strip()
                raise ConsistentBackupError(
                    f"{proc.cmd_label} --snapshot failed (rc={rc}, size={size}): {err}"
                )
            return
        if time.monotonic() >= deadline:
            terminate_pg_dump_process(proc)
            raise BackupTimeoutError(
                f"{proc.cmd_label} timed out after {timeout_s:.1f}s waiting for dump completion"
            )
        await asyncio.sleep(poll_interval_s)


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
    """Release both GC session advisory locks."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0)), "
            "pg_advisory_unlock(hashtextextended(%s, 0))",
            (WORKSPACE_FILE_BLOBS_GC_LOCK_KEY, PROVENANCE_GC_LOCK_KEY),
        )
        row = await cur.fetchone()
        if isinstance(row, (tuple, list)) and len(row) >= 2 and (row[0] is False or row[1] is False):
            raise ConsistentBackupError(
                f"pg_advisory_unlock failed to release one or both GC locks: {row!r}"
            )


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
    """Return True when S3 ``get_bucket_versioning`` reports ``Status == 'Enabled'``.

    Fails closed on any S3/network error rather than misclassifying a versioned bucket
    as unversioned.
    """
    try:
        resp = s3_client.get_bucket_versioning(Bucket=bucket)
    except Exception as exc:
        raise ConsistentBackupError(
            f"Failed probing bucket versioning for {bucket!r} ({type(exc).__name__}): {exc}"
        ) from exc
    status = str((resp or {}).get("Status") or "").strip()
    return status == "Enabled"


def list_all_object_versions(
    s3_client: Any,
    bucket: str,
    *,
    prefix: str = "",
    deadline: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Paginate ``list_object_versions`` for ``bucket`` and return ``(versions, delete_markers)``."""
    versions: list[dict[str, Any]] = []
    delete_markers: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"Bucket": bucket}
    clean_prefix = prefix.strip().strip("/")
    if clean_prefix:
        kwargs["Prefix"] = f"{clean_prefix}/"

    while True:
        if deadline is not None:
            _remaining_or_raise(deadline, f"list_object_versions({bucket})")
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
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    """Paginate ``list_objects_v2`` for an unversioned external bucket."""
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"Bucket": bucket}
    clean_prefix = prefix.strip().strip("/")
    if clean_prefix:
        kwargs["Prefix"] = f"{clean_prefix}/"

    while True:
        if deadline is not None:
            _remaining_or_raise(deadline, f"list_objects_v2({bucket})")
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
    *,
    deadline: float | None = None,
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
            if deadline is not None:
                _remaining_or_raise(deadline, "archive_terminal_asr_jobs")
            usage_file = asr_jobs_dir / "asr_usage.jsonl"
            if usage_file.is_file() and not usage_file.is_symlink():
                tf.add(usage_file, arcname="asr_usage.jsonl", recursive=False)
                included_usage = True

            for raw_id in sorted(set(terminal_job_ids)):
                if deadline is not None:
                    _remaining_or_raise(deadline, "archive_terminal_asr_jobs")
                job_id = str(raw_id or "").strip()
                if not _SAFE_JOB_ID_RE.fullmatch(job_id):
                    continue
                job_dir = asr_jobs_dir / job_id
                if not job_dir.is_dir() or job_dir.is_symlink():
                    continue
                # Guard against post-snapshot retries (e.g. a failed job retried in-place) or
                # indeterminate jobs: only archive directories whose live state.json is terminal.
                state_path = job_dir / "state.json"
                if state_path.is_file() and not state_path.is_symlink():
                    try:
                        state_obj = json.loads(state_path.read_text(encoding="utf-8"))
                    except Exception:
                        logger.warning(
                            "Skipping ASR job directory %s with unreadable state.json",
                            job_id,
                        )
                        continue
                    live_status = (
                        str(state_obj.get("status") or "").strip().lower()
                        if isinstance(state_obj, dict)
                        else ""
                    )
                    if live_status not in TERMINAL_ASR_ARCHIVE_STATUSES:
                        logger.info(
                            "Skipping ASR job directory %s with non-terminal live status %r",
                            job_id,
                            live_status,
                        )
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
                        if deadline is not None:
                            _remaining_or_raise(deadline, "archive_terminal_asr_jobs")
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
                if deadline is not None:
                    _remaining_or_raise(deadline, "archive_terminal_asr_jobs_hash")
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


_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclasses.dataclass
class AsrArchiveProcess:
    """Managed subprocess running ``archive-asr-jobs`` in its own process group."""

    popen: subprocess.Popen[bytes]
    ids_path: Path
    output_path: Path
    summary_path: Path | None = None
    stdout_drain: _StreamDrain | None = None
    stderr_drain: _StreamDrain | None = None
    closed: bool = False


def _build_asr_archive_cmd(
    asr_jobs_dir: Path,
    ids_path: Path,
    output_path: Path,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "src.dalton.consistent_backup",
        "archive-asr-jobs",
        "--asr-jobs-dir",
        str(asr_jobs_dir),
        "--job-ids-json",
        str(ids_path),
        "--output",
        str(output_path),
    ]


def start_asr_archive_process(
    asr_jobs_dir: Path,
    terminal_job_ids: Sequence[str],
    output_path: Path,
) -> AsrArchiveProcess:
    """Spawn ``archive-asr-jobs`` in its own session/process group so watchdog can kill it on timeout."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, ids_name = tempfile.mkstemp(prefix=".asr-job-ids-", suffix=".json", dir=str(output_path.parent))
    ids_path = Path(ids_name)
    sfd, summary_name = tempfile.mkstemp(
        prefix=".asr-archive-summary-", suffix=".json", dir=str(output_path.parent)
    )
    os.close(sfd)
    summary_path = Path(summary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"terminal_asr_job_ids": list(terminal_job_ids)}, fh)
        sub_env = dict(os.environ)
        existing_pp = sub_env.get("PYTHONPATH") or ""
        root_str = str(_REPO_ROOT)
        if root_str not in existing_pp.split(os.pathsep):
            sub_env["PYTHONPATH"] = f"{root_str}{os.pathsep}{existing_pp}" if existing_pp else root_str
        cmd = list(_build_asr_archive_cmd(asr_jobs_dir, ids_path, output_path))
        if "archive-asr-jobs" in cmd and "--summary-out" not in cmd:
            cmd.extend(["--summary-out", str(summary_path)])
        popen = subprocess.Popen(
            cmd,
            cwd=root_str,
            env=sub_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except Exception:
        ids_path.unlink(missing_ok=True)
        summary_path.unlink(missing_ok=True)
        raise
    stdout_drain = _StreamDrain(popen.stdout) if popen.stdout is not None else None
    stderr_drain = (
        _StreamDrain(popen.stderr, max_bytes=1024 * 1024)
        if popen.stderr is not None
        else None
    )
    return AsrArchiveProcess(
        popen=popen,
        ids_path=ids_path,
        output_path=output_path,
        summary_path=summary_path,
        stdout_drain=stdout_drain,
        stderr_drain=stderr_drain,
    )


def terminate_asr_archive_process(proc: AsrArchiveProcess) -> None:
    """Terminate the ASR archive subprocess (and its process group) and clean up temp files."""
    if proc.closed:
        return
    try:
        if proc.popen.poll() is None:
            try:
                os.killpg(proc.popen.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    proc.popen.terminate()
                except OSError:
                    pass
            try:
                proc.popen.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.popen.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    try:
                        proc.popen.kill()
                    except OSError:
                        pass
                try:
                    proc.popen.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    pass
    finally:
        if proc.stdout_drain is not None:
            proc.stdout_drain.close()
        elif proc.popen.stdout is not None:
            try:
                proc.popen.stdout.close()
            except Exception:
                pass
        if proc.stderr_drain is not None:
            proc.stderr_drain.close()
        elif proc.popen.stderr is not None:
            try:
                proc.popen.stderr.close()
            except Exception:
                pass
        proc.ids_path.unlink(missing_ok=True)
        if proc.summary_path is not None:
            proc.summary_path.unlink(missing_ok=True)
        proc.closed = True


async def wait_asr_archive_finished(
    proc: AsrArchiveProcess,
    *,
    deadline: float,
    poll_interval_s: float = 0.05,
) -> AsrArchiveSummary:
    """Wait for the ASR archive subprocess within ``deadline`` and return its parsed summary."""
    while True:
        if proc.closed:
            raise BackupTimeoutError("archive-asr-jobs was terminated by lock-phase watchdog")
        rc = proc.popen.poll()
        if rc is not None:
            if proc.closed or time.monotonic() >= deadline:
                terminate_asr_archive_process(proc)
                raise BackupTimeoutError("archive-asr-jobs exceeded lock-phase hard deadline")
            out_bytes = b""
            if proc.stdout_drain is not None:
                out_bytes = proc.stdout_drain.read_all()
            elif proc.popen.stdout is not None:
                try:
                    out_bytes = proc.popen.stdout.read()
                except (OSError, ValueError):
                    out_bytes = b""
            err_bytes = b""
            if proc.stderr_drain is not None:
                err_bytes = proc.stderr_drain.read_all()
            elif proc.popen.stderr is not None:
                try:
                    err_bytes = proc.popen.stderr.read()
                except (OSError, ValueError):
                    err_bytes = b""
            summary_raw = b""
            if proc.summary_path is not None and proc.summary_path.is_file():
                try:
                    summary_raw = proc.summary_path.read_bytes()
                except OSError:
                    summary_raw = b""
            terminate_asr_archive_process(proc)
            if rc != 0:
                err = err_bytes.decode("utf-8", errors="replace").strip()
                raise ConsistentBackupError(
                    f"archive-asr-jobs subprocess failed (rc={rc}): {err}"
                )
            json_source = summary_raw if summary_raw.strip() else out_bytes
            try:
                payload = json.loads(json_source.decode("utf-8"))
            except Exception as exc:
                raise ConsistentBackupError(
                    f"archive-asr-jobs returned invalid JSON summary: {exc}"
                ) from exc
            return AsrArchiveSummary(
                archive_path=Path(payload["archive_path"]),
                size_bytes=int(payload["size_bytes"]),
                sha256=str(payload["sha256"]),
                included_job_ids=tuple(str(x) for x in (payload.get("included_job_ids") or ())),
                included_usage_ledger=bool(payload.get("included_usage_ledger")),
            )
        if time.monotonic() >= deadline:
            terminate_asr_archive_process(proc)
            raise BackupTimeoutError("archive-asr-jobs exceeded lock-phase hard deadline")
        await asyncio.sleep(poll_interval_s)


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
        *,
        deadline: float | None = None,
        on_stream_open: Callable[[Any], None] | None = None,
        on_stream_close: Callable[[Any], None] | None = None,
    ) -> ArchivedObjectRecord:
        if deadline is not None:
            _remaining_or_raise(deadline, f"get_object({item.bucket}/{item.key})")
        body, content_type = _fetch_s3_object_stream(
            s3_client, item.bucket, item.key, item.version_id
        )
        if on_stream_open is not None:
            on_stream_open(body)
        hasher = hashlib.sha256()
        total = 0
        fd, tmp_name = tempfile.mkstemp(prefix="obj-", dir=str(self.tmp_dir))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out_fh:
                try:
                    while True:
                        if deadline is not None:
                            _remaining_or_raise(deadline, f"read_object({item.bucket}/{item.key})")
                        chunk = body.read(STREAM_CHUNK_BYTES)
                        if deadline is not None:
                            _remaining_or_raise(deadline, f"read_object({item.bucket}/{item.key})")
                        if not chunk:
                            break
                        total += len(chunk)
                        hasher.update(chunk)
                        out_fh.write(chunk)
                finally:
                    if on_stream_close is not None:
                        on_stream_close(body)
                    close_fn = getattr(body, "close", None)
                    if callable(close_fn):
                        close_fn()
            if deadline is not None:
                _remaining_or_raise(deadline, f"install_object({item.bucket}/{item.key})")
            digest = hasher.hexdigest()
            dst = self.object_path(digest)
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Always atomically install the freshly verified bytes rather than trusting
            # any pre-existing pool file solely by byte length.
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
        seen: set[tuple[str, str]] = set()
        for rec in self.records:
            pair = (str(rec.bucket), str(rec.key).lstrip("/"))
            if pair in seen:
                raise ConsistentBackupError(
                    f"Duplicate archived object in mirror pool manifest: {pair[0]}/{pair[1]}"
                )
            seen.add(pair)
        return tuple(self.records)


class TarGzObjectSink:
    """Standalone Core sink: writes objects + ``_minio_manifest.json`` into ``minio-objects.tar.gz``."""

    def __init__(
        self,
        tar_path: Path,
        *,
        tmp_dir: Path | None = None,
        manifest_extra: Mapping[str, Any] | None = None,
    ) -> None:
        self.tar_path = tar_path
        self.tar_path.parent.mkdir(parents=True, exist_ok=True)
        self._stage_dir = Path(
            tempfile.mkdtemp(prefix="minio-tar-stage-", dir=str(tmp_dir or tar_path.parent))
        )
        self._tmp_tar = tar_path.with_name(f".{tar_path.name}.tmp.{os.getpid()}")
        self._tf = tarfile.open(self._tmp_tar, "w:gz", format=tarfile.PAX_FORMAT)
        self._manifest_extra = dict(manifest_extra or {})
        self.records: list[ArchivedObjectRecord] = []
        self._seen_pairs: set[tuple[str, str]] = set()
        self._closed = False

    def archive_object(
        self,
        s3_client: Any,
        item: SelectedObjectVersion,
        *,
        deadline: float | None = None,
        on_stream_open: Callable[[Any], None] | None = None,
        on_stream_close: Callable[[Any], None] | None = None,
    ) -> ArchivedObjectRecord:
        pair = (str(item.bucket), str(item.key).lstrip("/"))
        if pair in self._seen_pairs:
            raise ConsistentBackupError(
                f"Duplicate archived object for tar sink: {pair[0]}/{pair[1]}"
            )
        if deadline is not None:
            _remaining_or_raise(deadline, f"get_object({item.bucket}/{item.key})")
        body, content_type = _fetch_s3_object_stream(
            s3_client, item.bucket, item.key, item.version_id
        )
        if on_stream_open is not None:
            on_stream_open(body)
        hasher = hashlib.sha256()
        total = 0
        tmp_obj = self._stage_dir / f"obj-{len(self.records)}.bin"
        try:
            with tmp_obj.open("wb") as out_fh:
                try:
                    while True:
                        if deadline is not None:
                            _remaining_or_raise(deadline, f"read_object({item.bucket}/{item.key})")
                        chunk = body.read(STREAM_CHUNK_BYTES)
                        if deadline is not None:
                            _remaining_or_raise(deadline, f"read_object({item.bucket}/{item.key})")
                        if not chunk:
                            break
                        total += len(chunk)
                        hasher.update(chunk)
                        out_fh.write(chunk)
                finally:
                    if on_stream_close is not None:
                        on_stream_close(body)
                    close_fn = getattr(body, "close", None)
                    if callable(close_fn):
                        close_fn()
            if deadline is not None:
                _remaining_or_raise(deadline, f"tar_add({item.bucket}/{item.key})")
            digest = hasher.hexdigest()
            arcname = f"objects/{item.bucket}/{item.key.lstrip('/')}"
            info = tarfile.TarInfo(name=arcname)
            info.size = total
            info.mtime = 0
            info.mode = 0o600
            info.pax_headers = {"DALTON.content_type": content_type}
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
        self._seen_pairs.add(pair)
        self.records.append(rec)
        return rec

    def finalize(self) -> tuple[ArchivedObjectRecord, ...]:
        if self._closed:
            return tuple(self.records)
        seen: set[tuple[str, str]] = set()
        for rec in self.records:
            pair = (str(rec.bucket), str(rec.key).lstrip("/"))
            if pair in seen:
                raise ConsistentBackupError(
                    f"Duplicate archived object in tar manifest: {pair[0]}/{pair[1]}"
                )
            seen.add(pair)
        manifest_payload: dict[str, Any] = {
            "version": 2,
            "buckets": sorted({r.bucket for r in self.records}),
            "bucket_count": len({r.bucket for r in self.records}),
            "object_count": len(self.records),
            "total_bytes": sum(r.size for r in self.records),
            "objects": [r.to_dict() for r in self.records],
        }
        for k, v in self._manifest_extra.items():
            if k not in ("version", "objects", "object_count", "total_bytes"):
                manifest_payload[k] = v
        if "buckets" in self._manifest_extra:
            manifest_payload["buckets"] = list(self._manifest_extra["buckets"])
            manifest_payload["bucket_count"] = len(manifest_payload["buckets"])
        manifest_bytes = (
            json.dumps(
                manifest_payload,
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
# End-to-end Step 1-4 orchestrator & independent lock-phase watchdog
# ---------------------------------------------------------------------------


def _run_in_daemon_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> asyncio.Future[Any]:
    """Run blocking ``fn`` in a daemon thread so watchdog cancellation never stalls ``asyncio.run`` teardown."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()

    def _worker() -> None:
        try:
            res = fn(*args, **kwargs)
        except BaseException as exc:
            if not fut.done() and not loop.is_closed():
                loop.call_soon_threadsafe(lambda e=exc: None if fut.done() else fut.set_exception(e))
        else:
            if not fut.done() and not loop.is_closed():
                loop.call_soon_threadsafe(lambda r=res: None if fut.done() else fut.set_result(r))

    threading.Thread(
        target=_worker,
        name=f"consistent-backup-io-{getattr(fn, '__name__', 'call')}",
        daemon=True,
    ).start()
    return fut


def _force_close_pgconn_sync(conn: Any) -> None:
    """Synchronously abort and close underlying libpq/force_close handles without blocking on SQL."""
    pgconn = getattr(conn, "pgconn", None)
    if pgconn is not None:
        cancel_fn = getattr(pgconn, "cancel", None)
        if callable(cancel_fn):
            try:
                cancel_fn()
            except Exception:
                pass
        finish_fn = getattr(pgconn, "finish", None)
        if callable(finish_fn):
            try:
                finish_fn()
            except Exception:
                pass

    force_close = getattr(conn, "force_close", None)
    if callable(force_close):
        try:
            force_close()
        except Exception:
            pass


def _force_close_db_connection(conn: Any, loop: asyncio.AbstractEventLoop | None) -> None:
    """Best-effort immediate closure of the DB control session from a non-event-loop thread.

    Drops both GC session advisory locks and aborts any open snapshot transaction at
    PostgreSQL immediately, even if the main or worker thread is stalled in I/O.
    """
    _force_close_pgconn_sync(conn)

    close_fn = getattr(conn, "close", None)
    if not callable(close_fn):
        return
    if inspect.iscoroutinefunction(close_fn):
        if loop is not None and loop.is_running():
            try:
                fut = asyncio.run_coroutine_threadsafe(close_fn(), loop)
                fut.result(timeout=1.0)
            except Exception:
                pass
    else:
        try:
            res = close_fn()
            if asyncio.iscoroutine(res):
                if loop is not None and loop.is_running():
                    try:
                        fut = asyncio.run_coroutine_threadsafe(res, loop)
                        fut.result(timeout=1.0)
                    except Exception:
                        pass
                else:
                    res.close()
        except Exception:
            pass


async def _force_close_db_connection_async(conn: Any) -> None:
    """Best-effort immediate closure of the DB control session from inside the event loop.

    Never waits for ``ROLLBACK`` or ``pg_advisory_unlock``: closing the session drops both
    advisory locks and aborts any open transaction atomically on PostgreSQL.
    """
    _force_close_pgconn_sync(conn)

    close_fn = getattr(conn, "close", None)
    if not callable(close_fn):
        return
    try:
        if inspect.iscoroutinefunction(close_fn):
            await asyncio.wait_for(close_fn(), timeout=1.0)
        else:
            res = close_fn()
            if asyncio.iscoroutine(res):
                await asyncio.wait_for(res, timeout=1.0)
    except Exception:
        pass


def _close_stream_isolated(body: Any, *, join_timeout_s: float = 0.05) -> None:
    """Close an object response stream in a daemon thread so a blocking ``close()`` never stalls teardown."""
    close_fn = getattr(body, "close", None)
    if not callable(close_fn):
        return

    def _closer() -> None:
        try:
            close_fn()
        except Exception:
            pass

    t = threading.Thread(target=_closer, name="consistent-backup-stream-close", daemon=True)
    t.start()
    t.join(timeout=join_timeout_s)


class _LockPhaseWatchdog:
    """Independent daemon thread enforcing the hard GC lock-phase deadline.

    On timeout, immediately executes in strict order:
    1. Closes the PostgreSQL control session FIRST (dropping both GC advisory locks).
    2. Terminates active ``pg_dump`` and ASR archive process groups.
    3. Closes any active S3 response streams LAST in isolated daemon threads so a
       blocking ``close()`` can never delay DB session closure or process termination.
    4. Cancels the asyncio lock-phase task so ``run_consistent_backup`` raises
       :class:`BackupTimeoutError` promptly without waiting for blocked I/O.
    """

    def __init__(
        self,
        *,
        deadline: float,
        conn: Any,
        loop: asyncio.AbstractEventLoop,
        task: asyncio.Task[Any],
    ) -> None:
        self._deadline = deadline
        self._conn = conn
        self._loop = loop
        self._task = task
        self._mu = threading.Lock()
        self._stop_event = threading.Event()
        self._disarmed = False
        self._phase = "lock_phase"
        self._dump_terminator: Callable[[], None] | None = None
        self._asr_terminator: Callable[[], None] | None = None
        self._active_streams: list[Any] = []
        self.timed_out = False
        self.timeout_phase = "lock_phase"
        self.db_closed_by_watchdog = False
        self._thread = threading.Thread(
            target=self._run,
            name="consistent-backup-lock-watchdog",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def set_phase(self, phase: str) -> None:
        with self._mu:
            self._phase = phase

    def set_dump_terminator(self, fn: Callable[[], None] | None) -> None:
        with self._mu:
            self._dump_terminator = fn

    def set_asr_terminator(self, fn: Callable[[], None] | None) -> None:
        with self._mu:
            self._asr_terminator = fn

    def register_stream(self, body: Any) -> None:
        with self._mu:
            self._active_streams.append(body)

    def unregister_stream(self, body: Any) -> None:
        with self._mu:
            try:
                self._active_streams.remove(body)
            except ValueError:
                pass

    def disarm(self) -> None:
        with self._mu:
            self._disarmed = True
        self._stop_event.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        rem = self._deadline - time.monotonic()
        if rem > 0:
            self._stop_event.wait(timeout=rem)
        with self._mu:
            if self._disarmed:
                return
            self.timed_out = True
            self.timeout_phase = self._phase
            dump_term = self._dump_terminator
            asr_term = self._asr_terminator
            streams = list(self._active_streams)
            task = self._task

        logger.error(
            "Consistent backup lock-phase watchdog expired during %s; closing DB session first, then terminating child process groups and streams",
            self.timeout_phase,
        )
        # 1. Close DB control session FIRST so both GC advisory locks drop immediately.
        _force_close_db_connection(self._conn, self._loop)
        self.db_closed_by_watchdog = True
        # 2. Terminate child process groups (pg_dump & ASR archiver).
        if dump_term is not None:
            try:
                dump_term()
            except Exception as exc:
                logger.warning("Watchdog failed terminating pg_dump: %s", exc)
        if asr_term is not None:
            try:
                asr_term()
            except Exception as exc:
                logger.warning("Watchdog failed terminating ASR archiver: %s", exc)
        # 3. Close registered object streams LAST in isolated daemon threads so a blocking close()
        #    cannot hold locks or stall watchdog completion.
        for body in streams:
            _close_stream_isolated(body)
        # 4. Cancel the asyncio lock-phase task promptly.
        if self._loop.is_running():
            try:
                self._loop.call_soon_threadsafe(task.cancel)
            except Exception:
                pass


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
    lock_phase_timeout_s: float | None = None,
    pg_dump_timeout_s: float | None = None,
    mirror_timeout_s: float | None = None,
    snapshot_import_timeout_s: float | None = None,
    env: Mapping[str, str] | None = None,
    after_snapshot_commit_hook: Callable[[str, SnapshotRefInventory], Any] | None = None,
    wait_pg_dump_imported_fn: Callable[[Any], Any] | None = None,
    wait_pg_dump_finished_fn: Callable[[Any], Any] | None = None,
    terminate_pg_dump_fn: Callable[[Any], Any] | None = None,
) -> ConsistentBackupResult:
    """Execute the full §3.1 Steps 1-4 consistent backup protocol with hard deadlines.

    - Enforces configurable hard deadlines on the entire GC lock-holding phase (default 2h)
      via an independent watchdog thread that closes the DB control session and terminates
      ``pg_dump`` and ASR archive process groups on expiry.
    - Also enforces hard deadlines on ``pg_dump`` (default 2h), snapshot import (default 120s),
      and full-bucket mirroring (separate default 4h).
    - Commits the ``REPEATABLE READ`` snapshot export transaction immediately after
      ``pg_dump`` imports the snapshot and ``enumerate_snapshot_refs`` finishes.
    - Enumerates ALL DB references independently of ``bucket_specs`` and fails loudly if
      the DB contains references for a bucket role without configured storage.
    - Releases both GC advisory locks before full-bucket versioned mirroring (or keeps
      them through unversioned bucket downloads, still capped by ``lock_phase_timeout_s``).
    - On timeout or error: terminates ``pg_dump`` & ASR archiver, rolls back any open snapshot
      txn, releases both GC advisory locks (or closes the control session), and propagates.
    """
    eff_lock_timeout = (
        float(lock_phase_timeout_s)
        if lock_phase_timeout_s is not None
        else _env_float(env, "DALTON_BACKUP_LOCK_TIMEOUT_S", DEFAULT_LOCK_PHASE_TIMEOUT_S)
    )
    eff_dump_timeout = (
        float(pg_dump_timeout_s)
        if pg_dump_timeout_s is not None
        else _env_float(env, "DALTON_BACKUP_PG_DUMP_TIMEOUT_S", DEFAULT_PG_DUMP_TIMEOUT_S)
    )
    eff_mirror_timeout = (
        float(mirror_timeout_s)
        if mirror_timeout_s is not None
        else _env_float(env, "DALTON_BACKUP_MIRROR_TIMEOUT_S", DEFAULT_MIRROR_TIMEOUT_S)
    )
    eff_import_timeout = (
        float(snapshot_import_timeout_s)
        if snapshot_import_timeout_s is not None
        else _env_float(env, "DALTON_BACKUP_SNAPSHOT_IMPORT_TIMEOUT_S", DEFAULT_SNAPSHOT_IMPORT_TIMEOUT_S)
    )

    locks_held = False
    snapshot_open = False
    warnings: list[str] = []
    dump_handle: Any = None
    dump_finished = False
    asr_proc: AsrArchiveProcess | None = None
    watchdog: _LockPhaseWatchdog | None = None

    async def _release_locks_now(*, strict: bool = False, deadline: float | None = None) -> None:
        nonlocal locks_held
        if not locks_held:
            return
        if watchdog is not None and watchdog.db_closed_by_watchdog:
            locks_held = False
            if strict:
                raise BackupTimeoutError(
                    f"Consistent backup exceeded hard lock-phase deadline during {watchdog.timeout_phase}"
                )
            return
        try:
            unlock_timeout = 10.0
            if deadline is not None:
                unlock_timeout = min(
                    unlock_timeout,
                    _remaining_or_raise(deadline, "release_gc_advisory_locks"),
                )
            await asyncio.wait_for(release_gc_advisory_locks(conn), timeout=unlock_timeout)
        except BaseException as exc:
            # Close the dedicated control session immediately so locks are dropped even if
            # unlock stalled or failed; on the normal path (strict=True) abort the backup.
            if not (watchdog is not None and watchdog.db_closed_by_watchdog):
                await _force_close_db_connection_async(conn)
            locks_held = False
            if strict:
                if isinstance(exc, (TimeoutError, BackupTimeoutError)):
                    raise BackupTimeoutError(
                        "Failed releasing GC advisory locks within deadline; closed DB session"
                    ) from exc
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise ConsistentBackupError(
                    f"Failed releasing GC advisory locks; closed DB session: {exc}"
                ) from exc
            logger.warning("Failed releasing GC advisory locks (closed DB session): %s", exc)
        else:
            locks_held = False

    def _terminate_dump_if_running() -> None:
        if dump_handle is None or dump_finished:
            return
        if isinstance(dump_handle, PgDumpProcess):
            terminate_pg_dump_process(dump_handle)
        elif terminate_pg_dump_fn is not None:
            try:
                terminate_pg_dump_fn(dump_handle)
            except Exception as exc:
                logger.warning("Failed terminating pg_dump handle: %s", exc)

    def _terminate_asr_if_running() -> None:
        if asr_proc is not None and not asr_proc.closed:
            terminate_asr_archive_process(asr_proc)

    async def _run_lock_phase() -> tuple[
        str,
        SnapshotRefInventory,
        dict[str, tuple[str, str]],
        list[tuple[BucketArchiveSpec, VersionSelectionResult]],
        AsrArchiveSummary | None,
    ]:
        nonlocal locks_held, snapshot_open, dump_handle, dump_finished, asr_proc, watchdog

        loop = asyncio.get_running_loop()
        current_task = asyncio.current_task()
        assert current_task is not None

        # Start hard lock-phase watchdog BEFORE acquiring the first advisory lock so a stalled
        # DB session during or immediately after lock acquisition is still bounded and closed.
        lock_deadline = time.monotonic() + max(0.05, eff_lock_timeout)
        watchdog = _LockPhaseWatchdog(
            deadline=lock_deadline,
            conn=conn,
            loop=loop,
            task=current_task,
        )
        watchdog.set_phase("acquire_gc_advisory_locks")
        watchdog.set_dump_terminator(_terminate_dump_if_running)
        watchdog.set_asr_terminator(_terminate_asr_if_running)
        watchdog.start()

        # Step 1: hold both GC session advisory locks (bounded lock_timeout + active watchdog)
        locks_held = True
        try:
            await asyncio.wait_for(
                acquire_gc_advisory_locks(conn, lock_timeout=lock_timeout),
                timeout=_remaining_or_raise(lock_deadline, "acquire_gc_advisory_locks"),
            )
        except TimeoutError as exc:
            raise BackupTimeoutError("acquire_gc_advisory_locks exceeded hard deadline") from exc

        # Step 2: REPEATABLE READ READ ONLY + pg_export_snapshot()
        watchdog.set_phase("begin_exported_snapshot")
        snapshot_id = await asyncio.wait_for(
            begin_exported_snapshot(conn),
            timeout=_remaining_or_raise(lock_deadline, "begin_exported_snapshot"),
        )
        snapshot_open = True

        # Enumerate ALL live DB references unconditionally (independent of bucket_specs)
        watchdog.set_phase("enumerate_snapshot_refs")
        inventory = await asyncio.wait_for(
            enumerate_snapshot_refs(
                conn,
                env=env,
                include_main=True,
                include_aux=True,
            ),
            timeout=_remaining_or_raise(lock_deadline, "enumerate_snapshot_refs"),
        )

        # Fail fast if DB references exist for a bucket role without configured bucket specs
        configured_roles = {role for spec in bucket_specs for role in spec.roles}
        refs_by_role: dict[str, int] = {}
        for ref in inventory.references:
            refs_by_role[ref.bucket_role] = refs_by_role.get(ref.bucket_role, 0) + 1
        for role, count in sorted(refs_by_role.items()):
            if count > 0 and role not in configured_roles:
                raise MissingBucketConfigError(role, count)

        # Start pg_dump --snapshot=<snapshot_id> while the snapshot txn is still open
        watchdog.set_phase("pg_dump_start")
        dump_deadline = time.monotonic() + max(0.05, eff_dump_timeout)
        dump_res = pg_dump_fn(snapshot_id, pg_dump_path)
        if asyncio.iscoroutine(dump_res):
            rem = min(
                _remaining_or_raise(lock_deadline, "pg_dump_start"),
                _remaining_or_raise(dump_deadline, "pg_dump_start"),
            )
            try:
                dump_handle = await asyncio.wait_for(dump_res, timeout=rem)
            except TimeoutError as exc:
                raise BackupTimeoutError("pg_dump exceeded hard deadline") from exc
        else:
            dump_handle = dump_res

        # Wait ONLY until pg_dump has imported the snapshot, then immediately COMMIT export txn
        watchdog.set_phase("wait_pg_dump_imported")
        if wait_pg_dump_imported_fn is not None:
            rem = min(
                eff_import_timeout,
                _remaining_or_raise(lock_deadline, "wait_pg_dump_imported"),
                _remaining_or_raise(dump_deadline, "wait_pg_dump_imported"),
            )
            wait_imp = wait_pg_dump_imported_fn(dump_handle)
            if asyncio.iscoroutine(wait_imp):
                try:
                    await asyncio.wait_for(wait_imp, timeout=rem)
                except TimeoutError as exc:
                    raise BackupTimeoutError("wait_pg_dump_imported exceeded hard deadline") from exc
        elif isinstance(dump_handle, PgDumpProcess):
            rem = min(
                eff_import_timeout,
                _remaining_or_raise(lock_deadline, "wait_pg_dump_imported"),
                _remaining_or_raise(dump_deadline, "wait_pg_dump_imported"),
            )
            await wait_pg_dump_imported(dump_handle, timeout_s=rem)
        elif wait_pg_dump_finished_fn is None:
            # Synchronous pg_dump_fn completed inline
            dump_finished = True

        # Snapshot imported & refs enumerated -> COMMIT snapshot txn immediately (locks stay held)
        watchdog.set_phase("commit_exported_snapshot")
        await asyncio.wait_for(
            commit_exported_snapshot(conn),
            timeout=_remaining_or_raise(lock_deadline, "commit_exported_snapshot"),
        )
        snapshot_open = False

        if after_snapshot_commit_hook is not None:
            watchdog.set_phase("after_snapshot_commit_hook")
            hook_res = after_snapshot_commit_hook(snapshot_id, inventory)
            if asyncio.iscoroutine(hook_res):
                await asyncio.wait_for(
                    hook_res,
                    timeout=_remaining_or_raise(lock_deadline, "after_snapshot_commit_hook"),
                )

        # Step 3a: list_object_versions per bucket & select versions under the two rules
        # Run blocking S3 calls in worker threads so the asyncio loop stays responsive to the watchdog.
        keys_by_role = inventory.keys_by_bucket_role()
        bucket_targets: dict[str, tuple[str, str]] = {}
        selections: list[tuple[BucketArchiveSpec, VersionSelectionResult]] = []

        for spec in bucket_specs:
            _remaining_or_raise(lock_deadline, f"select_versions({spec.bucket})")
            snap_keys_for_bucket: set[str] = set()
            for role in spec.roles:
                bucket_targets[role] = (spec.bucket, spec.key_prefix.strip("/"))
                for logical_key in keys_by_role.get(role, ()):
                    snap_keys_for_bucket.add(_apply_bucket_prefix(logical_key, spec.key_prefix))

            if spec.versioning_enabled is not None:
                ver_enabled = spec.versioning_enabled
            else:
                watchdog.set_phase(f"detect_bucket_versioning({spec.bucket})")
                ver_enabled = await _run_in_daemon_thread(
                    detect_bucket_versioning, spec.s3_client, spec.bucket
                )

            if ver_enabled:
                watchdog.set_phase(f"list_object_versions({spec.bucket})")
                vers, dms = await _run_in_daemon_thread(
                    list_all_object_versions,
                    spec.s3_client,
                    spec.bucket,
                    prefix=spec.key_prefix,
                    deadline=lock_deadline,
                )
            else:
                watchdog.set_phase(f"list_objects_v2({spec.bucket})")
                vers = await _run_in_daemon_thread(
                    list_all_current_objects,
                    spec.s3_client,
                    spec.bucket,
                    prefix=spec.key_prefix,
                    deadline=lock_deadline,
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

        # Deduplicate across overlapping specs (e.g. shared bucket with empty main prefix
        # and non-empty chat_upload prefix) by (endpoint, bucket, full_key) before archiving.
        selections = deduplicate_selections(selections)

        # Step 3b: pack terminal ASR jobs only + asr_usage.jsonl in its own process group
        asr_summary: AsrArchiveSummary | None = None
        if asr_jobs_dir is not None and asr_archive_path is not None:
            watchdog.set_phase("archive_terminal_asr_jobs")
            _remaining_or_raise(lock_deadline, "archive_terminal_asr_jobs")
            asr_proc = start_asr_archive_process(
                asr_jobs_dir,
                inventory.terminal_asr_job_ids,
                asr_archive_path,
            )
            asr_summary = await wait_asr_archive_finished(asr_proc, deadline=lock_deadline)

        # Step 4a: wait for pg_dump to finish within hard deadline
        watchdog.set_phase("wait_pg_dump_finished")
        if wait_pg_dump_finished_fn is not None:
            rem = min(
                _remaining_or_raise(lock_deadline, "wait_pg_dump_finished"),
                _remaining_or_raise(dump_deadline, "wait_pg_dump_finished"),
            )
            wait_fin = wait_pg_dump_finished_fn(dump_handle)
            if asyncio.iscoroutine(wait_fin):
                try:
                    await asyncio.wait_for(wait_fin, timeout=rem)
                except TimeoutError as exc:
                    raise BackupTimeoutError("wait_pg_dump_finished exceeded hard deadline") from exc
            dump_finished = True
        elif isinstance(dump_handle, PgDumpProcess):
            rem = min(
                _remaining_or_raise(lock_deadline, "wait_pg_dump_finished"),
                _remaining_or_raise(dump_deadline, "wait_pg_dump_finished"),
            )
            await wait_pg_dump_finished(dump_handle, timeout_s=rem)
            dump_finished = True

        # Step 4b (unversioned only): unversioned buckets must download while GC locks are held,
        # bounded strictly by lock_deadline (checked inside every chunk read AND enforced by watchdog).
        unversioned_selections = [(sp, sel) for sp, sel in selections if not sel.versioning_enabled]
        for spec, sel in unversioned_selections:
            for obj in sel.selected:
                watchdog.set_phase(f"mirror_unversioned({spec.bucket}/{obj.key})")
                _remaining_or_raise(lock_deadline, f"mirror_unversioned({spec.bucket}/{obj.key})")
                await _run_in_daemon_thread(
                    object_sink.archive_object,
                    spec.s3_client,
                    obj,
                    deadline=lock_deadline,
                    on_stream_open=watchdog.register_stream,
                    on_stream_close=watchdog.unregister_stream,
                )

        # Release both GC session advisory locks BEFORE disarming watchdog and before versioned mirroring.
        # Any unlock failure closes the dedicated DB session and aborts the backup instead of continuing.
        watchdog.set_phase("release_gc_advisory_locks")
        await _release_locks_now(strict=True, deadline=lock_deadline)
        watchdog.disarm()
        return snapshot_id, inventory, bucket_targets, selections, asr_summary

    try:
        lock_task = asyncio.create_task(_run_lock_phase())
        try:
            snapshot_id, inventory, bucket_targets, selections, asr_summary = await lock_task
        except asyncio.CancelledError as exc:
            if watchdog is not None and watchdog.timed_out:
                raise BackupTimeoutError(
                    f"Consistent backup exceeded hard lock-phase deadline during {watchdog.timeout_phase}"
                ) from exc
            raise

        # Step 4b (versioned): locks already released; mirror pinned VersionIds under mirror_deadline
        versioned_selections = [(sp, sel) for sp, sel in selections if sel.versioning_enabled]
        mirror_deadline = time.monotonic() + max(0.05, eff_mirror_timeout)
        for spec, sel in versioned_selections:
            for obj in sel.selected:
                _remaining_or_raise(mirror_deadline, f"mirror_versioned({spec.bucket}/{obj.key})")
                await asyncio.to_thread(
                    object_sink.archive_object,
                    spec.s3_client,
                    obj,
                    deadline=mirror_deadline,
                )

        manifest_records = object_sink.finalize()
    except BaseException as exc:
        # Close DB session FIRST without waiting for ROLLBACK or pg_advisory_unlock so
        # both session advisory locks and any open snapshot txn drop immediately even if
        # the database connection is stalled. Keep the watchdog armed until the session is closed.
        if (locks_held or snapshot_open) and not (
            watchdog is not None and watchdog.db_closed_by_watchdog
        ):
            await _force_close_db_connection_async(conn)
            locks_held = False
            snapshot_open = False
        _terminate_dump_if_running()
        _terminate_asr_if_running()
        if watchdog is not None:
            watchdog.disarm()
        if isinstance(object_sink, TarGzObjectSink):
            object_sink.abort()
        if watchdog is not None and watchdog.timed_out and not isinstance(exc, BackupTimeoutError):
            raise BackupTimeoutError(
                f"Consistent backup exceeded hard lock-phase deadline during {watchdog.timeout_phase}"
            ) from exc
        raise
    finally:
        if watchdog is not None:
            watchdog.disarm()
        _terminate_dump_if_running()
        _terminate_asr_if_running()
        await _release_locks_now()

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
    workspace_uploads_requeued: int = 0
    workspace_uploads_aborted: int = 0
    transcriptions_cancelled: int = 0
    transcriptions_failed: int = 0
    asr_dirs_failed: int = 0

    def to_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


def converge_restored_asr_jobs_dir(asr_jobs_dir: Path) -> int:
    """Mark any non-terminal restored ASR job directory on disk ``failed`` BEFORE service startup.

    Ensures ``AsrJobManager.recover_incomplete_jobs()`` never auto-resumes restored in-flight
    or ``indeterminate`` jobs (forcing explicit ``/resubmit`` instead).
    """
    if not asr_jobs_dir.is_dir():
        return 0
    from src.dalton.transcription_service.jobs import atomic_write_json_locked

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    converged = 0
    for entry in sorted(asr_jobs_dir.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        job_id = entry.name
        if not _SAFE_JOB_ID_RE.fullmatch(job_id):
            continue
        state_path = entry / "state.json"
        if not state_path.is_file() or state_path.is_symlink():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(state, dict):
            continue
        cur_status = str(state.get("status") or "").strip().lower()
        if cur_status in TERMINAL_ASR_ARCHIVE_STATUSES:
            continue
        updated = dict(state)
        updated["status"] = "failed"
        updated["stage"] = "failed"
        updated["error_category"] = updated.get("error_category") or "restored_inflight"
        updated["error_message"] = RESTORED_INFLIGHT_ERROR_MESSAGE
        updated["restored_no_resume"] = True
        updated["completed_at"] = updated.get("completed_at") or now_iso
        updated["updated_at"] = now_iso
        updated["version"] = int(updated.get("version") or 0) + 1
        if atomic_write_json_locked(state_path, updated, label="restored-state"):
            converged += 1
    return converged


async def reconcile_restored_inflight_states(
    conn: Any,
    *,
    inspect_chat_upload_object_fn: Callable[[str], tuple[int, str] | None] | None = None,
    inspect_workspace_upload_object_fn: Callable[[str], tuple[int, str] | None] | None = None,
    asr_jobs_dir: Path | None = None,
    restored_before: dt.datetime | None = None,
) -> InflightReconcileSummary:
    """Converge all inflight DB rows (and optional restored ``asr_jobs`` dir) BEFORE services start.

    Must be executed after DB/object/asr-jobs restore and BEFORE starting backend or
    transcription services to accept new submissions.

    Follows the unified concurrency pattern:
    - Reads ``completing`` ``dalton_chat_file_uploads`` and ``pulling`` ``workspace_file_uploads``
      rows first without holding a DB lock;
    - Performs object-storage size + SHA-256 verification OUTSIDE any DB transaction/lock;
    - Re-locks rows ``FOR UPDATE`` inside a DB transaction, re-checks state, and commits.
    """
    drive_aborted = 0
    att_aborted = 0
    chat_promoted = 0
    chat_aborted = 0
    ws_requeued = 0
    ws_aborted = 0
    asr_cancelled = 0
    asr_failed = 0
    asr_dirs_failed = converge_restored_asr_jobs_dir(asr_jobs_dir) if asr_jobs_dir is not None else 0

    # Phase 1: discover `completing` chat uploads & `pulling` workspace uploads without holding a long DB transaction
    completing_candidates: list[tuple[str, str, int, str | None, str | None]] = []
    pulling_ws_candidates: list[tuple[str, str, int, str | None]] = []
    has_chat_uploads = False
    has_workspace_uploads = False
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
            has_workspace_uploads = await _table_exists(cur, "public.workspace_file_uploads")
            if has_workspace_uploads:
                await cur.execute(
                    "SELECT upload_id, object_key, size, sha256 "
                    "FROM workspace_file_uploads "
                    "WHERE cleaned_at IS NULL AND state = 'pulling'"
                )
                for row in await cur.fetchall():
                    uid, okey, sz, sha = _extract_tuple(row, 4)
                    pulling_ws_candidates.append(
                        (
                            str(uid),
                            str(okey or "").strip(),
                            int(sz),
                            str(sha).strip().lower() if sha else None,
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

    verified_pulling_ws_ids: list[str] = []
    if pulling_ws_candidates and inspect_workspace_upload_object_fn is not None:
        for uid, okey, expected_size, expected_sha in pulling_ws_candidates:
            if not okey:
                continue
            res = inspect_workspace_upload_object_fn(okey)
            if asyncio.iscoroutine(res):
                res = await res
            if res is None:
                continue
            obj_size, obj_sha = int(res[0]), str(res[1]).strip().lower()
            if obj_size != expected_size:
                continue
            if expected_sha and obj_sha != expected_sha:
                continue
            verified_pulling_ws_ids.append(uid)

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

            # 3b. workspace_file_uploads: re-queue verified `pulling` rows for clean sweep retry
            #     (clearing pre-restore lease & placing_inode); explicitly abort all remaining
            #     open rows (`uploading`, `completing`, and unverified/missing `pulling`).
            if has_workspace_uploads:
                requeued_ids: list[str] = []
                for uid in verified_pulling_ws_ids:
                    await cur.execute(
                        """
                        SELECT upload_id FROM workspace_file_uploads
                         WHERE upload_id = %s
                           AND cleaned_at IS NULL
                           AND state = 'pulling'
                           FOR UPDATE
                        """,
                        (uid,),
                    )
                    if await cur.fetchone() is not None:
                        await cur.execute(
                            """
                            UPDATE workspace_file_uploads
                               SET lease_until = NULL,
                                   placing_inode = NULL,
                                   attempts = 0,
                                   error = NULL,
                                   updated_at = NOW()
                             WHERE upload_id = %s
                               AND cleaned_at IS NULL
                               AND state = 'pulling'
                            """,
                            (uid,),
                        )
                        if int(cur.rowcount or 0) > 0:
                            ws_requeued += int(cur.rowcount or 0)
                            requeued_ids.append(uid)

                if requeued_ids:
                    await cur.execute(
                        """
                        UPDATE workspace_file_uploads
                           SET state = 'aborted',
                               lease_until = NULL,
                               placing_inode = NULL,
                               error = COALESCE(error, 'aborted_after_restore'),
                               updated_at = NOW()
                         WHERE cleaned_at IS NULL
                           AND state IN ('uploading', 'completing', 'pulling')
                           AND NOT (upload_id::text = ANY(%s))
                        """,
                        (requeued_ids,),
                    )
                else:
                    await cur.execute(
                        """
                        UPDATE workspace_file_uploads
                           SET state = 'aborted',
                               lease_until = NULL,
                               placing_inode = NULL,
                               error = COALESCE(error, 'aborted_after_restore'),
                               updated_at = NOW()
                         WHERE cleaned_at IS NULL
                           AND state IN ('uploading', 'completing', 'pulling')
                        """
                    )
                ws_aborted = max(0, int(cur.rowcount or 0))

            # 4. dalton_transcription_jobs (includes `indeterminate` and `awaiting_upload` rows
            #    with persisted submit intent -> failed, resubmit only; persists
            #    usage.restored_no_resume = true so normal submit & /retry are rejected)
            if await _table_exists(cur, "public.dalton_transcription_jobs"):
                if restored_before is not None:
                    await cur.execute(
                        """
                        UPDATE dalton_transcription_jobs
                           SET status = 'failed',
                               stage = 'failed',
                               error_category = COALESCE(error_category, 'restored_inflight'),
                               error_message = %s,
                               usage = COALESCE(usage, '{}'::jsonb) || '{"restored_no_resume": true}'::jsonb,
                               completed_at = COALESCE(completed_at, NOW()),
                               updated_at = NOW(),
                               version = version + 1
                         WHERE (
                               status = ANY(%s)
                               OR (
                                   status = 'awaiting_upload'
                                   AND COALESCE((usage ->> 'submit_attempted')::boolean, FALSE) IS TRUE
                               )
                         )
                           AND created_at <= %s
                        """,
                        (
                            RESTORED_INFLIGHT_ERROR_MESSAGE,
                            list(RESTORED_NONTERMINAL_ASR_STATUSES),
                            restored_before,
                        ),
                    )
                else:
                    await cur.execute(
                        """
                        UPDATE dalton_transcription_jobs
                           SET status = 'failed',
                               stage = 'failed',
                               error_category = COALESCE(error_category, 'restored_inflight'),
                               error_message = %s,
                               usage = COALESCE(usage, '{}'::jsonb) || '{"restored_no_resume": true}'::jsonb,
                               completed_at = COALESCE(completed_at, NOW()),
                               updated_at = NOW(),
                               version = version + 1
                         WHERE (
                               status = ANY(%s)
                               OR (
                                   status = 'awaiting_upload'
                                   AND COALESCE((usage ->> 'submit_attempted')::boolean, FALSE) IS TRUE
                               )
                         )
                        """,
                        (RESTORED_INFLIGHT_ERROR_MESSAGE, list(RESTORED_NONTERMINAL_ASR_STATUSES)),
                    )
                asr_failed = max(0, int(cur.rowcount or 0))

                if restored_before is not None:
                    await cur.execute(
                        """
                        UPDATE dalton_transcription_jobs
                           SET status = 'cancelled',
                               stage = 'cancelled',
                               usage = COALESCE(usage, '{}'::jsonb) || '{"restored_no_resume": true}'::jsonb,
                               completed_at = COALESCE(completed_at, NOW()),
                               updated_at = NOW(),
                               version = version + 1
                         WHERE status = 'awaiting_upload'
                           AND COALESCE((usage -> 'upload' ->> 'completed')::boolean, FALSE) IS NOT TRUE
                           AND COALESCE((usage ->> 'submit_attempted')::boolean, FALSE) IS NOT TRUE
                           AND created_at <= %s
                        """,
                        (restored_before,),
                    )
                else:
                    await cur.execute(
                        """
                        UPDATE dalton_transcription_jobs
                           SET status = 'cancelled',
                               stage = 'cancelled',
                               usage = COALESCE(usage, '{}'::jsonb) || '{"restored_no_resume": true}'::jsonb,
                               completed_at = COALESCE(completed_at, NOW()),
                               updated_at = NOW(),
                               version = version + 1
                         WHERE status = 'awaiting_upload'
                           AND COALESCE((usage -> 'upload' ->> 'completed')::boolean, FALSE) IS NOT TRUE
                           AND COALESCE((usage ->> 'submit_attempted')::boolean, FALSE) IS NOT TRUE
                        """
                    )
                asr_cancelled = max(0, int(cur.rowcount or 0))

    return InflightReconcileSummary(
        drive_uploads_aborted=drive_aborted,
        chat_attachments_aborted=att_aborted,
        chat_uploads_promoted_ready=chat_promoted,
        chat_uploads_aborted=chat_aborted,
        workspace_uploads_requeued=ws_requeued,
        workspace_uploads_aborted=ws_aborted,
        transcriptions_cancelled=asr_cancelled,
        transcriptions_failed=asr_failed,
        asr_dirs_failed=asr_dirs_failed,
    )


def _default_chat_upload_inspector(object_key: str) -> tuple[int, str] | None:
    """Inspect a chat upload object using ``chat_upload_storage.compute_stored_object_digest``."""
    from src.dalton import chat_upload_storage

    if not chat_upload_storage.is_chat_upload_storage_configured():
        return None
    digest_result = chat_upload_storage.compute_stored_object_digest(object_key)
    if digest_result is None:
        return None
    size, sha = digest_result
    clean_sha = str(sha or "").strip().lower()
    if not clean_sha:
        return None
    return int(size), clean_sha


def _default_workspace_upload_inspector(object_key: str) -> tuple[int, str] | None:
    """Inspect a ``workspace_file_uploads`` object in main storage, returning ``(size, sha256)`` or ``None``."""
    specs = resolve_runtime_bucket_specs(os.environ)
    main_spec = next((sp for sp in specs if "main" in sp.roles), None)
    if main_spec is None:
        return None
    full_key = _apply_bucket_prefix(object_key, main_spec.key_prefix)
    try:
        body, _ct = _fetch_s3_object_stream(main_spec.s3_client, main_spec.bucket, full_key, None)
    except Exception:
        return None
    hasher = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = body.read(STREAM_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            hasher.update(chunk)
    except Exception:
        return None
    finally:
        close_fn = getattr(body, "close", None)
        if callable(close_fn):
            close_fn()
    return total, hasher.hexdigest()


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
        help=(
            "Converge inflight uploads and active/indeterminate transcription jobs after a "
            "database restore (must run BEFORE starting services)."
        ),
    )
    p_rec.add_argument(
        "--skip-object-check",
        action="store_true",
        help="Skip object-storage verification for completing chat uploads (abort them directly).",
    )
    p_rec.add_argument(
        "--asr-jobs-dir",
        default="",
        help="Optional restored asr_jobs directory whose non-terminal state.json files should be marked failed.",
    )
    p_rec.add_argument(
        "--restored-before",
        default="",
        help="Optional ISO-8601 timestamp upper bound (e.g. backup set created_at) to scope restored rows.",
    )

    p_asr = sub.add_parser(
        "archive-asr-jobs",
        help="Pack terminal ASR job directories and asr_usage.jsonl into a tar.gz archive.",
    )
    p_asr.add_argument("--asr-jobs-dir", required=True)
    p_asr.add_argument("--job-ids-json", required=True)
    p_asr.add_argument("--output", required=True)
    p_asr.add_argument(
        "--summary-out",
        default="",
        help="Optional path to write JSON summary atomically instead of stdout.",
    )

    return ap


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.cmd == "reconcile-restored-inflight-states":
        from src.server.database.pool import open_session_connection

        inspector = None if args.skip_object_check else _default_chat_upload_inspector
        ws_inspector = None if args.skip_object_check else _default_workspace_upload_inspector
        asr_dir = Path(args.asr_jobs_dir) if args.asr_jobs_dir else None
        restored_before: dt.datetime | None = None
        if args.restored_before:
            parsed = dt.datetime.fromisoformat(args.restored_before.strip().replace("Z", "+00:00"))
            restored_before = parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)

        async def _run() -> InflightReconcileSummary:
            conn = await open_session_connection()
            try:
                return await reconcile_restored_inflight_states(
                    conn,
                    inspect_chat_upload_object_fn=inspector,
                    inspect_workspace_upload_object_fn=ws_inspector,
                    asr_jobs_dir=asr_dir,
                    restored_before=restored_before,
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
        summary_json = json.dumps(
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
        if args.summary_out:
            summary_dest = Path(args.summary_out)
            summary_dest.parent.mkdir(parents=True, exist_ok=True)
            tmp_summary = summary_dest.with_name(f".{summary_dest.name}.tmp.{os.getpid()}")
            tmp_summary.write_text(summary_json + "\n", encoding="utf-8")
            os.replace(tmp_summary, summary_dest)
        else:
            print(summary_json)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
