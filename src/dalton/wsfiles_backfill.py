"""Hardened online backfill & pre-flight stats for ``workspace_files`` blobs.

Import-clean by design: never imports ``src.server.database.pool`` or
``src.config.env`` (which calls ``load_dotenv()``). All object-storage I/O goes
through the :mod:`src.utils.storage` facade.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import psycopg
from psycopg.rows import dict_row

from src.dalton.storage_keys import storage_user_id
from src.server.database.blob_keys import (
    BLOB_CONTENT_TYPE,
    CLAIM_SQL,
    INLINE_MAX_BYTES,
    REGISTER_SQL,
    RELAY_MAX_BYTES,
    BlobError,
    blob_key,
)
from src.utils import storage as storage_facade

logger = logging.getLogger("backfill_workspace_file_blobs")

UPLOAD_CONCURRENCY = 8
INFLIGHT_MAX_BYTES = 64 * 1024 * 1024

_INLINE_WHERE = (
    "blob_sha256 IS NULL "
    "AND pack_sha256 IS NULL "
    "AND (content_text IS NOT NULL OR content_binary IS NOT NULL)"
)

_DUMMY_SHA256 = "0" * 64


@dataclass(frozen=True)
class PendingRow:
    """One inline row loaded into memory, awaiting claim/upload/verify/repoint."""

    file_id: str
    user_id: str
    mapped_user_id: str
    sha256: str
    data: bytes
    stored_hash: str | None
    workspace_id: str
    file_path: str


@dataclass
class BackfillCounts:
    moved: int = 0
    moved_bytes: int = 0
    skipped_put: int = 0
    verified: int = 0
    failed: int = 0
    skipped_raced: int = 0
    skipped_lossy: int = 0
    skipped_legacy_owner: int = 0
    skipped_empty: int = 0
    lossy_file_ids: list[str] = field(default_factory=list)
    pending: list[PendingRow] = field(default_factory=list)
    pending_bytes: int = 0


@dataclass
class BackfillStats:
    inline_rows: int = 0
    inline_bytes: int = 0
    dedup_bytes: int = 0
    non_uuid_owners: dict[str, str] = field(default_factory=dict)
    owner_errors: list[str] = field(default_factory=list)
    skipped_lossy: list[str] = field(default_factory=list)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def _inline_bytes(row: dict) -> bytes | None:
    content_binary = row.get("content_binary")
    if content_binary is not None:
        return bytes(content_binary)
    content_text = row.get("content_text")
    if content_text is not None:
        return content_text.encode("utf-8")
    return None


def choose_reverse_inline_target(
    data: bytes,
    is_binary: bool,
    fallback_inline_columns: Callable[[bytes, bool], tuple[str | None, bytes | None]],
) -> tuple[str | None, bytes | None, bool, bool]:
    """Select ``(content_text, content_binary, final_is_binary, relabelled)`` for ``--reverse``.

    Pointer rows in ``(INLINE_MAX_BYTES, RELAY_MAX_BYTES]`` (``> 100 MiB``) are
    written back verbatim into ``content_binary`` while preserving their
    original ``is_binary`` flag so :func:`resolve_stored_byte_range` can stream
    them via native PostgreSQL TOAST slicing without UTF-8 materialization.
    Smaller rows retain upstream ``_inline_columns`` semantics unchanged.
    """
    if len(data) > INLINE_MAX_BYTES:
        return None, data, bool(is_binary), False
    content_text, content_binary = fallback_inline_columns(data, is_binary)
    final_is_binary = bool(is_binary) or content_binary is not None
    relabelled = content_binary is not None and not is_binary
    return content_text, content_binary, final_is_binary, relabelled


async def _next_inline_ids(
    conn: psycopg.AsyncConnection,
    after: str | None,
    limit: int,
    workspace_id: str | None,
) -> list[str]:
    params: list = []
    scope_clause = ""
    if workspace_id is not None:
        scope_clause = "AND workspace_id = %s::uuid"
        params.append(workspace_id)
    cursor_clause = ""
    if after is not None:
        cursor_clause = "AND workspace_file_id > %s::uuid"
        params.append(after)
    params.append(limit)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT workspace_file_id
            FROM workspace_files
            WHERE {_INLINE_WHERE} {scope_clause} {cursor_clause}
            ORDER BY workspace_file_id
            LIMIT %s
            """,
            params,
        )
        return [str(r["workspace_file_id"]) for r in await cur.fetchall()]


