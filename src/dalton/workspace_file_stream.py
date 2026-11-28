"""Unified stored-file range reading and streaming downloads for large files.

Covers stored workspace files in the ``(SERVE_MAX_BYTES, RELAY_MAX_BYTES]``
range (``(100 MiB, 256 MiB]``) for both stopped-workspace backup downloads and
public share downloads, plus SQL 1-based inline slicing so rolled-back large
inline rows stream in bounded ``8 MiB`` chunks without materializing the full
body in memory.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Any

import anyio
from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse
from psycopg.rows import dict_row

from ptc_agent.core.sandbox.runtime import STREAM_CHUNK_BYTES
from src.dalton.streaming_redactor import StreamingSecretRedactor
from src.server.app.workspace_files._shared import (
    RANGE_MAX_BYTES,
    _is_binary,
    _starts_as_text,
    _unavailable_as_503,
    streamed_download_budget,
)
from src.server.database.blob_keys import RELAY_MAX_BYTES
from src.server.database.pool import get_db_connection
from src.server.services.persistence.resolve import (
    SERVE_MAX_BYTES,
    FileBytesUnavailable,
    resolve_file_byte_range,
)
from src.server.services.served_paths import is_text_content_type
from src.server.services.workspace_file_containment import UTF8_SNIFF_BYTES
from src.server.utils.http_headers import content_disposition
from src.server.utils.secret_redactor import get_vault_secrets_for_redaction

logger = logging.getLogger(__name__)

# Matches crud.py's wait budget for the shared 16-slot streamed download budget.
_STREAM_SLOT_WAIT_S: float = 30.0
_RO_BINARY_DETAIL = "Binary file download is not available in read-only mode"


# Sentinel distinguishing "version field not provided" from a literal SQL NULL.
_UNSET: Any = object()


class StoredStreamSizeMismatchError(RuntimeError):
    """Raised mid-stream when bytes read disagree with the manifest ``file_size``."""


class StoredStreamVersionChangedError(FileBytesUnavailable):
    """Raised when an inline file row changes version between metadata, sniff, and slices.

    Subclasses :class:`FileBytesUnavailable` so pre-header reads map cleanly to
    HTTP ``503`` in :func:`backup_large_download` and HTTP ``404`` in
    :func:`shared_large_download`, while mid-stream changes abort the response.
    """


async def get_large_file_meta(
    workspace_id: str,
    file_path: str,
    *,
    conn=None,
) -> dict[str, Any] | None:
    """Fetch lightweight file metadata without reading inline byte columns.

    Returns ``workspace_file_id, file_path, file_name, file_size, content_hash,
    updated_at, blob_sha256, pack_sha256, pack_offset, mime_type, is_binary,
    inline_kind`` where ``inline_kind`` is ``'binary'``, ``'text'``, or ``None``.
    """

    async def _execute(cur) -> dict[str, Any] | None:
        await cur.execute(
            """
            SELECT workspace_file_id,
                   file_path,
                   file_name,
                   file_size,
                   content_hash,
                   updated_at,
                   blob_sha256,
                   pack_sha256,
                   pack_offset,
                   mime_type,
                   is_binary,
                   CASE
                       WHEN content_binary IS NOT NULL THEN 'binary'
                       WHEN content_text IS NOT NULL THEN 'text'
                       ELSE NULL
                   END AS inline_kind
            FROM workspace_files
            WHERE workspace_id = %s AND file_path = %s AND kind = 'file'
            """,
            (workspace_id, file_path),
        )
        row = await cur.fetchone()
        return dict(row) if row is not None else None

    if conn is not None:
        async with conn.cursor(row_factory=dict_row) as cur:
            return await _execute(cur)
    async with get_db_connection() as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            return await _execute(cur)


async def get_inline_file_byte_range(
    workspace_id: str,
    file_path: str,
    offset: int,
    length: int,
    *,
    inline_kind: str | None = None,
    expected_file_id: Any = _UNSET,
    expected_updated_at: Any = _UNSET,
    expected_content_hash: Any = _UNSET,
    conn=None,
) -> bytes:
    """Read a byte slice of an inline ``workspace_files`` row in SQL.

    ``offset`` is 0-based; PostgreSQL ``substring(... FROM n FOR m)`` is
    1-based, so this always passes ``offset + 1`` as the first slice argument.
    When any version pin (``expected_file_id``, ``expected_updated_at``,
    ``expected_content_hash``) is provided, the ``WHERE`` clause binds to that
    exact row version and raises :class:`StoredStreamVersionChangedError` if
    the row was overwritten, deleted, or had its inline column cleared.
    Returns ``b""`` when ``offset`` is at or past EOF for the matched version.
    """
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    if length <= 0:
        return b""

    pg_from = offset + 1
    where_clauses = ["workspace_id = %s", "file_path = %s", "kind = 'file'"]
    where_params: list[Any] = [workspace_id, file_path]
    version_pinned = False

    if expected_file_id is not _UNSET:
        where_clauses.append("workspace_file_id = %s")
        where_params.append(expected_file_id)
        version_pinned = True
    if expected_updated_at is not _UNSET:
        where_clauses.append("updated_at IS NOT DISTINCT FROM %s")
        where_params.append(expected_updated_at)
        version_pinned = True
    if expected_content_hash is not _UNSET:
        where_clauses.append("content_hash IS NOT DISTINCT FROM %s")
        where_params.append(expected_content_hash)
        version_pinned = True

    if inline_kind == "binary":
        if version_pinned:
            where_clauses.append("content_binary IS NOT NULL")
        where_sql = " AND ".join(where_clauses)
        sql = f"""
            SELECT substring(content_binary FROM %s FOR %s) AS chunk
            FROM workspace_files
            WHERE {where_sql}
        """
        params: tuple[Any, ...] = (pg_from, length, *where_params)
    elif inline_kind == "text":
        if version_pinned:
            where_clauses.append("content_text IS NOT NULL")
        where_sql = " AND ".join(where_clauses)
        sql = f"""
            SELECT substring(convert_to(content_text, 'UTF8') FROM %s FOR %s) AS chunk
            FROM workspace_files
            WHERE {where_sql}
        """
        params = (pg_from, length, *where_params)
    else:
        where_sql = " AND ".join(where_clauses)
        sql = f"""
            SELECT CASE
                       WHEN content_binary IS NOT NULL
                           THEN substring(content_binary FROM %s FOR %s)
                       WHEN content_text IS NOT NULL
                           THEN substring(convert_to(content_text, 'UTF8') FROM %s FOR %s)
                       ELSE NULL
                   END AS chunk
            FROM workspace_files
            WHERE {where_sql}
        """
        params = (pg_from, length, pg_from, length, *where_params)

    async def _execute(cur) -> bytes:
        await cur.execute(sql, params)
        row = await cur.fetchone()
        if row is None:
            if version_pinned:
                logger.error(
                    "Stored inline stream version changed workspace=%s path=%r offset=%d",
                    workspace_id,
                    file_path,
                    offset,
                )
                raise StoredStreamVersionChangedError(
                    f"Stored file version changed while streaming {file_path!r}"
                )
            return b""
        chunk = row["chunk"] if isinstance(row, dict) else row[0]
        if chunk is None:
            if version_pinned:
                logger.error(
                    "Stored inline bytes cleared mid-stream workspace=%s path=%r offset=%d",
                    workspace_id,
                    file_path,
                    offset,
                )
                raise StoredStreamVersionChangedError(
                    f"Stored inline bytes cleared while streaming {file_path!r}"
                )
            return b""
        return bytes(chunk) if isinstance(chunk, memoryview) else bytes(chunk)

    if conn is not None:
        async with conn.cursor(row_factory=dict_row) as cur:
            return await _execute(cur)
    async with get_db_connection() as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            return await _execute(cur)


async def resolve_stored_byte_range(
    workspace_id: str,
    file_meta: dict[str, Any],
    offset: int,
    length: int,
    *,
    user_id: str,
    conn=None,
) -> bytes:
    """Read up to ``length`` bytes starting at 0-based ``offset`` from any stored row.

    Dispatches inline rows to :func:`get_inline_file_byte_range` (binding to
    the metadata's ``workspace_file_id``, ``updated_at``, and ``content_hash``
    when present) and blob/pack pointer rows to upstream
    :func:`resolve_file_byte_range` (stripping any inline columns from
    ``file_meta`` first). Returns ``b""`` past EOF and propagates
    :class:`FileBytesUnavailable` (including
    :class:`StoredStreamVersionChangedError`) on storage or version errors.
    """
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    if length <= 0:
        return b""

    size = file_meta.get("file_size")
    if size is not None and offset >= int(size):
        return b""

    inline_kind = file_meta.get("inline_kind")
    if inline_kind in ("binary", "text"):
        file_path = file_meta.get("file_path")
        if not file_path:
            raise ValueError("file_meta must include 'file_path' for inline range reads")
        pin_kwargs: dict[str, Any] = {}
        if "workspace_file_id" in file_meta:
            pin_kwargs["expected_file_id"] = file_meta["workspace_file_id"]
        if "updated_at" in file_meta:
            pin_kwargs["expected_updated_at"] = file_meta["updated_at"]
        if "content_hash" in file_meta:
            pin_kwargs["expected_content_hash"] = file_meta["content_hash"]
        return await get_inline_file_byte_range(
            workspace_id,
            file_path,
            offset,
            length,
            inline_kind=inline_kind,
            conn=conn,
            **pin_kwargs,
        )

    # Strip inline keys so upstream resolve_file_byte_range never treats a
    # pointer row carrying stray keys as inline and returns None.
    pointer_meta = {
        k: v
        for k, v in file_meta.items()
        if k not in ("content_binary", "content_text")
    }
    ranged = await resolve_file_byte_range(
        pointer_meta, offset, length, user_id=user_id
    )
    if ranged is None:
        raise FileBytesUnavailable("File record has no stored bytes")
    data, _file_size = ranged
    return data


async def stream_stored_file(
    workspace_id: str,
    file_meta: dict[str, Any],
    *,
    user_id: str,
    redactor: StreamingSecretRedactor | None = None,
    first_chunk: bytes | None = None,
    conn=None,
) -> AsyncIterator[bytes]:
    """Stream a stored file in ``RANGE_MAX_BYTES`` (8 MiB) slices.

    When the caller pre-reads ``first_chunk`` before sending HTTP headers, it is
    processed first and subsequent slices resume from ``offset = len(first_chunk)``.
    After reading finishes, total raw bytes read are compared against
    ``file_meta['file_size']``; any mismatch logs ``ERROR`` and raises
    :class:`StoredStreamSizeMismatchError` to abort the HTTP stream.
    """
    expected_size = int(file_meta.get("file_size") or 0)
    offset = 0
    total_read = 0

    def _emit_chunk(raw: bytes) -> bytes:
        if redactor is not None:
            return redactor.feed(raw)
        return raw

    if first_chunk is not None:
        offset = len(first_chunk)
        total_read = len(first_chunk)
        if total_read > expected_size:
            logger.error(
                "Stored stream exceeded manifest size on first chunk "
                "workspace=%s path=%r read=%d expected=%d",
                workspace_id,
                file_meta.get("file_path"),
                total_read,
                expected_size,
            )
            raise StoredStreamSizeMismatchError(
                f"Stored file grew past {expected_size} bytes"
            )
        out = _emit_chunk(first_chunk)
        if out:
            yield out

    while offset < expected_size:
        chunk = await resolve_stored_byte_range(
            workspace_id,
            file_meta,
            offset,
            RANGE_MAX_BYTES,
            user_id=user_id,
            conn=conn,
        )
        if not chunk:
            break
        offset += len(chunk)
        total_read += len(chunk)
        if total_read > expected_size:
            logger.error(
                "Stored stream exceeded manifest size "
                "workspace=%s path=%r read=%d expected=%d",
                workspace_id,
                file_meta.get("file_path"),
                total_read,
                expected_size,
            )
            raise StoredStreamSizeMismatchError(
                f"Stored file grew past {expected_size} bytes"
            )
        out = _emit_chunk(chunk)
        if out:
            yield out

    if total_read != expected_size:
        logger.error(
            "Stored stream size mismatch workspace=%s path=%r read=%d expected=%d",
            workspace_id,
            file_meta.get("file_path"),
            total_read,
            expected_size,
        )
        raise StoredStreamSizeMismatchError(
            f"Stored file ended at {total_read} of {expected_size} bytes"
        )

    if redactor is not None:
        tail = redactor.flush()
        if tail:
            yield tail


async def _with_held_cleanup(
    body: AsyncIterator[bytes], held: AsyncExitStack
) -> AsyncIterator[bytes]:
    """Ensure ``held`` closes when the body iterator completes or is aborted."""
    try:
        async for chunk in body:
            yield chunk
    finally:
        with anyio.CancelScope(shield=True):
            await held.aclose()


class _StoredStreamedDownload(StreamingResponse):
    """StreamingResponse that owns a stream-budget slot until send finishes."""

    def __init__(
        self, body: AsyncIterator[bytes], *, held: AsyncExitStack, **kwargs: Any
    ) -> None:
        self._held = held
        super().__init__(_with_held_cleanup(body, held), **kwargs)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._held.aclose()


async def _acquire_stream_slot(held: AsyncExitStack) -> None:
    """Acquire one slot from the shared 16-slot ``streamed_download_budget()``."""
    try:
        async with asyncio.timeout(_STREAM_SLOT_WAIT_S):
            await held.enter_async_context(
                streamed_download_budget().hold(STREAM_CHUNK_BYTES)
            )
    except TimeoutError:
        retry_after = str(max(1, int(round(_STREAM_SLOT_WAIT_S))))
        raise HTTPException(
            status_code=503,
            detail="Too many downloads in progress; try again shortly",
            headers={"Retry-After": retry_after},
        ) from None


def _in_stream_range(file_size: int) -> bool:
    return SERVE_MAX_BYTES < file_size <= RELAY_MAX_BYTES


async def backup_large_download(
    workspace_id: str,
    workspace: dict[str, Any],
    normalized_path: str,
    disposition: str,
    *,
    is_non_owner_reader: bool = False,
    byte_range: tuple[int, int] | None = None,
) -> Response | None:
    """Stream a ``(100 MiB, 256 MiB]`` stored file for a stopped workspace.

    Returns ``None`` when the request falls outside the streaming window
    (missing row, ``<= 100 MiB``, ``> 256 MiB``, or ranged request), letting
    upstream :func:`download_from_backup` handle it unchanged.
    """
    if byte_range is not None:
        return None

    try:
        file_meta = await get_large_file_meta(workspace_id, normalized_path)
    except RuntimeError as exc:
        if "database pool is not open" in str(exc):
            return None
        raise
    if file_meta is None:
        return None

    file_size = int(file_meta.get("file_size") or 0)
    if not _in_stream_range(file_size):
        return None

    if is_non_owner_reader and (
        file_meta.get("is_binary") or _is_binary(normalized_path)
    ):
        raise HTTPException(status_code=403, detail=_RO_BINARY_DETAIL)

    user_id = workspace["user_id"]
    filename = (
        file_meta.get("file_name")
        or normalized_path.rsplit("/", 1)[-1]
        or "download"
    )
    mime = file_meta.get("mime_type") or "application/octet-stream"

    async with AsyncExitStack() as held:
        await _acquire_stream_slot(held)

        with _unavailable_as_503(file_meta):
            first_chunk = await resolve_stored_byte_range(
                workspace_id,
                file_meta,
                0,
                min(UTF8_SNIFF_BYTES, file_size),
                user_id=user_id,
            )
            if not first_chunk:
                raise FileBytesUnavailable("Stored file returned empty first chunk")

        head = first_chunk[:UTF8_SNIFF_BYTES]
        is_text = is_text_content_type(mime) or _starts_as_text(head, file_size)

        if is_non_owner_reader and not is_text:
            raise HTTPException(status_code=403, detail=_RO_BINARY_DETAIL)

        redactor: StreamingSecretRedactor | None = None
        if is_text:
            vault_secrets = await get_vault_secrets_for_redaction(user_id)
            redactor = StreamingSecretRedactor(vault_secrets=vault_secrets)

        headers: dict[str, str] = {
            "Content-Disposition": content_disposition(
                filename, disposition=disposition
            ),
            "Cache-Control": "private, no-cache",
        }
        if redactor is None:
            headers["Content-Length"] = str(file_size)

        body = stream_stored_file(
            workspace_id,
            file_meta,
            user_id=user_id,
            redactor=redactor,
            first_chunk=first_chunk,
        )
        return _StoredStreamedDownload(
            body,
            held=held.pop_all(),
            media_type=mime,
            headers=headers,
        )


async def shared_large_download(
    workspace_id: str,
    owner_user_id: str,
    normalized_path: str,
    *,
    not_found_detail: str = "Shared resource not found",
    expected_size: int | None = None,
) -> Response | None:
    """Stream a ``(100 MiB, 256 MiB]`` stored file for a public share download.

    Caller preconditions (NOT re-checked here):
    - ``resolve_shared_files(share_token, require_download=True)`` has already
      succeeded, including any downgrade ``403`` (``allow_files=false`` with
      ``Cache-Control: no-store`` and ``Clear-Site-Data: "cache"``).
    - ``normalized_path`` has been validated against traversal and passed
      ``target.visible(normalized_path)``.

    When ``expected_size`` is supplied from a warm sandbox's
    ``FileTooLargeToServe`` check and disagrees with the mirrored ``file_size``,
    a stale large mirror row is never served (raises HTTP ``404``).

    Every file in the streaming range is unconditionally passed through
    :class:`StreamingSecretRedactor` regardless of MIME type or binary content.
    Responses always carry ``Cache-Control: no-store`` (no ``Content-Length``
    or ``ETag``). Any storage error while reading the first slice collapses to
    HTTP ``404`` with ``not_found_detail`` before headers are sent. Returns
    ``None`` outside ``(100 MiB, 256 MiB]`` to hand control back to upstream.
    """
    try:
        file_meta = await get_large_file_meta(workspace_id, normalized_path)
    except RuntimeError as exc:
        if "database pool is not open" in str(exc):
            return None
        raise
    if file_meta is None:
        return None

    file_size = int(file_meta.get("file_size") or 0)
    if not _in_stream_range(file_size):
        return None
    if expected_size is not None and file_size != int(expected_size):
        logger.warning(
            "Shared large mirror size disagrees with warm sandbox workspace=%s path=%r "
            "mirror_size=%d live_size=%d",
            workspace_id,
            normalized_path,
            file_size,
            int(expected_size),
        )
        raise HTTPException(status_code=404, detail=not_found_detail)

    filename = (
        file_meta.get("file_name")
        or normalized_path.rsplit("/", 1)[-1]
        or "download"
    )
    mime = (
        file_meta.get("mime_type")
        or mimetypes.guess_type(normalized_path)[0]
        or "application/octet-stream"
    )

    async with AsyncExitStack() as held:
        await _acquire_stream_slot(held)

        try:
            first_chunk = await resolve_stored_byte_range(
                workspace_id,
                file_meta,
                0,
                min(UTF8_SNIFF_BYTES, file_size),
                user_id=owner_user_id,
            )
        except Exception as exc:
            logger.warning(
                "Shared large file storage read failed workspace=%s path=%r: %s",
                workspace_id,
                normalized_path,
                exc,
            )
            raise HTTPException(status_code=404, detail=not_found_detail) from None

        if not first_chunk:
            logger.warning(
                "Shared large file returned empty first chunk workspace=%s path=%r",
                workspace_id,
                normalized_path,
            )
            raise HTTPException(status_code=404, detail=not_found_detail)

        vault_secrets = await get_vault_secrets_for_redaction(owner_user_id)
        redactor = StreamingSecretRedactor(vault_secrets=vault_secrets)

        headers = {
            "Content-Disposition": content_disposition(filename),
            "Cache-Control": "no-store",
        }
        body = stream_stored_file(
            workspace_id,
            file_meta,
            user_id=owner_user_id,
            redactor=redactor,
            first_chunk=first_chunk,
        )
        return _StoredStreamedDownload(
            body,
            held=held.pop_all(),
            media_type=mime,
            headers=headers,
        )


async def shared_large_download_or_warm(
    target: Any,
    normalized_path: str,
    *,
    not_found_detail: str = "File not found",
) -> tuple[Response | None, Any | None]:
    """Enforce warm-sandbox-first semantics before streaming a large shared mirror.

    Returns ``(streamed_response, warm_shared_bytes)``:
    - When the workspace sandbox is warm:
      - Reads the live file via ``share_files.warm_sandbox_bytes`` first.
      - If missing or a symlink resolves outside workspace / invisible scope
        (``resolved is None``), raises HTTP ``404`` without falling back to the
        persisted mirror.
      - If present and readable (including a file replaced/shrunk since the
        last sync), checks ``folder_unmoved`` and returns the live
        ``SharedBytes`` so the caller serves live bytes without reading twice.
      - If ``FileTooLargeToServe`` is raised (present, contained, visible, and
        larger than exec read cap), checks ``folder_unmoved``, resolves the
        canonical client path, and streams the ``(100 MiB, 256 MiB]`` mirror
        only when its ``file_size`` matches the live file's size; a mismatch or
        missing mirror for a ``> 100 MiB`` live file raises HTTP ``404``.
      - On transient/other sandbox errors, logs the miss and allows mirror
        fallback just like ``_shared_file_bytes``.
    - When the sandbox is not warm (stopped / cold):
      - Streams ``(100 MiB, 256 MiB]`` mirrors via :func:`shared_large_download`,
        or returns ``(None, None)`` to let upstream ``_shared_file_bytes`` read
        ``<= 100 MiB`` DB rows.
    """
    from src.server.app import share_files
    from src.server.services.workspace_file_containment import FileTooLargeToServe
    from src.server.services.workspace_files import to_client_path

    workspace_id = target.workspace_id
    owner_user_id = target.workspace["user_id"]
    sandbox = share_files.warm_sandbox(target.workspace, workspace_id)

    if sandbox is not None:
        try:
            resolved = await share_files.warm_sandbox_bytes(
                sandbox,
                normalized_path,
                work_dir=target.work_dir,
                visible=target.visible,
            )
        except FileTooLargeToServe as too_large:
            if not await share_files.folder_unmoved(
                workspace_id, target.workspace.get("dir_name")
            ):
                raise HTTPException(
                    status_code=404, detail=not_found_detail
                ) from None
            canonical_client_path = to_client_path(
                sandbox, too_large.canonical, target.work_dir
            )
            if not target.visible(canonical_client_path):
                raise HTTPException(
                    status_code=404, detail=not_found_detail
                ) from None
            streamed = await shared_large_download(
                workspace_id,
                owner_user_id,
                canonical_client_path,
                not_found_detail=not_found_detail,
                expected_size=too_large.size,
            )
            if streamed is not None:
                return streamed, None
            if too_large.size > SERVE_MAX_BYTES:
                raise HTTPException(
                    status_code=404, detail=not_found_detail
                ) from None
            return None, None
        except HTTPException:
            raise
        except Exception as exc:
            share_files._log_sandbox_miss("file", workspace_id, exc)
            streamed = await shared_large_download(
                workspace_id,
                owner_user_id,
                normalized_path,
                not_found_detail=not_found_detail,
            )
            return streamed, None
        else:
            if resolved is None:
                raise HTTPException(status_code=404, detail=not_found_detail)
            if not await share_files.folder_unmoved(
                workspace_id, target.workspace.get("dir_name")
            ):
                raise HTTPException(status_code=404, detail=not_found_detail)
            client_path, content = resolved
            return None, share_files.SharedBytes(
                client_path,
                content,
                share_files._mime_for(client_path, None),
                "sandbox",
            )

    streamed = await shared_large_download(
        workspace_id,
        owner_user_id,
        normalized_path,
        not_found_detail=not_found_detail,
    )
    return streamed, None
