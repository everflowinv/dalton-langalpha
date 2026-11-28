"""Unified stored-file range reading and streaming downloads for large files.

Covers stored workspace files in the ``(SERVE_MAX_BYTES, RELAY_MAX_BYTES]``
range (``(100 MiB, 256 MiB]``) for both stopped-workspace backup downloads and
public share downloads, plus SQL 1-based inline slicing so rolled-back large
inline rows stream in bounded ``8 MiB`` chunks without materializing the full
body in memory.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import mimetypes
import re
import shlex
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
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
_LIVE_DIGEST_TIMEOUT_S: int = 30
_ONLINE_ATTACH_TIMEOUT_S: float = 10.0
_ONLINE_ATTACH_CLOSE_TIMEOUT_S: float = 3.0
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


# Sentinel distinguishing "version field not provided" from a literal SQL NULL.
_UNSET: Any = object()


def _mirror_expected_sha256(file_meta: Mapping[str, Any]) -> str | None:
    """Return the validated 64-hex SHA-256 digest on a mirror row, or ``None``."""
    content_hash = str(file_meta.get("content_hash") or "").strip().lower()
    blob_sha256 = str(file_meta.get("blob_sha256") or "").strip().lower()
    if content_hash and blob_sha256 and content_hash != blob_sha256:
        return None
    digest = content_hash or blob_sha256
    if not digest or not _SHA256_HEX_RE.fullmatch(digest):
        return None
    return digest


def _live_file_digest_command(
    target: str,
    *,
    roots: Sequence[str],
    denied_roots: Sequence[str] = (),
) -> str:
    """Build a read-only shell probe that resolves containment, checks stat
    stability before/after hashing, and prints canonical path, stat line, and SHA-256.
    """
    from ptc_agent.core.sandbox.file_version import version_line
    from src.server.services.workspace_file_containment import (
        READ_CONTAINMENT,
        READ_MISSING,
        _RESOLVE_SH,
        _root_lines,
    )

    setup, patterns = _root_lines(roots)
    if not patterns:
        return f"exit {READ_CONTAINMENT}"
    denied_setup: list[str] = []
    denied_patterns: list[str] = []
    for index, root in enumerate(denied_roots):
        var = f"d{index}"
        denied_setup.append(
            f"{var}in={shlex.quote(root)}; "
            f'{var}=$(realpath -m -- "${var}in" 2>/dev/null) '
            f'|| {var}=$(readlink -f -- "${var}in" 2>/dev/null) '
            f'|| {var}=$(__la_rp "${var}in") || true; '
        )
        denied_patterns.extend([f'"${var}"', f'"${var}"/*'])
    denied_check = (
        f'case "$tt" in {"|".join(denied_patterns)}) '
        f"exit {READ_CONTAINMENT} ;; *) ;; esac; "
        if denied_patterns
        else ""
    )
    ver_line = version_line('"$tt"')
    hash_cmd = (
        "if command -v sha256sum >/dev/null 2>&1; then "
        'h=$(sha256sum -- "$tt" 2>/dev/null) || exit 9; h=${h%% *}; '
        "elif command -v shasum >/dev/null 2>&1; then "
        'h=$(shasum -a 256 -- "$tt" 2>/dev/null) || exit 9; h=${h%% *}; '
        "elif command -v openssl >/dev/null 2>&1; then "
        'h=$(openssl dgst -sha256 -r -- "$tt" 2>/dev/null) || exit 9; h=${h%% *}; '
        "else "
        "h=$(python3 -c 'import hashlib,sys; h=hashlib.sha256(); "
        'f=open(sys.argv[1],"rb"); '
        '[h.update(b) for b in iter(lambda:f.read(1<<20),b"")]; '
        "f.close(); print(h.hexdigest())' \"$tt\" 2>/dev/null) || exit 9; "
        "fi; "
    )
    return "".join(
        [
            _RESOLVE_SH,
            *setup,
            *denied_setup,
            f"t={shlex.quote(target)}; ",
            'tt=$(realpath -m -- "$t" 2>/dev/null) ',
            '|| tt=$(readlink -f -- "$t" 2>/dev/null) || tt=$(__la_rp "$t") ',
            f"|| exit {READ_CONTAINMENT}; ",
            f'[ -n "$tt" ] || exit {READ_CONTAINMENT}; ',
            f'case "$tt" in {"|".join(patterns)}) ;; ',
            f"*) exit {READ_CONTAINMENT} ;; esac; ",
            denied_check,
            f'[ -f "$tt" ] || exit {READ_MISSING}; ',
            'printf %s "$tt" | base64 | tr -d "\\n"; printf "\\n"; ',
            ver_line,
            'v1=$v; ',
            hash_cmd,
            ver_line,
            'v2=$v; ',
            '[ -n "$h" ] && [ "$v1" = "$v2" ] || exit 8; ',
            'printf "%s\\n%s\\n" "$v1" "$h"',
        ]
    )


async def read_live_file_version(
    sandbox: Any,
    absolute_path: str,
    *,
    work_dir: str,
) -> tuple[str, int, str] | None:
    """Return ``(canonical_path, size_bytes, sha256_hex)`` for a live contained file, or ``None``.

    Fails closed on containment escape, missing file, concurrent write while
    hashing, missing runtime, or any exec/parse error so a stale mirror is never
    served when the live version cannot be verified.
    """
    from ptc_agent.core.sandbox.file_version import line_size
    from src.server.services.workspace_file_containment import (
        _addresses_project,
        is_within,
        serving_roots,
    )

    if not _addresses_project(absolute_path, work_dir):
        return None
    runtime = getattr(sandbox, "runtime", None)
    if runtime is None:
        return None
    roots = serving_roots(sandbox, work_dir=work_dir)
    filesystem = getattr(getattr(sandbox, "config", None), "filesystem", None)
    denied_roots = tuple(getattr(filesystem, "denied_directories", ()) or ())
    try:
        result = await runtime.exec(
            _live_file_digest_command(
                absolute_path,
                roots=roots,
                denied_roots=denied_roots,
            ),
            timeout=_LIVE_DIGEST_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warning(
            "Live file digest check failed for %r: %s",
            absolute_path,
            exc,
        )
        return None

    if getattr(result, "exit_code", None) != 0:
        return None

    stdout = str(getattr(result, "stdout", "") or "")
    lines = stdout.splitlines()
    if len(lines) < 3 or not lines[0]:
        return None
    try:
        canonical = base64.b64decode(lines[0], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if not any(is_within(root, canonical) for root in roots):
        return None
    validate = getattr(sandbox, "validate_path", None)
    if callable(validate) and not validate(canonical):
        return None

    size = line_size(lines[1])
    digest = lines[2].strip().lower()
    if size is None or not _SHA256_HEX_RE.fullmatch(digest):
        return None
    return canonical, size, digest


@asynccontextmanager
async def attach_online_sandbox_readonly(
    workspace: Mapping[str, Any],
    workspace_id: str,
    *,
    timeout_s: float = _ONLINE_ATTACH_TIMEOUT_S,
    close_timeout_s: float = _ONLINE_ATTACH_CLOSE_TIMEOUT_S,
) -> AsyncIterator[Any | None]:
    """Attach a temporary running-only, non-waking sandbox handle on a session-less worker.

    Never calls ``get_session_for_workspace`` and never starts/restarts a stopped
    sandbox. Yields ``None`` when the sandbox cannot be reached or is not running,
    and always closes the temporary provider/sandbox handle on exit.
    """
    from ptc_agent.core.sandbox.attach_only import attach_only
    from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
    from ptc_agent.core.sandbox.runtime import RuntimeState
    from src.server.services.computer_manager._types import ComputerBinding
    from src.server.services.workspace_manager import WorkspaceManager

    durable_sandbox_id = workspace.get("sandbox_id")
    if not isinstance(durable_sandbox_id, str) or not durable_sandbox_id.strip():
        yield None
        return
    durable_sandbox_id = durable_sandbox_id.strip()

    provider: Any = None
    sandbox: Any = None

    async def _safe_close(obj: Any) -> None:
        if obj is None:
            return
        close_fn = getattr(obj, "close", None)
        if not callable(close_fn):
            return
        try:
            async with asyncio.timeout(close_timeout_s):
                await close_fn()
        except Exception:
            logger.debug("Ignoring error closing temporary readonly sandbox handle", exc_info=True)

    try:
        try:
            manager = WorkspaceManager.get_instance()
            computer_id = workspace.get("computer_id")
            computer_kind = workspace.get("computer_kind")
            binding = (
                ComputerBinding(
                    workspace_id=str(workspace_id),
                    computer_id=str(computer_id),
                    kind=str(computer_kind),
                    dir_name=workspace.get("dir_name"),
                    root_dir=workspace.get("computer_root_dir"),
                    provider_ref=durable_sandbox_id,
                    resource_tier=workspace.get("resource_tier"),
                    is_always_on=bool(workspace.get("is_always_on", False)),
                    provider_config=workspace.get("computer_provider_config"),
                    user_id=workspace.get("user_id"),
                )
                if computer_id and computer_kind
                else None
            )
            core_config = manager._core_config_for(binding)
            provider = manager._provider_for(binding)
            async with asyncio.timeout(timeout_s):
                runtime = await provider.get(durable_sandbox_id)
                state = await runtime.get_state()
                is_running = (
                    state == RuntimeState.RUNNING
                    or getattr(state, "value", None) == "running"
                )
                if is_running:
                    work_dir = (
                        await runtime.fetch_working_dir()
                        or (binding.root_dir if binding is not None else None)
                        or core_config.filesystem.working_directory
                    )
                else:
                    work_dir = None

            if not is_running:
                p_close, provider = provider, None
                await _safe_close(p_close)
            else:
                sandbox = PTCSandbox(
                    core_config,
                    None,
                    resource_tier=workspace.get("resource_tier"),
                    host_unavailable_policy=None,
                    provider=provider,
                    allow_reconnect=False,
                )
                provider = None
                sandbox.runtime = runtime
                sandbox.sandbox_id = durable_sandbox_id
                sandbox._work_dir = work_dir
        except Exception as exc:
            logger.warning(
                "Temporary non-waking attach failed for workspace=%s sandbox=%s: %s",
                workspace_id,
                durable_sandbox_id,
                exc,
            )
            s_close, sandbox = sandbox, None
            p_close, provider = provider, None
            await _safe_close(s_close)
            await _safe_close(p_close)

        if sandbox is None:
            yield None
        else:
            with attach_only():
                yield sandbox
    finally:
        s_close, sandbox = sandbox, None
        p_close, provider = provider, None
        await _safe_close(s_close)
        await _safe_close(p_close)


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
    expected_sha256: str | None = None,
    require_version_match: bool = False,
) -> Response | None:
    """Stream a ``(100 MiB, 256 MiB]`` stored file for a public share download.

    Caller preconditions (NOT re-checked here):
    - ``resolve_shared_files(share_token, require_download=True)`` has already
      succeeded, including any downgrade ``403`` (``allow_files=false`` with
      ``Cache-Control: no-store`` and ``Clear-Site-Data: "cache"``).
    - ``normalized_path`` has been validated against traversal and passed
      ``target.visible(normalized_path)``.

    When ``expected_size`` / ``expected_sha256`` (or ``require_version_match=True``)
    is supplied from a live sandbox check and disagrees with the mirrored row
    (or the digest is missing/unverifiable), a stale mirror row is never served
    (raises HTTP ``404``).

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

    if require_version_match or expected_sha256 is not None:
        wanted_sha256 = str(expected_sha256 or "").strip().lower()
        mirror_sha256 = _mirror_expected_sha256(file_meta)
        if (
            not wanted_sha256
            or not _SHA256_HEX_RE.fullmatch(wanted_sha256)
            or not mirror_sha256
            or mirror_sha256 != wanted_sha256
        ):
            logger.warning(
                "Shared large mirror digest disagrees with live sandbox workspace=%s path=%r "
                "mirror_sha256=%r live_sha256=%r",
                workspace_id,
                normalized_path,
                mirror_sha256,
                wanted_sha256 or None,
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


async def _serve_from_live_sandbox(
    sandbox: Any,
    target: Any,
    normalized_path: str,
    *,
    not_found_detail: str,
) -> tuple[Response | None, Any | None]:
    """Check and serve a shared file against a live (cached or attached) sandbox.

    Never falls back to an unverified persisted mirror on miss, escape,
    same-size replacement, or sandbox error (always fails closed with 404).
    """
    from src.server.app import share_files
    from src.server.services.workspace_file_containment import FileTooLargeToServe
    from src.server.services.workspace_files import to_client_path

    workspace_id = target.workspace_id
    owner_user_id = target.workspace["user_id"]

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

        live_version = await read_live_file_version(
            sandbox,
            too_large.canonical,
            work_dir=target.work_dir,
        )
        if live_version is None:
            logger.warning(
                "Shared large file live digest unavailable workspace=%s path=%r; refusing mirror",
                workspace_id,
                canonical_client_path,
            )
            raise HTTPException(
                status_code=404, detail=not_found_detail
            ) from None
        live_canonical, live_size, live_sha256 = live_version
        live_client_path = to_client_path(sandbox, live_canonical, target.work_dir)
        if (
            live_size != too_large.size
            or not target.visible(live_client_path)
            or not await share_files.folder_unmoved(
                workspace_id, target.workspace.get("dir_name")
            )
        ):
            raise HTTPException(
                status_code=404, detail=not_found_detail
            ) from None

        if live_size > SERVE_MAX_BYTES:
            streamed = await shared_large_download(
                workspace_id,
                owner_user_id,
                live_client_path,
                not_found_detail=not_found_detail,
                expected_size=live_size,
                expected_sha256=live_sha256,
                require_version_match=True,
            )
            if streamed is None:
                raise HTTPException(
                    status_code=404, detail=not_found_detail
                ) from None
            return streamed, None

        # (EXEC_READ_MAX_BYTES, SERVE_MAX_BYTES]: verify mirror digest before serving.
        try:
            mirror_meta = await get_large_file_meta(workspace_id, live_client_path)
        except RuntimeError as exc:
            if "database pool is not open" in str(exc):
                mirror_meta = None
            else:
                raise
        if (
            mirror_meta is None
            or int(mirror_meta.get("file_size") or 0) != live_size
            or _mirror_expected_sha256(mirror_meta) != live_sha256
        ):
            raise HTTPException(
                status_code=404, detail=not_found_detail
            ) from None
        record = await share_files.FilePersistenceService.get_file_content(
            workspace_id, live_client_path
        )
        if not record:
            raise HTTPException(
                status_code=404, detail=not_found_detail
            ) from None
        content = await share_files.resolve_file_bytes_or_none(
            record,
            user_id=owner_user_id,
            context=f"reading verified large shared file in workspace {workspace_id}",
        )
        if (
            content is None
            or len(content) != live_size
            or hashlib.sha256(content).hexdigest() != live_sha256
        ):
            raise HTTPException(
                status_code=404, detail=not_found_detail
            ) from None
        return None, share_files.SharedBytes(
            live_client_path,
            content,
            share_files._mime_for(live_client_path, record.get("mime_type")),
            "database",
        )
    except HTTPException:
        raise
    except Exception as exc:
        share_files._log_sandbox_miss("file", workspace_id, exc)
        raise HTTPException(status_code=404, detail=not_found_detail) from None
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


async def shared_large_download_or_warm(
    target: Any,
    normalized_path: str,
    *,
    not_found_detail: str = "File not found",
) -> tuple[Response | None, Any | None]:
    """Enforce live-sandbox-first semantics using shared DB online state.

    Returns ``(streamed_response, warm_shared_bytes)``:
    - Determines whether the sandbox is online from cross-process DB state
      (:func:`is_sandbox_online`), not process-local session memory alone.
    - When this worker holds a warm session, checks the live sandbox directly.
    - When DB says the sandbox is online but this worker holds no session,
      performs a non-waking running-only attach (:func:`attach_online_sandbox_readonly`)
      and checks the live file; if the live sandbox cannot be reached or verified,
      raises HTTP ``404`` without falling back to the persisted mirror.
    - For ``FileTooLargeToServe`` on an online sandbox, verifies the live file's
      SHA-256 digest and size against the mirror row before streaming; any
      mismatch (including same-size replacements) or missing digest raises ``404``.
    - Only when DB says the sandbox is offline/stopped (and no warm handle exists)
      does this stream ``(100 MiB, 256 MiB]`` mirrors via :func:`shared_large_download`
      or return ``(None, None)`` for ``<= 100 MiB`` DB rows.
    """
    from src.server.app import share_files
    from src.server.services.warm_files import is_sandbox_online

    workspace_id = target.workspace_id
    owner_user_id = target.workspace["user_id"]
    sandbox = share_files.warm_sandbox(target.workspace, workspace_id)

    if sandbox is not None:
        return await _serve_from_live_sandbox(
            sandbox,
            target,
            normalized_path,
            not_found_detail=not_found_detail,
        )

    if is_sandbox_online(target.workspace):
        async with attach_online_sandbox_readonly(
            target.workspace, workspace_id
        ) as attached:
            if attached is None:
                logger.warning(
                    "Shared download refused: workspace=%s status=%r is online in DB "
                    "but live sandbox is unavailable on this worker; not falling back to mirror",
                    workspace_id,
                    target.workspace.get("status"),
                )
                raise HTTPException(status_code=404, detail=not_found_detail)
            return await _serve_from_live_sandbox(
                attached,
                target,
                normalized_path,
                not_found_detail=not_found_detail,
            )

    streamed = await shared_large_download(
        workspace_id,
        owner_user_id,
        normalized_path,
        not_found_detail=not_found_detail,
    )
    return streamed, None
