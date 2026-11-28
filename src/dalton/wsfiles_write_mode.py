"""Level-1 write-inline-only routing and bounded upload-failure inline fallback.

Hooked into ``src/server/services/persistence/backup.py`` under
``# DALTON-HOOK(workspace-file-write-mode)``:

1. Level-1 rollback (``DALTON_WSFILES_WRITE_INLINE_ONLY=1`` /
   ``workspace_files.write_inline_only: true``):
   - Does NOT alter ``blobs_on``, ``scan_cap_bytes``, ``history_cap_bytes``,
     upload limits, or ``ScanRules`` fingerprints.
   - Regular files outside Git repositories (``repo_of(path) is None``) with
     ``size <= INLINE_MAX_BYTES`` (100 MiB) — including small ``<= 256 KiB``
     files that would otherwise enter the pack set — are written inline to
     PostgreSQL using bounded reads verified against scanned size and SHA-256.
   - Files ``> 100 MiB`` and files inside Git repositories (including ``.git``
     history) continue writing to the main object-storage bucket (standalone
     objects and packs).
   - Unchanged existing pointer/packed rows remain untouched and readable.

2. Bounded upload-failure inline fallback (when main storage is enabled):
   - Runs before ``hold_repos(...)`` in ``_sync_locked``.
   - Eligible items returned as ``UnsavedFile(path, "failed", size)`` by
     ``_persist_blobs`` or ``_persist_packed`` with ``repo_of(path) is None``
     and ``size <= INLINE_MAX_BYTES`` are written inline within a per-sync
     cumulative budget of 64 MiB (counted by ``entry.size`` and enforced on the
     actual read stream), logging a ``WARNING`` per recovered file.
   - Any file that grew or mutated after scan (over cap, size mismatch, or
     SHA-256 mismatch) is rejected without writing and recorded in
     ``result.unsaved`` as ``reason="changed"``.
   - Items over budget, ``> 100 MiB``, inside Git repositories, or with
     ``reason != "failed"`` (e.g. ``"changed"``) remain in ``result.unsaved``
     so Git repositories keep whole-repo ``hold_repos()`` protection and
     retriable items retry on the next sync.
   - Fallback writes never trigger row deletions (``gone={}``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from ptc_agent.core.paths import WorkspaceLayout
from ptc_agent.core.sandbox.files import FileOverCap
from src.dalton.storage_config import is_wsfiles_write_inline_only
from src.server.database.blob_keys import INLINE_MAX_BYTES
from src.server.services.persistence import blobs as _blobs
from src.server.services.persistence._rows import (
    _content_matches,
    _detect_is_binary,
    _row_base,
)
from src.server.services.persistence.repos import repo_of
from src.server.services.persistence.sync_result import SyncResult, UnsavedFile
from src.server.services.persistence.transfer import (
    INPROCESS_MAX_INFLIGHT_BYTES,
    ScanEntry,
)

logger = logging.getLogger(__name__)

# Maximum cumulative bytes written inline as an upload-failure fallback per sync pass.
UPLOAD_FALLBACK_BUDGET_BYTES = 64 * 1024 * 1024  # 64 MiB

_CHANGED_SENTINEL = object()


def is_write_inline_only_active(*, blobs_on: bool) -> bool:
    """True when main storage is enabled and Level-1 inline-only write mode is on."""
    return bool(blobs_on) and is_wsfiles_write_inline_only()


def is_inline_write_eligible(entry: ScanEntry) -> bool:
    """True for regular non-Git files that fit within ``INLINE_MAX_BYTES`` (100 MiB)."""
    return (
        entry.kind == "file"
        and repo_of(entry.path) is None
        and entry.size <= INLINE_MAX_BYTES
    )


def should_skip_pack_for_write_mode(
    entry: ScanEntry, *, write_inline_only: bool
) -> bool:
    """Whether ``entry`` should bypass ``pack_members`` and route inline instead."""
    return write_inline_only and is_inline_write_eligible(entry)


def unchanged_inline_only_packed_paths(
    scan_entries: list[ScanEntry],
    existing: dict[str, dict[str, Any]],
    *,
    write_inline_only: bool,
) -> frozenset[str]:
    """Unchanged non-Git previously-packed paths kept out of ``pack_members``.

    Passing these into ``_persist_packed(..., withheld=...)`` prevents unrelated
    ``.git`` pack chunks from being rewritten on every unchanged sync solely
    because unchanged non-Git packed files were not placed in ``pack_members``.
    Any modified or deleted previously-packed file is NOT included here.
    """
    if not write_inline_only:
        return frozenset()
    kept: set[str] = set()
    for entry in scan_entries:
        if not is_inline_write_eligible(entry):
            continue
        db = existing.get(entry.path)
        if db is not None and db.get("pack_sha256") and _content_matches(db, entry):
            kept.add(entry.path)
    return frozenset(kept)


async def _read_bounded_sandbox_bytes(
    sandbox: Any, abs_path: str, max_bytes: int
) -> bytes | None | object:
    """Read at most ``max_bytes`` from ``sandbox``; return ``_CHANGED_SENTINEL`` if larger."""
    limit = max(0, int(max_bytes))
    capped_reader = getattr(sandbox, "adownload_file_bytes_capped", None)
    if callable(capped_reader):
        try:
            content = await capped_reader(abs_path, lambda _buf, _lim=limit: _lim)
        except FileOverCap:
            return _CHANGED_SENTINEL
        if content is None:
            return None
        if len(content) > limit:
            return _CHANGED_SENTINEL
        return content

    runtime = getattr(sandbox, "runtime", None)
    stream_fn = getattr(runtime, "download_file_stream", None)
    if callable(stream_fn):
        stream = stream_fn(abs_path)
        buf = bytearray()
        try:
            async for chunk in stream:
                buf.extend(chunk)
                if len(buf) > limit:
                    return _CHANGED_SENTINEL
        finally:
            await stream.aclose()
        return bytes(buf)

    content = await sandbox.adownload_file_bytes(abs_path)
    if content is None:
        return None
    if len(content) > limit:
        return _CHANGED_SENTINEL
    return content


async def _persist_inline_bounded(
    workspace_id: str,
    sandbox: Any,
    entries: list[ScanEntry],
    *,
    layout: WorkspaceLayout,
    conn: Any,
    max_bytes_by_path: dict[str, int] | None = None,
) -> tuple[int, list[UnsavedFile]]:
    """Bounded, exact-verified inline writer for Dalton write-inline-only & fallback paths.

    - Streams with a strict per-file byte ceiling (never exceeding scanned size,
      ``INLINE_MAX_BYTES``, or any caller-supplied per-entry budget cap).
    - Verifies ``len(content) == entry.size`` and ``sha256(content) == entry.sha256``.
    - Any file that changed or grew past the cap returns ``UnsavedFile(path, "changed")``
      and is NOT upserted into PostgreSQL.
    - Uses ``blobs.bulk_upsert_files`` and ``_row_base(entry)`` so pointer columns
      (``blob_sha256``, ``pack_sha256``, ``pack_offset``) are cleared on upsert.
    """
    synced = 0
    unsaved: list[UnsavedFile] = []

    async def _one(entry: ScanEntry) -> dict[str, Any] | UnsavedFile:
        hard_cap = min(int(entry.size), INLINE_MAX_BYTES)
        if max_bytes_by_path is not None and entry.path in max_bytes_by_path:
            hard_cap = min(hard_cap, int(max_bytes_by_path[entry.path]))
        if hard_cap < 0:
            return UnsavedFile(entry.path, "changed", entry.size)
        try:
            abs_path = _blobs._entry_abs_path(entry, layout)
            content = await _read_bounded_sandbox_bytes(sandbox, abs_path, hard_cap)
            if content is None or content is _CHANGED_SENTINEL:
                return UnsavedFile(entry.path, "changed", entry.size)
            assert isinstance(content, bytes)
            if len(content) != entry.size or len(content) > hard_cap:
                return UnsavedFile(entry.path, "changed", entry.size)
            content_hash = hashlib.sha256(content).hexdigest()
            if entry.sha256 is not None and content_hash != entry.sha256:
                return UnsavedFile(entry.path, "changed", entry.size)
            is_binary = _detect_is_binary(entry.path, content)
            content_text = None
            content_binary = None
            if is_binary:
                content_binary = content
            else:
                try:
                    content_text = content.decode("utf-8")
                except UnicodeDecodeError:
                    is_binary = True
                    content_binary = content
            row = _row_base(entry)
            mime, _ = mimetypes.guess_type(entry.path)
            row.update(
                {
                    "file_size": len(content),
                    "content_hash": content_hash,
                    "content_text": content_text,
                    "content_binary": content_binary,
                    "mime_type": mime,
                    "is_binary": is_binary,
                }
            )
            return row
        except Exception as e:
            logger.warning(
                "Error downloading file %s for bounded inline write (workspace %s): %s",
                entry.path,
                workspace_id,
                e,
            )
            return UnsavedFile(entry.path, "failed", entry.size)

    for batch in _blobs._batched_by_weight(
        entries, INPROCESS_MAX_INFLIGHT_BYTES, _blobs.RELAY_CONCURRENCY
    ):
        rows: list[dict[str, Any]] = []
        for payload in await asyncio.gather(*(_one(e) for e in batch)):
            if isinstance(payload, UnsavedFile):
                unsaved.append(payload)
            else:
                rows.append(payload)
        if rows:
            synced += await _blobs.bulk_upsert_files(workspace_id, rows, conn=conn)
    return synced, unsaved


async def persist_needs_bytes_for_blobs_on(
    *,
    user_id: str | None,
    workspace_id: str,
    sandbox: Any,
    needs_bytes: list[ScanEntry],
    write_inline_only: bool,
    layout: WorkspaceLayout,
    conn: Any,
    rows: list[dict[str, Any]],
    result: SyncResult,
    persist_blobs_fn: Callable[..., Awaitable[tuple[list[dict[str, Any]], list[UnsavedFile]]]],
    started_at: datetime | None = None,
    persist_inline_by_repo_fn: Callable[..., Awaitable[tuple[int, int, list[UnsavedFile]]]] | None = None,
) -> None:
    """Route ``needs_bytes`` when ``blobs_on`` is True, honoring ``write_inline_only``."""
    del started_at, persist_inline_by_repo_fn  # retained for backwards compatibility
    if not write_inline_only:
        persisted, unsaved = await persist_blobs_fn(
            user_id, workspace_id, sandbox, needs_bytes, layout=layout
        )
        rows.extend(persisted)
        result.unsaved.extend(unsaved)
        return

    inline_entries: list[ScanEntry] = []
    blob_entries: list[ScanEntry] = []
    for entry in needs_bytes:
        if is_inline_write_eligible(entry):
            inline_entries.append(entry)
        else:
            blob_entries.append(entry)

    if blob_entries:
        persisted, unsaved = await persist_blobs_fn(
            user_id, workspace_id, sandbox, blob_entries, layout=layout
        )
        rows.extend(persisted)
        result.unsaved.extend(unsaved)

    if inline_entries:
        inline_synced, inline_unsaved = await _persist_inline_bounded(
            workspace_id,
            sandbox,
            inline_entries,
            layout=layout,
            conn=conn,
        )
        result.synced += inline_synced
        result.unsaved.extend(inline_unsaved)


async def fallback_failed_uploads_inline(
    *,
    workspace_id: str,
    sandbox: Any,
    scan_entries: list[ScanEntry],
    rows: list[dict[str, Any]],
    result: SyncResult,
    scanned_unsaved: int,
    layout: WorkspaceLayout,
    conn: Any,
    blobs_on: bool = True,
    started_at: datetime | None = None,
    persist_inline_by_repo_fn: Callable[..., Awaitable[tuple[int, int, list[UnsavedFile]]]] | None = None,
    budget_bytes: int | None = None,
) -> list[dict[str, Any]]:
    """Write eligible failed main-bucket uploads inline within a 64 MiB per-pass budget.

    Must be called BEFORE ``hold_repos(...)`` so Git repository failures remain
    protected by ``hold_repos()`` while recovered non-Git files leave
    ``result.unsaved``, leave ``rows``, and increment ``result.synced``.
    Files that changed after scan are updated to ``reason="changed"`` and are not
    written or counted as synced.
    """
    del started_at, persist_inline_by_repo_fn  # retained for backwards compatibility
    if not blobs_on:
        return rows
    cap = UPLOAD_FALLBACK_BUDGET_BYTES if budget_bytes is None else budget_bytes
    if cap <= 0:
        return rows
    transfer_unsaved = result.unsaved[scanned_unsaved:]
    if not transfer_unsaved:
        return rows

    entry_by_path: dict[str, ScanEntry] = {
        e.path: e for e in scan_entries if e.kind == "file"
    }

    fallback_entries: list[ScanEntry] = []
    fallback_paths: set[str] = set()
    max_bytes_by_path: dict[str, int] = {}
    used_bytes = 0
    for item in transfer_unsaved:
        if item.reason != "failed":
            continue
        if item.path in fallback_paths:
            continue
        entry = entry_by_path.get(item.path)
        if entry is None or not is_inline_write_eligible(entry):
            continue
        if used_bytes + entry.size > cap:
            continue
        remaining = cap - used_bytes
        used_bytes += entry.size
        fallback_paths.add(entry.path)
        fallback_entries.append(entry)
        max_bytes_by_path[entry.path] = min(entry.size, remaining, INLINE_MAX_BYTES)

    if not fallback_entries:
        return rows

    inline_synced, inline_unsaved = await _persist_inline_bounded(
        workspace_id,
        sandbox,
        fallback_entries,
        layout=layout,
        conn=conn,
        max_bytes_by_path=max_bytes_by_path,
    )
    still_unsaved_by_path = {u.path: u for u in inline_unsaved}
    succeeded_paths: set[str] = set()
    for entry in fallback_entries:
        if entry.path not in still_unsaved_by_path:
            succeeded_paths.add(entry.path)
            logger.warning(
                "Object-storage upload failed for %s (%d bytes, workspace %s); "
                "wrote file inline as bounded fallback",
                entry.path,
                entry.size,
                workspace_id,
            )

    if succeeded_paths:
        result.synced += inline_synced

    new_transfer_unsaved: list[UnsavedFile] = []
    for item in transfer_unsaved:
        if item.reason == "failed" and item.path in succeeded_paths:
            continue
        if item.reason == "failed" and item.path in still_unsaved_by_path:
            new_transfer_unsaved.append(still_unsaved_by_path[item.path])
            continue
        new_transfer_unsaved.append(item)
    result.unsaved[scanned_unsaved:] = new_transfer_unsaved

    if not succeeded_paths:
        return rows
    return [r for r in rows if r.get("file_path") not in succeeded_paths]
