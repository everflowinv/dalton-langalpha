#!/usr/bin/env python3
"""Save one market-digest body, archive it, and index that body.

The caller supplies an operator config and a prepared automation execution.
This module reads the execution binding and the saved input batch, then stores
the exact Markdown. It does not fetch mail, call an embedding API, send a
card, or accept a recipient. The backend confirms delivery before it calls
``finalize_output`` and injects an embedder.

Embedding retry is explicit and bounded. One ``finalize_output`` call invokes
the embedder at most once, and only outside any SQLite write transaction.
A raised embedder, or a process stop before the cache file is durable, leaves
no successful cache. A later ``finalize_output`` for that same saved body may
call the embedder again. No extra approval is required, and a started status
without a cache does not stick. Invalid vectors are not cached, so a later
call may try once more. A durable cache keyed by body hash, embedding model,
and chunking version is reused and the embedder is not called. A matching
``dalton_digest_commits`` row is authoritative: finalize does not embed again
and does not rewrite chunks. This function never loops.

Lock order is the per-batch body lock, then the date-wide archive lock.
The archive lock is shared by AM and PM. Neither lock is re-entered.
The same-batch lock serializes a duplicate finalize. On macOS ``flock`` is
per process, so each lock pairs a process-local thread lock with ``flock``.
"""

from __future__ import annotations

import array
import base64
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import fcntl
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
from typing import Any, Protocol

def _bind_input_api():
    """Load the frozen input API from either install layout.

    Repository tests import ``scripts.dalton.market_digest_inputs``. The
    bridge launchd process inserts ``scripts/dalton`` on ``sys.path`` and does
    not provide a ``scripts`` package, so the fallback is ``market_digest_inputs``.
    """

    try:
        module = importlib.import_module("scripts.dalton.market_digest_inputs")
    except ImportError:
        module = importlib.import_module("market_digest_inputs")
    return module.get_execution_binding, module.read_inputs


get_execution_binding, read_inputs = _bind_input_api()

