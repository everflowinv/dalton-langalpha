#!/usr/bin/env python3
"""Durable retrieval journal for one AI-finance batch.

Successful search and full-document responses are stored under that batch and
replayed before another upstream call. A full-document call records an
in-flight marker first. If that call has no durable confirmed response, a
later attempt reports it unresolved and does not send it again.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RetrievalCacheCorrupt(RuntimeError):
    """The batch journal cannot be trusted, so no upstream call is made."""


class UnresolvedRetrieval(RuntimeError):
    """A full-document call has no durable confirmed response."""


class ConfirmedRetrievalFailure(Exception):
    """A finished full-document response that is not a usable page.

    The journal stores this and replays it. The scan keeps it on that
    document. It is not an unresolved call and it is not sent again.
    """


_TOOLS = {"search_library", "get_document"}
# Closed set written by this process. A record's error_type is one of these
# tokens, not an arbitrary class name.
_CONFIRMED_FAILURES = {
    "RuntimeError": RuntimeError,
    "ConfirmedRetrievalFailure": ConfirmedRetrievalFailure,
}
_CALL_NAME = re.compile(r"^(?:search_library|get_document)-[0-9a-f]{64}\.json$")
_MAX_RECORD_BYTES = 4 * 1024 * 1024
_BLOCKED_PARTS = {".openclaw", ".feishu-daily-ai-finance"}
# Threads in one process do not exclude each other with flock. These locks are
# keyed by the journal file so two workers share one in-flight call.
_CALL_GUARDS: dict[str, threading.Lock] = {}
_CALL_GUARD = threading.Lock()


def open_batch_retrieval(
    batch_dir: Path,
    *,
    batch_id: str,
    started_at: datetime,
    hours: int,
    max_documents: int,
    queries: list[str],
) -> BatchRetrieval:
    """Open `{batch}/.retrieval`, bound to this frozen batch."""
    root = _require_batch_dir(batch_dir, batch_id)
    journal = root / ".retrieval"
    if _is_symlink(journal):
        raise RetrievalCacheCorrupt("retrieval journal must not be a symlink")
    journal.mkdir(exist_ok=True)
    info = journal.lstat()
    if not stat.S_ISDIR(info.st_mode) or _is_symlink(journal):
        raise RetrievalCacheCorrupt("retrieval journal must be a real directory")
    binding = _binding(
        batch_id=batch_id,
        started_at=started_at,
        hours=hours,
        max_documents=max_documents,
        queries=queries,
    )
    return BatchRetrieval(journal, binding)


class BatchRetrieval:
    """Per-batch journal. One instance is shared by the scan's workers."""

    def __init__(self, root: Path, binding: dict[str, Any]) -> None:
        self.root = root
        self.binding = binding
        self._calls = root / "calls"
        self._locks = root / "locks"
        _require_real_directory(self._calls)
        _require_real_directory(self._locks)
        self._ensure_binding()
        self._audit()

    def wrap(self, rpc: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        """Return an rpc with the same tool/arguments/timeout signature."""

        def cached(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
            if tool == "auth_status" or tool not in _TOOLS:
                return rpc(tool, arguments, timeout=timeout)

            def invoke() -> dict[str, Any]:
                return rpc(tool, arguments, timeout=timeout)

            return self.call(tool, arguments, invoke)

        return cached

    def call(
        self,
        tool: str,
        arguments: dict[str, Any],
        invoke: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Replay a confirmed call, or perform it once and store the outcome."""
        if tool not in _TOOLS:
            raise RetrievalCacheCorrupt("retrieval tool is not part of this batch")
        if not isinstance(arguments, dict):
            raise RetrievalCacheCorrupt("retrieval arguments must be an object")
        frozen_args = json.loads(_canonical(arguments))
        name = _call_name(tool, frozen_args)
        with _process_lock(str(self._calls / name)):
            with self._file_lock(name):
                path = self._calls / name
                record = _read_record(path) if _entry_exists(path) else None
                if record is None and tool == "search_library" and self._has_document_call():
                    raise UnresolvedRetrieval("search discovery is not durable for this batch")
                if record is not None:
                    return _replay(record, tool, frozen_args)
                if tool == "get_document":
                    try:
                        _write_record(path, {
                            "schema": 1,
                            "state": "inflight",
                            "tool": tool,
                            "args": frozen_args,
                        })
                    except OSError as exc:
                        raise UnresolvedRetrieval("full-document request was not durable") from exc
                try:
                    value = invoke()
                except Exception as exc:
                    if tool != "get_document":
                        raise
                    # A finished RuntimeError or ConfirmedRetrievalFailure is stored
                    # and replayed. A timeout or any other failure stays in flight:
                    # the call may have been sent, and it is not sent again.
                    failure = _confirmed_failure_name(exc)
                    if failure is None:
                        raise UnresolvedRetrieval(
                            "full-document failure has no durable confirmed response"
                        ) from exc
                    try:
                        _write_error(path, tool, frozen_args, failure, str(exc))
                    except Exception as persist_exc:
                        raise UnresolvedRetrieval("full-document result was not durable") from persist_exc
                    raise
                if not isinstance(value, dict):
                    if tool == "get_document":
                        try:
                            _write_error(
                                path,
                                tool,
                                frozen_args,
                                "RuntimeError",
                                "retrieval payload must be an object",
                            )
                        except Exception as persist_exc:
                            raise UnresolvedRetrieval("full-document result was not durable") from persist_exc
                    raise RuntimeError("retrieval payload must be an object")
                try:
                    _write_value(path, tool, frozen_args, value)
                except Exception as exc:
                    if tool == "get_document":
                        raise UnresolvedRetrieval("full-document result was not durable") from exc
                    raise RetrievalCacheCorrupt("search result was not durable") from exc
                return json.loads(_canonical(value))

    def _ensure_binding(self) -> None:
        path = self.root / "binding.json"
        if not _entry_exists(path):
            _write_record(path, self.binding)
            return
        found = _read_json(path)
        if found != self.binding:
            raise RetrievalCacheCorrupt("retrieval journal does not belong to this frozen batch")

    def _audit(self) -> None:
        try:
            info = self._calls.lstat()
        except OSError as exc:
            raise RetrievalCacheCorrupt("retrieval call directory is not a real directory") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RetrievalCacheCorrupt("retrieval call directory is not a real directory")
        for path in self._calls.iterdir():
            if path.name.startswith("."):
                continue
            if _is_symlink(path) or not path.is_file() or _CALL_NAME.fullmatch(path.name) is None:
                raise RetrievalCacheCorrupt("unexpected retrieval journal entry")
            _read_record(path)

    def _has_document_call(self) -> bool:
        try:
            entries = list(self._calls.iterdir())
        except OSError as exc:
            raise RetrievalCacheCorrupt("retrieval call directory is not a real directory") from exc
        return any(
            path.name.startswith("get_document-") and not _is_symlink(path)
            for path in entries
        )

    @contextlib.contextmanager
    def _file_lock(self, name: str) -> Iterator[None]:
        _require_real_directory(self._locks)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self._locks / name, flags, 0o600)
        except OSError as exc:
            raise RetrievalCacheCorrupt("retrieval lock is not a real file") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _process_lock(key: str) -> threading.Lock:
    with _CALL_GUARD:
        lock = _CALL_GUARDS.get(key)
        if lock is None:
            lock = threading.Lock()
            _CALL_GUARDS[key] = lock
        return lock


def _binding(
    *,
    batch_id: str,
    started_at: datetime,
    hours: int,
    max_documents: int,
    queries: list[str],
) -> dict[str, Any]:
    if started_at.tzinfo is None or started_at.tzinfo.utcoffset(started_at) is None:
        raise RetrievalCacheCorrupt("batch instant must be timezone-aware")
    if type(hours) is not int or type(max_documents) is not int or max_documents < 1:
        raise RetrievalCacheCorrupt("batch retrieval bounds are not usable")
    if not isinstance(queries, list) or not queries or any(type(query) is not str or not query for query in queries):
        raise RetrievalCacheCorrupt("batch queries are not usable")
    return {
        "schema": 1,
        "batch_id": batch_id,
        "started_at": started_at.astimezone(timezone.utc).isoformat(),
        "hours": hours,
        "max_documents": max_documents,
        "queries": queries,
    }


def _require_real_directory(path: Path) -> None:
    try:
        if _entry_exists(path) and (_is_symlink(path) or not stat.S_ISDIR(path.lstat().st_mode)):
            raise RetrievalCacheCorrupt("retrieval journal directory is not a real directory")
        path.mkdir(exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise RetrievalCacheCorrupt("retrieval journal directory is not a real directory") from exc
    if _is_symlink(path) or not stat.S_ISDIR(info.st_mode):
        raise RetrievalCacheCorrupt("retrieval journal directory is not a real directory")


def _require_batch_dir(batch_dir: Path, batch_id: str) -> Path:
    if not isinstance(batch_dir, Path) or not batch_dir.is_absolute() or not isinstance(batch_id, str):
        raise RetrievalCacheCorrupt("batch directory must be an absolute path")
    if batch_id in {"", ".", ".."} or any(part in _BLOCKED_PARTS for part in batch_dir.parts):
        raise RetrievalCacheCorrupt("retrieval journal path is not available")
    if batch_dir.name != batch_id or _is_symlink(batch_dir) or not batch_dir.is_dir():
        raise RetrievalCacheCorrupt("retrieval journal is not bound to this batch directory")
    return batch_dir


def _call_name(tool: str, arguments: dict[str, Any]) -> str:
    digest = hashlib.sha256(_canonical({"args": arguments, "tool": tool})).hexdigest()
    return f"{tool}-{digest}.json"


def _check_record(record: dict[str, Any], tool: str, arguments: dict[str, Any]) -> None:
    if record.get("schema") != 1 or record.get("tool") != tool:
        raise RetrievalCacheCorrupt("retrieval record does not match the requested call")
    if _canonical(record.get("args")) != _canonical(arguments):
        raise RetrievalCacheCorrupt("retrieval record does not match the requested call")
    state = record.get("state")
    if state == "inflight":
        if tool != "get_document":
            raise RetrievalCacheCorrupt("only a full-document call may be in flight")
        return
    if state != "confirmed":
        raise RetrievalCacheCorrupt("retrieval record state is not usable")
    outcome = record.get("outcome")
    if outcome == "value":
        value = record.get("value")
        if not isinstance(value, dict) or _value_sha(value) != record.get("sha256"):
            raise RetrievalCacheCorrupt("retrieval value hash does not match")
        return
    if outcome == "error":
        failure = record.get("error_type")
        message = record.get("message")
        if failure not in _CONFIRMED_FAILURES or not isinstance(message, str):
            raise RetrievalCacheCorrupt("retrieval error record is not usable")
        if _error_sha(failure, message) != record.get("sha256"):
            raise RetrievalCacheCorrupt("retrieval error hash does not match")
        return
    raise RetrievalCacheCorrupt("retrieval outcome is not usable")


def _replay(record: dict[str, Any], tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    _check_record(record, tool, arguments)
    if record.get("state") == "inflight":
        raise UnresolvedRetrieval("full-document call has no confirmed response")
    if record.get("outcome") == "error":
        raise _CONFIRMED_FAILURES[record["error_type"]](record["message"])
    return json.loads(_canonical(record["value"]))


def _write_value(path: Path, tool: str, arguments: dict[str, Any], value: dict[str, Any]) -> None:
    _write_record(path, {
        "schema": 1,
        "state": "confirmed",
        "tool": tool,
        "args": arguments,
        "outcome": "value",
        "value": json.loads(_canonical(value)),
        "sha256": _value_sha(value),
    })


def _confirmed_failure_name(exc: BaseException) -> str | None:
    if type(exc) is RuntimeError:
        return "RuntimeError"
    if type(exc) is ConfirmedRetrievalFailure:
        return "ConfirmedRetrievalFailure"
    return None


def _write_error(
    path: Path,
    tool: str,
    arguments: dict[str, Any],
    failure: str,
    message: str,
) -> None:
    if failure not in _CONFIRMED_FAILURES:
        raise RetrievalCacheCorrupt("retrieval error record is not usable")
    _write_record(path, {
        "schema": 1,
        "state": "confirmed",
        "tool": tool,
        "args": arguments,
        "outcome": "error",
        "error_type": failure,
        "message": message,
        "sha256": _error_sha(failure, message),
    })


def _value_sha(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _error_sha(failure: str, message: str) -> str:
    return hashlib.sha256(_canonical({"error_type": failure, "message": message})).hexdigest()


def _read_record(path: Path) -> dict[str, Any]:
    if path.name != "binding.json" and _CALL_NAME.fullmatch(path.name) is None:
        raise RetrievalCacheCorrupt("unexpected retrieval journal entry")
    record = _read_json(path)
    if not isinstance(record, dict) or record.get("schema") != 1:
        raise RetrievalCacheCorrupt("retrieval record is not usable")
    if path.name == "binding.json":
        return record
    tool = record.get("tool")
    args = record.get("args")
    if tool not in _TOOLS or not isinstance(args, dict) or _call_name(tool, args) != path.name:
        raise RetrievalCacheCorrupt("retrieval record name does not match its call")
    _check_record(record, tool, args)
    return record


def _read_json(path: Path) -> Any:
    try:
        payload = _read_regular(path)
    except OSError as exc:
        raise RetrievalCacheCorrupt("retrieval record cannot be read") from exc
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RetrievalCacheCorrupt("retrieval record is corrupt") from exc


def _write_record(path: Path, record: dict[str, Any]) -> None:
    if _is_symlink(path):
        raise RetrievalCacheCorrupt("refusing to replace a symlink in the retrieval journal")
    _atomic_write(path, _canonical(record) + b"\n")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _read_regular(path: Path) -> bytes:
    if _is_symlink(path):
        raise RetrievalCacheCorrupt("retrieval record must be a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RetrievalCacheCorrupt("retrieval record must be a regular file") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_RECORD_BYTES:
            raise RetrievalCacheCorrupt("retrieval record is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            block = os.read(fd, min(1024 * 1024, remaining))
            if not block:
                break
            remaining -= len(block)
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return False