async def _load_row(conn: psycopg.AsyncConnection, file_id: str) -> dict | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT f.workspace_file_id, f.workspace_id, w.user_id, f.file_path,
                   f.content_hash, f.content_text, f.content_binary,
                   f.blob_sha256, f.pack_sha256, f.pack_offset, f.file_size,
                   f.is_binary
            FROM workspace_files f
            JOIN workspaces w ON w.workspace_id = f.workspace_id
            WHERE f.workspace_file_id = %s::uuid
            """,
            (file_id,),
        )
        return await cur.fetchone()


async def collect_backfill_stats(
    conn: psycopg.AsyncConnection,
    *,
    batch: int = 500,
    workspace_id: str | None = None,
    include_legacy_owners: bool = False,  # noqa: ARG001 - accepted for CLI parity
) -> BackfillStats:
    """Read-only scan of inline rows, dedup bytes, owner mappings, and lossy rows.

    Does not touch object storage or check ``is_storage_enabled()``.
    """
    stats = BackfillStats()
    seen_hashes: dict[str, int] = {}
    seen_owners: set[str] = set()
    mapped_to_raw: dict[str, str] = {}

    after: str | None = None
    while True:
        ids = await _next_inline_ids(conn, after, batch, workspace_id)
        if not ids:
            break
        after = ids[-1]

        for file_id in ids:
            row = await _load_row(conn, file_id)
            if (
                row is None
                or row["blob_sha256"] is not None
                or row["pack_sha256"] is not None
            ):
                continue

            data = _inline_bytes(row)
            if data is None:
                continue

            owner = str(row["user_id"])
            if owner not in seen_owners:
                seen_owners.add(owner)
                try:
                    mapped = storage_user_id(owner)
                    # Validate the mapped id against blob_key's own rules.
                    blob_key(owner, _DUMMY_SHA256)
                except BlobError as exc:
                    err = f"Invalid storage user mapping for owner {owner!r}: {exc}"
                    stats.owner_errors.append(err)
                    if not _is_uuid(owner):
                        stats.non_uuid_owners[owner] = f"ERROR: {exc}"
                else:
                    prev = mapped_to_raw.get(mapped)
                    if prev is not None and prev != owner:
                        stats.owner_errors.append(
                            f"Storage user id collision: {prev!r} and {owner!r} "
                            f"both map to {mapped!r}"
                        )
                    else:
                        mapped_to_raw[mapped] = owner
                    if not _is_uuid(owner):
                        stats.non_uuid_owners[owner] = mapped

            stats.inline_rows += 1
            stats.inline_bytes += len(data)

            sha256 = hashlib.sha256(data).hexdigest()
            stored_hash = row["content_hash"]
            if (
                row.get("content_binary") is None
                and row.get("content_text") is not None
                and stored_hash
                and stored_hash != sha256
            ):
                stats.skipped_lossy.append(file_id)
            elif stored_hash and stored_hash != sha256:
                stats.skipped_lossy.append(file_id)

            dedup_key = stored_hash or sha256
            if dedup_key not in seen_hashes:
                seen_hashes[dedup_key] = len(data)

    stats.dedup_bytes = sum(seen_hashes.values())
    return stats


async def run_stats_only(
    conn: psycopg.AsyncConnection,
    *,
    batch: int = 500,
    workspace_id: str | None = None,
    include_legacy_owners: bool = False,
) -> int:
    """Execute ``--stats-only`` pre-flight inspection and log structured summary."""
    stats = await collect_backfill_stats(
        conn,
        batch=batch,
        workspace_id=workspace_id,
        include_legacy_owners=include_legacy_owners,
    )
    logger.info(
        "stats-only: inline_rows=%d inline_bytes=%d dedup_bytes=%d "
        "non_uuid_owners=%d skipped_lossy=%d owner_errors=%d",
        stats.inline_rows,
        stats.inline_bytes,
        stats.dedup_bytes,
        len(stats.non_uuid_owners),
        len(stats.skipped_lossy),
        len(stats.owner_errors),
    )
    if stats.non_uuid_owners:
        for raw_owner, mapped in sorted(stats.non_uuid_owners.items()):
            logger.info("non-UUID owner: %s -> %s", raw_owner, mapped)
    if stats.skipped_lossy:
        logger.warning(
            "skipped_lossy workspace_file_ids (%d): %s",
            len(stats.skipped_lossy),
            ", ".join(stats.skipped_lossy),
        )
    if stats.owner_errors:
        for err in stats.owner_errors:
            logger.error("owner check failed: %s", err)
        return 1
    return 0


async def _repoint_row(conn: psycopg.AsyncConnection, item: PendingRow) -> bool:
    """Single-row transaction: ``REGISTER_SQL`` + CAS clear of inline columns."""
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute(REGISTER_SQL, (item.user_id, item.sha256, len(item.data)))
        await cur.execute(
            """
            UPDATE workspace_files
               SET blob_sha256 = %s,
                   content_text = NULL,
                   content_binary = NULL,
                   file_size = %s
             WHERE workspace_file_id = %s::uuid
               AND content_hash IS NOT DISTINCT FROM %s
               AND blob_sha256 IS NULL
               AND pack_sha256 IS NULL
            """,
            (item.sha256, len(item.data), item.file_id, item.stored_hash),
        )
        return bool(cur.rowcount)


async def _flush_batch(
    conn: psycopg.AsyncConnection,
    counts: BackfillCounts,
    verified_digests: set[tuple[str, str]],
) -> None:
    """Four-step flush: CLAIM -> size-check/PUT -> read-back sha256 -> REGISTER+CAS."""
    if not counts.pending:
        return

    pending = counts.pending
    counts.pending = []
    counts.pending_bytes = 0

    # Step 1: CLAIM_SQL grouped by original user_id.
    by_user: dict[str, list[str]] = {}
    for item in pending:
        by_user.setdefault(item.user_id, []).append(item.sha256)

    failed_users: set[str] = set()
    for user_id, digests in by_user.items():
        try:
            async with conn.cursor() as cur:
                await cur.execute(CLAIM_SQL, (user_id, list(set(digests))))
        except Exception as exc:
            logger.error("CLAIM_SQL failed for user %s: %s", user_id, exc)
            failed_users.add(user_id)

    # Deduplicate object operations by (mapped_user_id, sha256) within the batch.
    unique_items: dict[tuple[str, str], PendingRow] = {}
    for item in pending:
        if item.user_id in failed_users:
            continue
        digest_key = (item.mapped_user_id, item.sha256)
        unique_items.setdefault(digest_key, item)

    sem = asyncio.Semaphore(UPLOAD_CONCURRENCY)

    # Step 2: Check object_size(key); skip PUT on matching length, else upload_bytes.
    async def _ensure_uploaded(
        digest_key: tuple[str, str], item: PendingRow
    ) -> tuple[tuple[str, str], bool, bool]:
        """Return ``(digest_key, ok, put_skipped)``."""
        key = blob_key(item.user_id, item.sha256)
        async with sem:
            try:
                existing_size = await asyncio.to_thread(storage_facade.object_size, key)
            except Exception as exc:
                logger.warning("object_size failed for %s: %s", key, exc)
                existing_size = None

            if existing_size == len(item.data):
                return digest_key, True, True

            try:
                ok = await asyncio.to_thread(
                    storage_facade.upload_bytes,
                    key,
                    item.data,
                    BLOB_CONTENT_TYPE,
                    max_size=RELAY_MAX_BYTES,
                )
            except Exception as exc:
                logger.error("upload_bytes raised for %s: %s", key, exc)
                return digest_key, False, False

            if ok is not True:
                logger.error(
                    "upload_bytes returned %r for %s (%d bytes)",
                    ok,
                    key,
                    len(item.data),
                )
                return digest_key, False, False
            return digest_key, True, False

    upload_results = await asyncio.gather(
        *(_ensure_uploaded(dk, it) for dk, it in unique_items.items())
    )
    upload_ok: dict[tuple[str, str], bool] = {}
    put_skipped_keys: set[tuple[str, str]] = set()
    for dk, ok, skipped in upload_results:
        upload_ok[dk] = ok
        if skipped:
            put_skipped_keys.add(dk)

    # Count skipped_put per row whose key skipped PUT (or deduplicated duplicate in batch).
    seen_uploaded_in_batch: set[tuple[str, str]] = set()
    for item in pending:
        if item.user_id in failed_users:
            continue
        dk = (item.mapped_user_id, item.sha256)
        if not upload_ok.get(dk):
            continue
        if dk in put_skipped_keys or dk in seen_uploaded_in_batch:
            counts.skipped_put += 1
        else:
            seen_uploaded_in_batch.add(dk)

    # Step 3: Read-back sha256_object(key) for digests not yet verified in this process.
    async def _verify_digest(
        digest_key: tuple[str, str], item: PendingRow
    ) -> tuple[tuple[str, str], bool, bool]:
        """Return ``(digest_key, ok, newly_verified)``."""
        if digest_key in verified_digests:
            return digest_key, True, False

        key = blob_key(item.user_id, item.sha256)
        async with sem:
            try:
                actual = await asyncio.to_thread(storage_facade.sha256_object, key)
            except Exception as exc:
                logger.warning("sha256_object raised for %s: %s", key, exc)
                actual = None

            if actual == item.sha256:
                return digest_key, True, True

            logger.warning(
                "read-back digest mismatch for %s (expected %s, got %s); re-uploading once",
                key,
                item.sha256,
                actual,
            )
            try:
                re_ok = await asyncio.to_thread(
                    storage_facade.upload_bytes,
                    key,
                    item.data,
                    BLOB_CONTENT_TYPE,
                    max_size=RELAY_MAX_BYTES,
                )
            except Exception as exc:
                logger.error("re-upload raised for %s: %s", key, exc)
                return digest_key, False, False

            if re_ok is not True:
                logger.error("re-upload failed for %s: %r", key, re_ok)
                return digest_key, False, False

            try:
                actual_retry = await asyncio.to_thread(
                    storage_facade.sha256_object, key
                )
            except Exception as exc:
                logger.error("re-verify sha256_object raised for %s: %s", key, exc)
                return digest_key, False, False

            if actual_retry == item.sha256:
                return digest_key, True, True

            logger.error(
                "read-back digest still mismatched after re-upload for %s "
                "(expected %s, got %s); leaving inline",
                key,
                item.sha256,
                actual_retry,
            )
            return digest_key, False, False

    verify_targets = [
        (dk, it) for dk, it in unique_items.items() if upload_ok.get(dk)
    ]
    verify_results = await asyncio.gather(
        *(_verify_digest(dk, it) for dk, it in verify_targets)
    )
    verify_ok: dict[tuple[str, str], bool] = {}
    for dk, ok, newly_verified in verify_results:
        verify_ok[dk] = ok
        if ok and newly_verified:
            verified_digests.add(dk)
            counts.verified += 1

    # Step 4: Single-row transaction REGISTER_SQL + CAS clear inline columns.
    for item in pending:
        dk = (item.mapped_user_id, item.sha256)
        if item.user_id in failed_users or not upload_ok.get(dk) or not verify_ok.get(dk):
            counts.failed += 1
            continue
        try:
            moved = await _repoint_row(conn, item)
        except Exception as exc:
            counts.failed += 1
            logger.error(
                "repoint transaction failed for workspace_file_id=%s (%s %s): %s",
                item.file_id,
                item.workspace_id,
                item.file_path,
                exc,
            )
            continue

        if moved:
            counts.moved += 1
            counts.moved_bytes += len(item.data)
        else:
            counts.skipped_raced += 1
            logger.warning(
                "row changed under us, left inline: %s %s",
                item.workspace_id,
                item.file_path,
            )


async def run_forward(
    conn: psycopg.AsyncConnection,
    *,
    apply: bool,
    batch: int = 500,
    workspace_id: str | None = None,
    include_legacy_owners: bool = False,
    sleep_ms: int = 0,
) -> int:
    """Hardened forward backfill with keyset pagination, read-back verification, and CAS."""
    after: str | None = None
    counts = BackfillCounts()
    legacy_owners: set[str] = set()
    verified_digests: set[tuple[str, str]] = set()
    sleep_s = max(0, sleep_ms) / 1000.0

    async def _flush_and_pace() -> None:
        if not counts.pending:
            return
        await _flush_batch(conn, counts, verified_digests)
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)

    while True:
        ids = await _next_inline_ids(conn, after, batch, workspace_id)
        if not ids:
            break
        after = ids[-1]

        for file_id in ids:
            row = await _load_row(conn, file_id)
            if (
                row is None
                or row["blob_sha256"] is not None
                or row["pack_sha256"] is not None
            ):
                continue

            owner = str(row["user_id"])
            if not include_legacy_owners and not _is_uuid(owner):
                if owner not in legacy_owners:
                    legacy_owners.add(owner)
                    logger.warning(
                        "owner %s has a non-UUID id; leaving its rows inline "
                        "(--include-legacy-owners to move them)",
                        owner,
                    )
                counts.skipped_legacy_owner += 1
                continue

            data = _inline_bytes(row)
            if data is None:
                counts.skipped_empty += 1
                continue

            sha256 = hashlib.sha256(data).hexdigest()
            stored = row["content_hash"]
            if stored and stored != sha256:
                counts.skipped_lossy += 1
                counts.lossy_file_ids.append(file_id)
                logger.warning(
                    "hash mismatch (lossy inline copy), left inline: "
                    "workspace_file_id=%s %s %s (stored %s, inline %s)",
                    file_id,
                    row["workspace_id"],
                    row["file_path"],
                    stored,
                    sha256,
                )
                continue

            try:
                mapped_owner = storage_user_id(owner)
                blob_key(owner, sha256)
            except BlobError as exc:
                counts.failed += 1
                logger.error(
                    "invalid storage key for workspace_file_id=%s owner=%r: %s",
                    file_id,
                    owner,
                    exc,
                )
                continue

            if not apply:
                counts.moved += 1
                counts.moved_bytes += len(data)
                continue

            # Enforce pending_bytes <= INFLIGHT_MAX_BYTES before enqueuing.
            # A single file > INFLIGHT_MAX_BYTES runs alone in its own batch.
            if counts.pending and (
                counts.pending_bytes + len(data) > INFLIGHT_MAX_BYTES
                or len(data) > INFLIGHT_MAX_BYTES
            ):
                await _flush_and_pace()

            counts.pending.append(
                PendingRow(
                    file_id=file_id,
                    user_id=owner,
                    mapped_user_id=mapped_owner,
                    sha256=sha256,
                    data=data,
                    stored_hash=stored,
                    workspace_id=str(row["workspace_id"]),
                    file_path=row["file_path"],
                )
            )
            counts.pending_bytes += len(data)

            if (
                len(counts.pending) >= UPLOAD_CONCURRENCY
                or counts.pending_bytes >= INFLIGHT_MAX_BYTES
            ):
                await _flush_and_pace()

        await _flush_and_pace()
        logger.info(
            "progress: moved=%d moved_bytes=%d skipped_put=%d verified=%d failed=%d",
            counts.moved,
            counts.moved_bytes,
            counts.skipped_put,
            counts.verified,
            counts.failed,
        )

    if counts.lossy_file_ids:
        logger.warning(
            "skipped_lossy workspace_file_ids (%d): %s",
            len(counts.lossy_file_ids),
            ", ".join(counts.lossy_file_ids),
        )

    logger.info(
        "%s summary: moved=%d moved_bytes=%d skipped_put=%d verified=%d "
        "failed=%d skipped_raced=%d skipped_lossy=%d skipped_legacy_owner=%d "
        "skipped_empty=%d",
        "apply" if apply else "dry-run",
        counts.moved,
        counts.moved_bytes,
        counts.skipped_put,
        counts.verified,
        counts.failed,
        counts.skipped_raced,
        counts.skipped_lossy,
        counts.skipped_legacy_owner,
        counts.skipped_empty,
    )
    return 1 if counts.failed > 0 else 0