MAX_BODY_BYTES = 128 * 1024
MAX_CONFIG_BYTES = 8 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_CHUNK_CHARS = 3000
MAX_EMBEDDING_DIMENSION = 8192
CHUNKING_VERSION = 1
SCHEMA = 1
SQLITE_TIMEOUT_SECONDS = 5.0
SOURCE_LABEL = "\U0001f4dd \u6765\u6e90"
SOURCE_HEADING = f"## {SOURCE_LABEL}"
_PERIODS = frozenset({"AM", "PM"})
_CONFIG_KEYS = frozenset(
    {
        "input_state_root",
        "body_state_root",
        "market_memory_dir",
        "index_db_path",
        "embedding_model",
    }
)
_CHUNK_COLUMNS = frozenset(
    {
        "id",
        "date",
        "period",
        "section_id",
        "section_label",
        "content",
        "source_emails",
        "embedding",
        "created_at",
    }
)
_TITLES = {
    "AM": "# \U0001f4ca \u5e02\u573a\u6668\u62a5 \u2014 {date}",
    "PM": "# \U0001f4ca \u5e02\u573a\u665a\u62a5 \u2014 {date}",
}
_HEADING_RE = re.compile(r"^## .+$", re.MULTILINE)
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,127}$")
_TOKEN_RE = re.compile(r"^[a-z0-9_]{1,48}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class OutputConfigError(ValueError):
    """Operator config is missing, relative, or not allowed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Embedder(Protocol):
    """Injected by the bridge. This module does not implement it."""

    def __call__(self, texts: list[str], model: str) -> list[list[float]]: ...


class _Rejected(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OutputConfig:
    """Absolute operator paths and the embedding model name. No secrets."""

    input_state_root: Path
    body_state_root: Path
    market_memory_dir: Path
    index_db_path: Path
    embedding_model: str


def load_output_config(config_path: Path) -> OutputConfig:
    """Read a small JSON object of absolute paths and one model name."""

    try:
        path = _config_file(config_path)
        raw = _read_owned(path, MAX_CONFIG_BYTES)
        loaded = json.loads(raw.decode("utf-8"))
    except _Rejected as exc:
        raise OutputConfigError(exc.code) from None
    except (UnicodeError, json.JSONDecodeError):
        raise OutputConfigError("config_invalid") from None
    if not isinstance(loaded, dict):
        raise OutputConfigError("config_invalid")
    unknown = [str(key) for key in loaded if str(key) not in _CONFIG_KEYS]
    if unknown:
        raise OutputConfigError("config_secret" if _looks_secret(unknown) else "config_invalid")
    if set(loaded) != _CONFIG_KEYS:
        raise OutputConfigError("config_invalid")
    try:
        return _build_config(loaded)
    except _Rejected as exc:
        raise OutputConfigError(exc.code) from None


def save_output(
    config: OutputConfig,
    automation_execution_id: str,
    period: str,
    markdown: str,
) -> dict[str, Any]:
    """Persist exact Markdown for the bound period and authoritative date.

    The body is stored before archive or index. The same batch and the same
    bytes reuse the saved record. A different body for that batch conflicts.
    """

    try:
        checked = _checked_config(config)
        selected = _require_period(period)
        bound = _bound_inputs(checked, automation_execution_id, selected)
        validated = _validate_markdown(markdown, selected, bound["date"])
        digest = hashlib.sha256(validated["payload"]).hexdigest()
        with _file_lock(_batch_lock_path(checked, selected, bound["date"])):
            return _save_locked(checked, bound, validated, digest)
    except _Rejected as exc:
        return _fail(exc.code)


def get_output(
    config: OutputConfig,
    execution_id: str,
    period: str,
    *,
    include_body: bool = False,
) -> dict[str, Any]:
    """Return saved metadata. ``include_body`` adds the stored Markdown."""

    if not isinstance(include_body, bool):
        return _fail("invalid_argument")
    try:
        checked = _checked_config(config)
        selected = _require_period(period)
        bound = _bound_inputs(checked, execution_id, selected)
        with _file_lock(_batch_lock_path(checked, selected, bound["date"])):
            stored = _load_saved(checked, bound)
            _reconcile_authorities(checked, stored)
            public = _public_record(stored["metadata"])
            if include_body:
                public["markdown"] = stored["markdown"]
            public["ok"] = True
            public["code"] = "ok"
            return public
    except _Rejected as exc:
        return _fail(exc.code)


def finalize_output(
    config: OutputConfig,
    execution_id: str,
    period: str,
    body_sha256: str,
    *,
    embedder: Embedder,
) -> dict[str, Any]:
    """Archive and index a body that is already saved and unchanged.

    ``embedder`` is called at most once. See the module docstring for the
    cache, restart, and retry rules.
    """

    if not callable(embedder):
        return _fail("invalid_argument")
    try:
        checked = _checked_config(config)
        selected = _require_period(period)
        if not isinstance(body_sha256, str) or _HEX64_RE.fullmatch(body_sha256) is None:
            raise _Rejected("hash_mismatch")
        bound = _bound_inputs(checked, execution_id, selected)
        with _file_lock(_batch_lock_path(checked, selected, bound["date"])):
            return _finalize_locked(checked, bound, body_sha256, embedder)
    except _Rejected as exc:
        return _fail(exc.code)


def _looks_secret(keys: list[str]) -> bool:
    markers = ("secret", "token", "password", "credential", "api_key", "apikey")
    for key in keys:
        lowered = key.lower().replace("-", "_")
        if any(marker in lowered for marker in markers):
            return True
    return False


def _build_config(raw: Mapping[str, Any]) -> OutputConfig:
    model = raw["embedding_model"]
    if not isinstance(model, str) or _MODEL_RE.fullmatch(model) is None or ".." in model:
        raise _Rejected("config_invalid")
    roots = {
        "input_state_root": _optional_dir(raw["input_state_root"], must_exist=True),
        "body_state_root": _optional_dir(raw["body_state_root"], must_exist=False),
        "market_memory_dir": _optional_dir(raw["market_memory_dir"], must_exist=False),
    }
    if len({roots["input_state_root"], roots["body_state_root"], roots["market_memory_dir"]}) < 3:
        raise _Rejected("config_invalid")
    database = _database_file(raw["index_db_path"])
    return OutputConfig(
        input_state_root=roots["input_state_root"],
        body_state_root=roots["body_state_root"],
        market_memory_dir=roots["market_memory_dir"],
        index_db_path=database,
        embedding_model=model,
    )


def _checked_config(config: OutputConfig) -> OutputConfig:
    if not isinstance(config, OutputConfig):
        raise _Rejected("invalid_config")
    return _build_config(
        {
            "input_state_root": config.input_state_root,
            "body_state_root": config.body_state_root,
            "market_memory_dir": config.market_memory_dir,
            "index_db_path": config.index_db_path,
            "embedding_model": config.embedding_model,
        }
    )


def _config_file(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise _Rejected("config_not_absolute")
    normalized = Path(os.path.normpath(path))
    info = _lstat(normalized)
    if info is None or not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise _Rejected("config_invalid")
    if info.st_uid != os.getuid():
        raise _Rejected("config_path_rejected")
    return normalized


def _path_string(value: object) -> Path:
    if not isinstance(value, (str, Path)):
        raise _Rejected("config_invalid")
    text = str(value)
    if not text or "\x00" in text or "\n" in text or "\r" in text:
        raise _Rejected("config_invalid")
    path = Path(text)
    if not path.is_absolute():
        raise _Rejected("config_not_absolute")
    return Path(os.path.normpath(path))


def _optional_dir(value: object, *, must_exist: bool) -> Path:
    path = _path_string(value)
    info = _lstat(path)
    if info is not None and stat.S_ISLNK(info.st_mode):
        raise _Rejected("config_path_rejected")
    if must_exist:
        if info is None or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise _Rejected("config_path_rejected")
        return path
    if info is None:
        parent = _lstat(path.parent)
        if parent is None or not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode):
            raise _Rejected("config_path_rejected")
        return path
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise _Rejected("config_path_rejected")
    return path


def _database_file(value: object) -> Path:
    path = _path_string(value)
    info = _lstat(path)
    if (
        info is None
        or stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
    ):
        raise _Rejected("index_db_invalid")
    try:
        header = _read_prefix(path, 16)
    except _Rejected:
        raise _Rejected("index_db_invalid") from None
    if header != b"SQLite format 3\x00":
        raise _Rejected("index_db_invalid")
    return path


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise _Rejected("state_rejected") from None


def _read_prefix(path: Path, size: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        raise _Rejected("state_rejected") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise _Rejected("state_rejected")
        return os.read(fd, size)
    finally:
        os.close(fd)


def _read_owned(path: Path, max_bytes: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        raise _Rejected("state_rejected") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise _Rejected("state_rejected")
        if info.st_size > max_bytes:
            raise _Rejected("state_too_large")
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            block = os.read(fd, min(1024 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _mkdir(path: Path) -> None:
    info = _lstat(path)
    if info is not None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise _Rejected("state_rejected")
        if info.st_uid != os.getuid():
            raise _Rejected("state_rejected")
        return
    parent = path.parent
    parent_info = _lstat(parent)
    if parent_info is None or stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise _Rejected("state_rejected")
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        _mkdir(path)
    except OSError:
        raise _Rejected("state_rejected") from None


def _child(root: Path, *parts: str) -> Path:
    current = root
    for part in parts:
        if part in {"", ".", ".."} or "/" in part or "\\" in part or "\x00" in part:
            raise _Rejected("state_rejected")
        current = current / part
        info = _lstat(current)
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise _Rejected("state_rejected")
    try:
        current.relative_to(root)
    except ValueError:
        raise _Rejected("state_rejected") from None
    return current


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    parent = path.parent
    _mkdir(parent)
    info = _lstat(path)
    if info is not None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise _Rejected("state_rejected")
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    except OSError:
        raise _Rejected("state_rejected") from None
    tmp_path = Path(tmp_name)
    closed = False
    committed = False
    try:
        os.fchmod(fd, mode)
        view = memoryview(payload)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
        os.fsync(fd)
        os.close(fd)
        closed = True
        os.replace(tmp_path, path)
        committed = True
        _fsync_dir(parent)
    except _Rejected:
        raise
    except OSError:
        raise _Rejected("state_rejected") from None
    finally:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        if not committed:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def _file_lock(path: Path):
    """One non-reentrant thread lock plus an exclusive flock."""

    _mkdir(path.parent)
    existing = _lstat(path)
    if existing is not None and stat.S_ISLNK(existing.st_mode):
        raise _Rejected("state_rejected")
    thread_lock = _thread_lock(path)
    with thread_lock:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except OSError:
            raise _Rejected("state_rejected") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise _Rejected("state_rejected")
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _batch_lock_path(config: OutputConfig, period: str, date_value: str) -> Path:
    _mkdir(config.body_state_root)
    return _child(config.body_state_root, "locks", f"batch-{period}-{date_value}.lock")


def _archive_lock_path(config: OutputConfig, date_value: str) -> Path:
    _mkdir(config.market_memory_dir)
    return _child(config.market_memory_dir, "locks", f"date-{date_value}.lock")


def _batch_dir(config: OutputConfig, period: str, date_value: str) -> Path:
    """Return the period/date directory without creating the period parent."""

    _mkdir(config.body_state_root)
    return _child(config.body_state_root, period, date_value)


def _ensure_batch_dir(config: OutputConfig, period: str, date_value: str) -> Path:
    """Create the period directory, then the date directory, under the body root.

    ``_mkdir`` only creates a directory whose parent already exists. The first
    save must create ``period`` before ``period/date``.
    """

    root = config.body_state_root
    _mkdir(root)
    period_dir = _child(root, period)
    _mkdir(period_dir)
    directory = _child(period_dir, date_value)
    _mkdir(directory)
    return directory


def _require_period(period: object) -> str:
    if period not in _PERIODS:
        raise _Rejected("invalid_argument")
    return str(period)


def _valid_date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    if parsed.strftime("%Y-%m-%d") != value:
        return None
    return value


def _safe_token(value: object) -> str | None:
    if isinstance(value, str) and _TOKEN_RE.fullmatch(value):
        return value
    return None


def _bound_inputs(config: OutputConfig, execution_id: object, period: str) -> dict[str, Any]:
    if not isinstance(execution_id, str):
        raise _Rejected("invalid_argument")
    try:
        binding = get_execution_binding(config.input_state_root, execution_id)
    except AssertionError:
        raise
    except Exception:
        raise _Rejected("binding_corrupt") from None
    if not isinstance(binding, Mapping):
        raise _Rejected("binding_corrupt")
    if binding.get("ok") is not True:
        code = _safe_token(binding.get("code")) or "binding_corrupt"
        raise _Rejected(code)
    if binding.get("period") != period:
        raise _Rejected("period_mismatch")
    date_value = _valid_date(binding.get("authoritative_date"))
    if date_value is None:
        raise _Rejected("input_not_ready")
    batch_id = binding.get("batch_id")
    if batch_id is not None and batch_id != f"{period}/{date_value}":
        raise _Rejected("date_mismatch")
    prepared = binding.get("prepared_execution_id")
    if not isinstance(prepared, str) or _HEX32_RE.fullmatch(prepared) is None:
        raise _Rejected("input_not_ready")
    input_hash = _input_hash(binding.get("hashes"))
    if input_hash is None:
        raise _Rejected("input_not_ready")
    try:
        loaded = read_inputs(config.input_state_root, period, date=date_value)
    except AssertionError:
        raise
    except Exception:
        raise _Rejected("input_unreadable") from None
    if not isinstance(loaded, Mapping):
        raise _Rejected("input_unreadable")
    status = loaded.get("preparation_status")
    if status not in {"ready", "ready_degraded"}:
        raise _Rejected("input_not_ready")
    if loaded.get("period") != period:
        raise _Rejected("period_mismatch")
    if loaded.get("authoritative_date") != date_value:
        raise _Rejected("date_mismatch")
    if loaded.get("execution_id") != prepared:
        raise _Rejected("input_conflict")
    if _input_hash(loaded.get("hashes")) != input_hash:
        raise _Rejected("input_conflict")
    return {
        "period": period,
        "date": date_value,
        "prepared_execution_id": prepared,
        "automation_execution_id": str(binding.get("automation_execution_id") or execution_id),
        "input_sha256": input_hash,
        "report_run_id": f"market-digest:{period}:{date_value}",
    }


def _input_hash(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    digest = value.get("input_sha256")
    if isinstance(digest, str) and _HEX64_RE.fullmatch(digest):
        return digest
    return None


def _validate_markdown(markdown: object, period: str, date_value: str) -> dict[str, Any]:
    if not isinstance(markdown, str) or markdown == "":
        raise _Rejected("body_invalid")
    if "\x00" in markdown:
        raise _Rejected("body_invalid")
    try:
        payload = markdown.encode("utf-8")
    except UnicodeError:
        raise _Rejected("body_invalid") from None
    if not payload or len(payload) > MAX_BODY_BYTES:
        raise _Rejected("body_too_large" if payload else "body_invalid")
    skip = f"SKIP: no new emails for {date_value} {period}"
    lines = markdown.splitlines()
    if lines == [skip]:
        return {
            "markdown": markdown,
            "payload": payload,
            "section_count": 0,
            "source_line_count": 0,
        }
    title = _TITLES[period].format(date=date_value)
    if not lines or lines[0] != title:
        raise _Rejected("body_invalid")
    headings = [line for line in lines if line.startswith("## ")]
    if SOURCE_HEADING not in headings:
        raise _Rejected("body_invalid")
    if headings[-1] != SOURCE_HEADING:
        raise _Rejected("body_invalid")
    if len(headings) < 2:
        raise _Rejected("body_invalid")
    source_lines = _source_lines(markdown)
    if not source_lines:
        raise _Rejected("body_invalid")
    return {
        "markdown": markdown,
        "payload": payload,
        "section_count": len(headings),
        "source_line_count": len(source_lines),
    }


def _source_lines(markdown: str) -> list[str]:
    collecting = False
    lines: list[str] = []
    for line in markdown.splitlines():
        if line == SOURCE_HEADING:
            collecting = True
            continue
        if collecting:
            if line.startswith("## "):
                break
            text = line.strip()
            if text:
                lines.append(text)
    return lines


def _slug(label: str) -> str:
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:16]
    ascii_bits = "".join(char for char in label.lower() if char.isascii() and char.isalnum())[:24]
    if ascii_bits:
        return f"{ascii_bits}-{digest}"
    return digest


def _segments(markdown: str) -> list[tuple[str, str, str]]:
    matches = list(_HEADING_RE.finditer(markdown))
    if not matches:
        label = markdown.splitlines()[0] if markdown else "digest"
        return [("full", label[:200], markdown)]
    segments: list[tuple[str, str, str]] = []
    first = matches[0].start()
    if first:
        preamble = markdown[:first]
        title = preamble.strip().splitlines()[0] if preamble.strip() else "title"
        segments.append(("title", title[:200], preamble))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        label = match.group(0)[3:].strip()[:200]
        segments.append((_slug(label), label, markdown[match.start() : end]))
    return segments


def _windows(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + MAX_CHUNK_CHARS, length)
        if end < length:
            newline = text.rfind("\n", start, end)
            if newline > start:
                end = newline + 1
        if end <= start:
            end = min(start + 1, length)
        piece = text[start:end]
        if not piece:
            break
        parts.append(piece)
        start = end
    return parts


def _chunk_markdown(markdown: str, date_value: str, period: str) -> list[dict[str, Any]]:
    sources = _source_lines(markdown)
    chunks: list[dict[str, Any]] = []
    for section_id, section_label, text in _segments(markdown):
        attached = sources if section_label == SOURCE_LABEL else []
        for piece in _windows(text):
            chunks.append(
                {
                    "date": date_value,
                    "period": period,
                    "section_id": section_id,
                    "section_label": section_label,
                    "content": piece,
                    "sources": list(attached),
                }
            )
    if not chunks or "".join(chunk["content"] for chunk in chunks) != markdown:
        raise _Rejected("body_invalid")
    if any(len(chunk["content"]) > MAX_CHUNK_CHARS for chunk in chunks):
        raise _Rejected("body_invalid")
    return chunks


def _metadata(
    bound: Mapping[str, Any],
    validated: Mapping[str, Any],
    body_sha256: str,
    *,
    writer: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "report_run_id": bound["report_run_id"],
        "period": bound["period"],
        "date": bound["date"],
        "automation_execution_id": writer or bound["automation_execution_id"],
        "prepared_execution_id": bound["prepared_execution_id"],
        "body_sha256": body_sha256,
        "input_sha256": bound["input_sha256"],
        "byte_count": len(validated["payload"]),
        "section_count": validated["section_count"],
        "source_line_count": validated["source_line_count"],
        "archive_status": "pending",
        "embedding_status": "pending",
        "index_status": "pending",
        "embedding_model": None,
        "chunking_version": CHUNKING_VERSION,
        "chunk_count": None,
    }


def _write_metadata(directory: Path, metadata: Mapping[str, Any]) -> None:
    payload = json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _atomic_write(directory / "metadata.json", payload, mode=0o600)


def _read_metadata(path: Path) -> dict[str, Any]:
    try:
        raw = _read_owned(path, MAX_METADATA_BYTES)
        loaded = json.loads(raw.decode("utf-8"))
    except _Rejected:
        raise
    except (UnicodeError, json.JSONDecodeError):
        raise _Rejected("state_corrupt") from None
    if not isinstance(loaded, dict) or loaded.get("schema") != SCHEMA:
        raise _Rejected("state_corrupt")
    required = (
        "report_run_id",
        "period",
        "date",
        "automation_execution_id",
        "prepared_execution_id",
        "body_sha256",
        "input_sha256",
        "byte_count",
        "section_count",
        "source_line_count",
        "archive_status",
        "embedding_status",
        "index_status",
    )
    if any(key not in loaded for key in required):
        raise _Rejected("state_corrupt")
    if any(_safe_token(loaded.get(key)) is None for key in ("archive_status", "embedding_status", "index_status")):
        raise _Rejected("state_corrupt")
    if not isinstance(loaded.get("byte_count"), int) or isinstance(loaded.get("byte_count"), bool):
        raise _Rejected("state_corrupt")
    if _HEX64_RE.fullmatch(str(loaded.get("body_sha256"))) is None:
        raise _Rejected("state_corrupt")
    return loaded


def _public_record(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "report_run_id": metadata["report_run_id"],
        "period": metadata["period"],
        "date": metadata["date"],
        "automation_execution_id": metadata["automation_execution_id"],
        "prepared_execution_id": metadata["prepared_execution_id"],
        "body_sha256": metadata["body_sha256"],
        "input_sha256": metadata["input_sha256"],
        "byte_count": metadata["byte_count"],
        "section_count": metadata["section_count"],
        "source_line_count": metadata["source_line_count"],
        "archive_status": metadata["archive_status"],
        "embedding_status": metadata["embedding_status"],
        "index_status": metadata["index_status"],
        "chunk_count": metadata.get("chunk_count"),
    }


def _load_saved(config: OutputConfig, bound: Mapping[str, Any]) -> dict[str, Any]:
    directory = _batch_dir(config, bound["period"], bound["date"])
    body_path = directory / "body.md"
    meta_path = directory / "metadata.json"
    if _lstat(meta_path) is None or _lstat(body_path) is None:
        raise _Rejected("output_missing")
    metadata = _read_metadata(meta_path)
    payload = _read_owned(body_path, MAX_BODY_BYTES)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != metadata.get("body_sha256") or metadata.get("byte_count") != len(payload):
        raise _Rejected("state_corrupt")
    if metadata.get("period") != bound["period"] or metadata.get("date") != bound["date"]:
        raise _Rejected("state_corrupt")
    if metadata.get("prepared_execution_id") != bound["prepared_execution_id"]:
        raise _Rejected("input_conflict")
    if metadata.get("input_sha256") != bound["input_sha256"]:
        raise _Rejected("input_conflict")
    if metadata.get("report_run_id") != bound["report_run_id"]:
        raise _Rejected("state_corrupt")
    try:
        markdown = payload.decode("utf-8")
    except UnicodeError:
        raise _Rejected("state_corrupt") from None
    return {"directory": directory, "metadata": metadata, "markdown": markdown, "payload": payload}


def _save_locked(
    config: OutputConfig,
    bound: Mapping[str, Any],
    validated: Mapping[str, Any],
    digest: str,
) -> dict[str, Any]:
    directory = _ensure_batch_dir(config, bound["period"], bound["date"])
    body_path = directory / "body.md"
    meta_path = directory / "metadata.json"
    existing_meta = _lstat(meta_path)
    existing_body = _lstat(body_path)
    if existing_body is not None and existing_meta is None:
        # body.md is already durable. Finish metadata only when it is the same body.
        current_hash = hashlib.sha256(_read_owned(body_path, MAX_BODY_BYTES)).hexdigest()
        if current_hash != digest:
            raise _Rejected("body_conflict")
        metadata = _metadata(bound, validated, digest)
        _write_metadata(directory, metadata)
        public = _public_record(metadata)
        public.update({"ok": True, "code": "saved", "reused": False})
        return public
    if existing_meta is not None and existing_body is not None:
        current = _read_owned(body_path, MAX_BODY_BYTES)
        current_hash = hashlib.sha256(current).hexdigest()
        metadata = _read_metadata(meta_path)
        if metadata.get("prepared_execution_id") != bound["prepared_execution_id"]:
            raise _Rejected("input_conflict")
        if metadata.get("input_sha256") != bound["input_sha256"]:
            raise _Rejected("input_conflict")
        if current_hash != digest or metadata.get("body_sha256") != digest:
            raise _Rejected("body_conflict")
        public = _public_record(metadata)
        public.update({"ok": True, "code": "reused", "reused": True})
        return public
    if existing_body is not None or existing_meta is not None:
        raise _Rejected("state_corrupt")
    metadata = _metadata(bound, validated, digest)
    _atomic_write(body_path, validated["payload"], mode=0o600)
    try:
        _write_metadata(directory, metadata)
    except _Rejected:
        raise
    public = _public_record(metadata)
    public.update({"ok": True, "code": "saved", "reused": False})
    return public


def _parse_archive(text: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, list[str]]] = [("other", [])]
    for line in text.splitlines(keepends=True):
        marker = line.rstrip("\r\n")
        if marker in {"## AM", "## PM"}:
            blocks.append((marker[3:], []))
            continue
        blocks[-1][1].append(line)
    return [(kind, "".join(lines)) for kind, lines in blocks]


def _render_archive(blocks: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    for kind, body in blocks:
        if kind == "other":
            if body:
                parts.append(body)
            continue
        if parts and not parts[-1].endswith("\n"):
            parts.append("\n")
        payload = body if body.endswith("\n") or body == "" else body + "\n"
        parts.append(f"## {kind}\n{payload}")
    return "".join(parts)


def _upsert_period(blocks: list[tuple[str, str]], period: str, payload: str) -> list[tuple[str, str]]:
    updated: list[tuple[str, str]] = []
    seen = False
    for kind, body in blocks:
        if kind == period:
            if not seen:
                updated.append((period, payload))
                seen = True
            continue
        updated.append((kind, body))
    if seen:
        return updated
    if period == "AM":
        for index, (kind, _) in enumerate(updated):
            if kind == "PM":
                updated.insert(index, ("AM", payload))
                return updated
    updated.append((period, payload))
    return updated


def _archive_body(config: OutputConfig, date_value: str, period: str, markdown: str) -> str:
    """Replace only the exact period section. Return written or reused."""

    _mkdir(config.market_memory_dir)
    path = _child(config.market_memory_dir, f"{date_value}.md")
    canonical = markdown if markdown.endswith("\n") else markdown + "\n"
    with _file_lock(_archive_lock_path(config, date_value)):
        current = ""
        if _lstat(path) is not None:
            try:
                current = _read_owned(path, MAX_ARCHIVE_BYTES).decode("utf-8")
            except UnicodeError:
                raise _Rejected("archive_failed") from None
        blocks = _parse_archive(current)
        bodies = [body for kind, body in blocks if kind == period]
        if bodies == [canonical] and current == _render_archive(blocks):
            return "reused"
        rendered = _render_archive(_upsert_period(blocks, period, canonical)).encode("utf-8")
        if len(rendered) > MAX_ARCHIVE_BYTES:
            raise _Rejected("archive_too_large")
        _atomic_write(path, rendered, mode=0o644)
        return "written"


def _cache_path(config: OutputConfig, body_sha256: str) -> Path:
    _mkdir(config.body_state_root)
    model_tag = hashlib.sha256(config.embedding_model.encode("utf-8")).hexdigest()[:16]
    name = f"{body_sha256}.{model_tag}.v{CHUNKING_VERSION}.json"
    return _child(config.body_state_root, "cache", name)


def _write_cache(
    config: OutputConfig,
    body_sha256: str,
    blobs: list[bytes],
    dimension: int,
) -> None:
    payload = {
        "schema": SCHEMA,
        "body_sha256": body_sha256,
        "embedding_model": config.embedding_model,
        "chunking_version": CHUNKING_VERSION,
        "dimension": dimension,
        "count": len(blobs),
        "blobs_b64": [base64.b64encode(blob).decode("ascii") for blob in blobs],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    _atomic_write(_cache_path(config, body_sha256), raw, mode=0o600)


def _read_cache(config: OutputConfig, body_sha256: str, count: int) -> list[bytes] | None:
    path = _cache_path(config, body_sha256)
    if _lstat(path) is None:
        return None
    try:
        loaded = json.loads(_read_owned(path, MAX_CACHE_BYTES).decode("utf-8"))
    except (_Rejected, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    if loaded.get("schema") != SCHEMA or loaded.get("body_sha256") != body_sha256:
        return None
    if loaded.get("embedding_model") != config.embedding_model:
        return None
    if loaded.get("chunking_version") != CHUNKING_VERSION:
        return None
    blobs_b64 = loaded.get("blobs_b64")
    dimension = loaded.get("dimension")
    if (
        not isinstance(blobs_b64, list)
        or len(blobs_b64) != count
        or loaded.get("count") != count
        or isinstance(dimension, bool)
        or not isinstance(dimension, int)
        or not 1 <= dimension <= MAX_EMBEDDING_DIMENSION
    ):
        return None
    blobs: list[bytes] = []
    for item in blobs_b64:
        if not isinstance(item, str):
            return None
        try:
            blob = base64.b64decode(item.encode("ascii"), validate=True)
        except (UnicodeError, ValueError):
            return None
        decoded = _decode_blob(blob, dimension)
        if decoded is None:
            return None
        blobs.append(blob)
    return blobs


def _decode_blob(blob: bytes, dimension: int) -> array.array | None:
    if len(blob) != dimension * 4:
        return None
    values = array.array("f")
    try:
        values.frombytes(blob)
    except (BufferError, ValueError):
        return None
    if len(values) != dimension or any(not math.isfinite(item) for item in values):
        return None
    return values


def _encode_vectors(vectors: object, count: int) -> list[bytes]:
    if not isinstance(vectors, list) or len(vectors) != count or count < 1:
        raise _Rejected("embedding_invalid")
    blobs: list[bytes] = []
    dimension: int | None = None
    for vector in vectors:
        if isinstance(vector, (str, bytes)) or not isinstance(vector, Sequence):
            raise _Rejected("embedding_invalid")
        if len(vector) == 0 or len(vector) > MAX_EMBEDDING_DIMENSION:
            raise _Rejected("embedding_invalid")
        if dimension is None:
            dimension = len(vector)
        elif len(vector) != dimension:
            raise _Rejected("embedding_invalid")
        numbers: list[float] = []
        for item in vector:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise _Rejected("embedding_invalid")
            number = float(item)
            if not math.isfinite(number):
                raise _Rejected("embedding_invalid")
            numbers.append(number)
        try:
            encoded = array.array("f", numbers)
        except (OverflowError, TypeError, ValueError):
            raise _Rejected("embedding_invalid") from None
        if len(encoded) != dimension or any(not math.isfinite(item) for item in encoded):
            raise _Rejected("embedding_invalid")
        blobs.append(encoded.tobytes())
    return blobs


def _connect(path: Path) -> sqlite3.Connection:
    try:
        return sqlite3.connect(str(path), timeout=SQLITE_TIMEOUT_SECONDS, isolation_level=None)
    except sqlite3.Error:
        raise _Rejected("index_db_invalid") from None


def _inspect_index(
    config: OutputConfig,
    bound: Mapping[str, Any],
    body_sha256: str,
) -> dict[str, Any]:
    conn = _connect(config.index_db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()}
        if not _CHUNK_COLUMNS <= columns:
            raise _Rejected("index_db_invalid")
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'dalton_digest_commits'"
        ).fetchone()
        if present is None:
            return {"committed": False}
        row = conn.execute(
            """
            SELECT body_sha256, input_sha256, embedding_model, chunk_count, chunking_version
            FROM dalton_digest_commits
            WHERE date = ? AND period = ?
            """,
            (bound["date"], bound["period"]),
        ).fetchone()
        if row is None:
            return {"committed": False}
        stored_count = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE date = ? AND period = ?",
            (bound["date"], bound["period"]),
        ).fetchone()
        count = int(stored_count[0]) if stored_count is not None else 0
        committed = (
            row[0] == body_sha256
            and row[1] == bound["input_sha256"]
            and row[2] == config.embedding_model
            and row[3] == count
            and row[4] == CHUNKING_VERSION
            and count > 0
        )
        return {"committed": committed, "chunk_count": count}
    except _Rejected:
        raise
    except sqlite3.Error:
        raise _Rejected("index_db_invalid") from None
    finally:
        conn.close()


def _index_chunks(
    config: OutputConfig,
    bound: Mapping[str, Any],
    body_sha256: str,
    chunks: list[dict[str, Any]],
    blobs: list[bytes],
) -> None:
    if len(chunks) != len(blobs) or not chunks:
        raise _Rejected("embedding_invalid")
    dimension = len(blobs[0]) // 4
    conn = _connect(config.index_db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()}
            if not _CHUNK_COLUMNS <= columns:
                raise _Rejected("index_db_invalid")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dalton_digest_commits (
                    date TEXT NOT NULL,
                    period TEXT NOT NULL,
                    body_sha256 TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    chunking_version INTEGER NOT NULL,
                    embedding_dimension INTEGER NOT NULL,
                    PRIMARY KEY (date, period)
                )
                """
            )
            conn.execute(
                "DELETE FROM chunks WHERE date = ? AND period = ?",
                (bound["date"], bound["period"]),
            )
            conn.executemany(
                """
                INSERT INTO chunks (
                    date, period, section_id, section_label, content, source_emails, embedding
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chunk["date"],
                        chunk["period"],
                        chunk["section_id"],
                        chunk["section_label"],
                        chunk["content"],
                        json.dumps(chunk["sources"], ensure_ascii=False),
                        blob,
                    )
                    for chunk, blob in zip(chunks, blobs, strict=True)
                ],
            )
            conn.execute(
                """
                INSERT INTO dalton_digest_commits (
                    date, period, body_sha256, input_sha256, embedding_model,
                    chunk_count, chunking_version, embedding_dimension
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date, period) DO UPDATE SET
                    body_sha256 = excluded.body_sha256,
                    input_sha256 = excluded.input_sha256,
                    embedding_model = excluded.embedding_model,
                    chunk_count = excluded.chunk_count,
                    chunking_version = excluded.chunking_version,
                    embedding_dimension = excluded.embedding_dimension
                """,
                (
                    bound["date"],
                    bound["period"],
                    body_sha256,
                    bound["input_sha256"],
                    config.embedding_model,
                    len(chunks),
                    CHUNKING_VERSION,
                    dimension,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    except _Rejected:
        raise
    except sqlite3.Error:
        raise _Rejected("index_failed") from None
    finally:
        conn.close()


def _set_status(directory: Path, metadata: dict[str, Any], **updates: Any) -> dict[str, Any]:
    metadata.update(updates)
    _write_metadata(directory, metadata)
    return metadata


def _reconcile_authorities(config: OutputConfig, stored: dict[str, Any]) -> None:
    metadata = stored["metadata"]
    try:
        inspected = _inspect_index(config, metadata, str(metadata["body_sha256"]))
    except _Rejected:
        return
    if inspected.get("committed") and metadata.get("index_status") != "done":
        _set_status(
            stored["directory"],
            metadata,
            index_status="done",
            chunk_count=inspected.get("chunk_count"),
            embedding_model=config.embedding_model,
            embedding_status="ready",
        )
    elif metadata.get("index_status") == "done" and not inspected.get("committed"):
        _set_status(stored["directory"], metadata, index_status="pending")


def _finalize_locked(
    config: OutputConfig,
    bound: Mapping[str, Any],
    body_sha256: str,
    embedder: Embedder,
) -> dict[str, Any]:
    try:
        stored = _load_saved(config, bound)
    except _Rejected:
        raise
    if stored["metadata"].get("body_sha256") != body_sha256:
        raise _Rejected("hash_mismatch")
    directory = stored["directory"]
    metadata = stored["metadata"]
    markdown = stored["markdown"]
    _reconcile_authorities(config, stored)
    archive_result = "pending"
    try:
        archive_result = _archive_body(config, bound["date"], bound["period"], markdown)
    except _Rejected as exc:
        code = exc.code if exc.code in {"archive_too_large", "state_rejected"} else "archive_failed"
        _set_status(directory, metadata, archive_status="failed")
        return _finalize_result(
            metadata,
            ok=False,
            code=code,
            archive={"status": "failed", "code": code},
            embedding={"status": metadata.get("embedding_status") or "pending", "code": "not_started"},
            index={"status": metadata.get("index_status") or "pending", "code": "not_started"},
        )
    _set_status(directory, metadata, archive_status="done")
    try:
        inspected = _inspect_index(config, bound, body_sha256)
    except _Rejected as exc:
        return _finalize_result(
            metadata,
            ok=False,
            code=exc.code,
            archive={"status": archive_result, "code": "ok"},
            embedding={"status": "skipped", "code": "not_started"},
            index={"status": "failed", "code": exc.code},
        )
    if inspected.get("committed"):
        _set_status(
            directory,
            metadata,
            index_status="done",
            embedding_status="ready",
            embedding_model=config.embedding_model,
            chunk_count=inspected.get("chunk_count"),
        )
        return _finalize_result(
            metadata,
            ok=True,
            code="finalized",
            archive={"status": archive_result, "code": "ok"},
            embedding={"status": "skipped", "code": "already_indexed"},
            index={
                "status": "reused",
                "code": "ok",
                "chunk_count": inspected.get("chunk_count"),
            },
        )
    chunks = _chunk_markdown(markdown, bound["date"], bound["period"])
    cached = _read_cache(config, body_sha256, len(chunks))
    embedding_status = "cached"
    if cached is None:
        _set_status(directory, metadata, embedding_status="started", embedding_model=config.embedding_model)
        try:
            vectors = embedder([chunk["content"] for chunk in chunks], config.embedding_model)
        except AssertionError:
            raise
        except _Rejected:
            raise
        except Exception:
            _set_status(directory, metadata, embedding_status="failed")
            return _finalize_result(
                metadata,
                ok=False,
                code="embedding_failed",
                archive={"status": archive_result, "code": "ok"},
                embedding={"status": "failed", "code": "embedding_failed"},
                index={"status": "pending", "code": "not_started"},
            )
        try:
            cached = _encode_vectors(vectors, len(chunks))
        except _Rejected:
            _set_status(directory, metadata, embedding_status="failed")
            return _finalize_result(
                metadata,
                ok=False,
                code="embedding_invalid",
                archive={"status": archive_result, "code": "ok"},
                embedding={"status": "invalid", "code": "embedding_invalid"},
                index={"status": "pending", "code": "not_started"},
            )
        _write_cache(config, body_sha256, cached, len(cached[0]) // 4)
        embedding_status = "embedded"
        _set_status(
            directory,
            metadata,
            embedding_status="ready",
            embedding_model=config.embedding_model,
            chunk_count=len(chunks),
        )
    try:
        _index_chunks(config, bound, body_sha256, chunks, cached)
    except _Rejected as exc:
        _set_status(directory, metadata, index_status="failed", chunk_count=len(chunks))
        return _finalize_result(
            metadata,
            ok=False,
            code=exc.code,
            archive={"status": archive_result, "code": "ok"},
            embedding={"status": embedding_status, "code": "ok"},
            index={"status": "failed", "code": exc.code, "chunk_count": len(chunks)},
        )
    _set_status(
        directory,
        metadata,
        index_status="done",
        embedding_status="ready",
        embedding_model=config.embedding_model,
        chunk_count=len(chunks),
    )
    return _finalize_result(
        metadata,
        ok=True,
        code="finalized",
        archive={"status": archive_result, "code": "ok"},
        embedding={"status": embedding_status, "code": "ok"},
        index={"status": "committed", "code": "ok", "chunk_count": len(chunks)},
    )


def _finalize_result(
    metadata: Mapping[str, Any],
    *,
    ok: bool,
    code: str,
    archive: dict[str, Any],
    embedding: dict[str, Any],
    index: dict[str, Any],
) -> dict[str, Any]:
    public = _public_record(metadata)
    public.update(
        {
            "ok": ok,
            "code": code,
            "archive": archive,
            "embedding": embedding,
            "index": index,
        }
    )
    return public


def _fail(code: str) -> dict[str, Any]:
    safe = code if _TOKEN_RE.fullmatch(code) else "state_rejected"
    return {"ok": False, "code": safe}


__all__ = [
    "CHUNKING_VERSION",
    "Embedder",
    "OutputConfig",
    "OutputConfigError",
    "finalize_output",
    "get_output",
    "load_output_config",
    "save_output",
]
