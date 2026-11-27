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
     PostgreSQL.
   - Files ``> 100 MiB`` and files inside Git repositories (including ``.git``
     history) continue writing to the main object-storage bucket (standalone
     objects and packs).
   - Unchanged existing pointer/packed rows remain untouched and readable.

2. Bounded upload-failure inline fallback (when main storage is enabled):
   - Runs before ``hold_repos(...)`` in ``_sync_locked``.
   - Eligible items returned as ``UnsavedFile(path, "failed", size)`` by
     ``_persist_blobs`` or ``_persist_packed`` with ``repo_of(path) is None``
     and ``size <= INLINE_MAX_BYTES`` are written inline within a per-sync
     cumulative budget of 64 MiB (counted by ``entry.size``), logging a
     ``WARNING`` per file.
   - Items over budget, ``> 100 MiB``, inside Git repositories, or with
     ``reason != "failed"`` (e.g. ``"changed"``) remain in ``result.unsaved``
     so Git repositories keep whole-repo ``hold_repos()`` protection and
     retriable items retry on the next sync.
   - Fallback writes never trigger row deletions (``gone={}``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from ptc_agent.core.paths import WorkspaceLayout
from src.dalton.storage_config import is_wsfiles_write_inline_only
from src.server.database.blob_keys import INLINE_MAX_BYTES
from src.server.services.persistence._rows import _content_matches
from src.server.services.persistence.repos import repo_of
from src.server.services.persistence.sync_result import SyncResult, UnsavedFile
from src.server.services.persistence.transfer import ScanEntry

logger = logging.getLogger(__name__)

# Maximum cumulative bytes written inline as an upload-failure fallback per sync pass.
UPLOAD_FALLBACK_BUDGET_BYTES = 64 * 1024 * 1024  # 64 MiB


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


async def persist_needs_bytes_for_blobs_on(
    *,
    user_id: str | None,
    workspace_id: str,
    sandbox: Any,
    needs_bytes: list[ScanEntry],
    write_inline_only: bool,
    layout: WorkspaceLayout,
    conn: Any,
    started_at: datetime,
    rows: list[dict[str, Any]],
    result: SyncResult,
    persist_blobs_fn: Callable[..., Awaitable[tuple[list[dict[str, Any]], list[UnsavedFile]]]],
    persist_inline_by_repo_fn: Callable[..., Awaitable[tuple[int, int, list[UnsavedFile]]]],
) -> None:
    """Route ``needs_bytes`` when ``blobs_on`` is True, honoring ``write_inline_only``."""
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
        # All inline_entries have repo_of(path) is None; pass empty ``gone`` so
        # inline writes never delete rows backed by object storage.
        inline_synced, inline_deleted, inline_unsaved = await persist_inline_by_repo_fn(
            workspace_id,
            sandbox,
            inline_entries,
            layout=layout,
            conn=conn,
            gone={},
            untouched_since=started_at,
        )
        result.synced += inline_synced
        result.deleted += inline_deleted
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
    started_at: datetime,
    persist_inline_by_repo_fn: Callable[..., Awaitable[tuple[int, int, list[UnsavedFile]]]],
    budget_bytes: int | None = None,
) -> list[dict[str, Any]]:
    """Write eligible failed main-bucket uploads inline within a 64 MiB per-pass budget.

    Must be called BEFORE ``hold_repos(...)`` so Git repository failures remain
    protected by ``hold_repos()`` while recovered non-Git files leave
    ``result.unsaved``, leave ``rows``, and increment ``result.synced``.
    """
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
        used_bytes += entry.size
        fallback_paths.add(entry.path)
        fallback_entries.append(entry)

    if not fallback_entries:
        return rows

    # Never pass ``gone``: fallback inline writes must not trigger row deletions.
    inline_synced, _inline_deleted, inline_unsaved = await persist_inline_by_repo_fn(
        workspace_id,
        sandbox,
        fallback_entries,
        layout=layout,
        conn=conn,
        gone={},
        untouched_since=started_at,
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

    if not succeeded_paths:
        return rows

    result.synced += inline_synced
    new_transfer_unsaved: list[UnsavedFile] = []
    for item in transfer_unsaved:
        if item.path in succeeded_paths and item.reason == "failed":
            continue
        new_transfer_unsaved.append(item)
    result.unsaved[scanned_unsaved:] = new_transfer_unsaved

    return [r for r in rows if r.get("file_path") not in succeeded_paths]
