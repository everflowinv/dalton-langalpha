#!/usr/bin/env python3
"""Prepare one AM or PM market-digest input batch without a second mail fetch.

The reference digest command labels mail inside that single process. This stage
invokes it at most once per business batch, then syncs TMTB once. A timeout,
crash, or any other uncertain digest result is never retried. Recovery reads
only the expected ``digest_YYYY-MM-DD_PERIOD.json`` under the configured output
directory. A confirmed ``output_file`` must name that expected file; any other
path is rejected and is not opened.

The child process group is reaped when the leader exits or the timeout expires.
Descendants that still hold the pipes are included. Each pipe reader owns a
duplicate descriptor and exits before the stream that owns the original is closed.

Credentials stay in the child environment built by
``build_nonarchive_subprocess_env``. This module does not read shell profiles,
``.env`` files, or ``gws`` credential stores.

``status`` prints metadata and hashes. ``read`` returns the saved batch for the
research step and does not accept a filesystem path.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Protocol
import uuid

HKT = timezone(timedelta(hours=8))

PRIORITY_SENDERS: tuple[str, ...] = (
    "Jeffrey Favuzza",
    "Peter Callahan",
    "Christian DeGrasse",
)
FUNDA_SOURCE = "funda-ai"

DIGEST_FILE_STATUSES = frozenset({"emails_fetched", "reused_existing", "already_fetched"})
DIGEST_EMPTY_STATUS = "no_new_emails"
TMTB_STATUSES = frozenset({"new_docs", "reused_existing", "no_new_docs"})
TMTB_UNAVAILABLE = "alphaengine_unavailable"

DEFAULT_TIMEOUT_SECONDS = 900.0
MAX_TIMEOUT_SECONDS = 3600.0
DEFAULT_STDOUT_LIMIT_BYTES = 1024 * 1024
MAX_STDOUT_LIMIT_BYTES = 2 * 1024 * 1024
DEFAULT_STDERR_LIMIT_BYTES = 0
MAX_STDERR_LIMIT_BYTES = 64 * 1024
MAX_JSON_BYTES = 32 * 1024 * 1024
DEFAULT_READ_MAX_BYTES = 32 * 1024 * 1024
HARD_READ_MAX_BYTES = 64 * 1024 * 1024
MAX_ATTEMPT_BYTES = 1024 * 1024
SCHEMA = 1
_RUNTIME_KEYS = (
    "_state_root",
    "_digest_doc",
    "_digest_raw",
    "_tmtb_snapshot",
    "_tmtb_source",
    "_tmtb_meta",
)
_TERMINAL_DIGEST_OUTCOMES = frozenset(
    {"emails_fetched", "reused_existing", DIGEST_EMPTY_STATUS, "recovered"}
)
_STATUS_TEXT_LIMIT = 80

_CONFIG_KEYS = frozenset(
    {
        "state_root",
        "skill_dir",
        "tmtb_script",
        "python",
        "repo_root",
        "digest_output_dir",
        "tmtb_output_dir",
        "timeout_seconds",
        "stdout_limit_bytes",
        "stderr_limit_bytes",
    }
)
_DATE_RE_LEN = 10
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_FRESH_SKEW_SECONDS = 2.0
_GROUP_SIGNAL_WAIT_SECONDS = 0.5
_PIPE_JOIN_SECONDS = 1.0
_PIPE_POLL_SECONDS = 0.05


class PrepareConfigError(ValueError):
    """Operator configuration is missing, relative, or not allowed."""


def _load_build_nonarchive_subprocess_env() -> Callable[..., dict[str, str]]:
    try:
        from scripts.dalton.bridge_subprocess_env import (
            build_nonarchive_subprocess_env as builder,
        )
    except ImportError:
        import importlib.util

        path = Path(__file__).resolve().with_name("bridge_subprocess_env.py")
        spec = importlib.util.spec_from_file_location("dalton_bridge_subprocess_env", path)
        if spec is None or spec.loader is None:
            raise
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        builder = module.build_nonarchive_subprocess_env
    return builder


build_nonarchive_subprocess_env = _load_build_nonarchive_subprocess_env()


class JsonCommandRunner(Protocol):
    def __call__(
        self,
        argv: list[str],
        *,
        env: Mapping[str, str],
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        on_spawn: Callable[[int], None] | None = None,
    ) -> ChildResult: ...


class NonarchiveEnvBuilder(Protocol):
    def __call__(
        self,
        base_env: Mapping[str, str] | None = None,
        *,
        extra_env: Mapping[str, str] | None = None,
        drop_keys: Iterable[str] = (),
        allow_keys: Iterable[str] = (),
        repo_root: Path | None = None,
    ) -> dict[str, str]: ...


@dataclass(frozen=True)
class ChildResult:
    """Bounded result of one child command. Stderr text is not retained."""

    returncode: int | None
    stdout: str
    stdout_truncated: bool
    stderr_byte_count: int
    stderr_truncated: bool
    timed_out: bool
    pid: int | None
    still_running: bool


@dataclass(frozen=True)
class PrepareConfig:
    """Absolute paths the operator supplied. No secret values."""

    state_root: Path
    skill_dir: Path
    tmtb_script: Path
    python_executable: Path
    repo_root: Path
    digest_output_dir: Path | None = None
    tmtb_output_dir: Path | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    stdout_limit_bytes: int = DEFAULT_STDOUT_LIMIT_BYTES
    stderr_limit_bytes: int = DEFAULT_STDERR_LIMIT_BYTES


def default_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def hong_kong_date(now: datetime | None = None) -> str:
    current = now if now is not None else datetime.now(HKT)
    if current.tzinfo is None:
        raise PrepareConfigError("now must be timezone-aware")
    return current.astimezone(HKT).strftime("%Y-%m-%d")


def _normalize_period(period: str) -> str:
    value = str(period or "").strip().upper()
    if value not in {"AM", "PM"}:
        raise PrepareConfigError("period must be AM or PM")
    return value


def _valid_date(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != _DATE_RE_LEN:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    if parsed.strftime("%Y-%m-%d") != value:
        return None
    return value


def _next_date(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d")
    return (parsed + timedelta(days=1)).strftime("%Y-%m-%d")


def _previous_date(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d")
    return (parsed - timedelta(days=1)).strftime("%Y-%m-%d")


def _may_cross_midnight(now: datetime, timeout_seconds: float) -> bool:
    current = now.astimezone(HKT)
    midnight = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return current + timedelta(seconds=timeout_seconds) >= midnight


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    _mkdir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    closed = False
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
        os.fsync(fd)
        os.close(fd)
        closed = True
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            dir_fd = -1
        if dir_fd >= 0:
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
            finally:
                os.close(dir_fd)
    except BaseException:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    _atomic_write_bytes(path, encoded + b"\n")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _combined_hash(parts: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, data in parts:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def _read_regular_bytes(path: Path, max_bytes: int) -> bytes:
    if path.is_symlink():
        raise _OutputRejected("symlink")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise _OutputRejected("symlink" if getattr(exc, "errno", None) in {62, 40} else "unreadable") from exc
    try:
        st = os.fstat(fd)
        if not _is_reg(st.st_mode):
            raise _OutputRejected("not_regular")
        if st.st_size > max_bytes:
            raise _OutputRejected("too_large")
        chunks: list[bytes] = []
        remaining = st.st_size
        while remaining:
            block = os.read(fd, min(1024 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _is_reg(mode: int) -> bool:
    return (mode & 0o170000) == 0o100000


class _OutputRejected(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict):
        return value
    return None


def _thread_lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def _period_lock(state_root: Path, period: str):
    """Period-wide lock so a prepare that crosses midnight cannot race another."""

    lock_dir = state_root / "locks"
    _mkdir(lock_dir)
    lock_path = lock_dir / f"prepare_{period}.lock"
    thread_lock = _thread_lock_for(lock_path.resolve())
    with thread_lock:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _signal_owned_group(pid: int, sig: int, *, leader_alive: bool) -> bool:
    """Signal the session started for ``pid``. Never signal this process group."""

    if pid <= 1 or pid == os.getpid():
        return False
    own_group = os.getpgrp()
    if leader_alive:
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            pgid = pid
        except OSError:
            return False
        # start_new_session makes the leader pid the process group id.
        if pgid != pid:
            return False
    else:
        try:
            os.getpgid(pid)
        except ProcessLookupError:
            pgid = pid
        except OSError:
            return False
        else:
            # The leader pid is alive again, so it is not our exited child.
            return False
    if pgid <= 1 or pgid == own_group or pgid == os.getpid():
        return False
    try:
        os.killpg(pgid, sig)
    except OSError:
        return False
    return True


def _reap_group(proc: subprocess.Popen[bytes]) -> None:
    """End the owned process group even after the leader has exited."""

    pid = proc.pid
    if not isinstance(pid, int) or pid <= 1:
        return
    if _signal_owned_group(pid, signal.SIGTERM, leader_alive=proc.poll() is None) and proc.poll() is None:
        try:
            proc.wait(timeout=_GROUP_SIGNAL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    if _signal_owned_group(pid, signal.SIGKILL, leader_alive=proc.poll() is None) and proc.poll() is None:
        try:
            proc.wait(timeout=_GROUP_SIGNAL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            pass


def _take_ready(
    fd: int,
    limit: int,
    chunks: list[bytes],
    kept: int,
    total: int,
    truncated: bool,
) -> tuple[int, int, bool, bool]:
    """Read bytes already queued on a nonblocking fd. The last flag is EOF."""

    while True:
        try:
            block = os.read(fd, 65536)
        except BlockingIOError:
            return kept, total, truncated, False
        except OSError:
            return kept, total, truncated, True
        if not block:
            return kept, total, truncated, True
        total += len(block)
        if kept < limit:
            room = limit - kept
            chunks.append(block[:room])
            kept += min(len(block), room)
            if len(block) > room:
                truncated = True
        else:
            truncated = True


def _read_fd(
    fd: int,
    limit: int,
    box: dict[str, tuple[bytes, int, bool]],
    stop: threading.Event,
) -> None:
    """Drain one owned nonblocking fd until EOF or ``stop``.

    The fd is closed on the way out, before this thread can observe a reused
    descriptor. It is not a descriptor owned by a ``BufferedReader``.
    """

    chunks: list[bytes] = []
    kept = 0
    total = 0
    truncated = False
    poller = select.poll()
    timeout_ms = max(1, int(_PIPE_POLL_SECONDS * 1000))
    try:
        poller.register(fd, select.POLLIN)
        while True:
            if stop.is_set():
                kept, total, truncated, _eof = _take_ready(fd, limit, chunks, kept, total, truncated)
                break
            try:
                ready = poller.poll(timeout_ms)
            except OSError:
                break
            if not ready:
                continue
            kept, total, truncated, eof = _take_ready(fd, limit, chunks, kept, total, truncated)
            if eof:
                break
    finally:
        box["value"] = (b"".join(chunks), total, truncated)
        try:
            os.close(fd)
        except OSError:
            pass


def _start_pipe_reader(
    stream: Any,
    limit: int,
    box: dict[str, tuple[bytes, int, bool]],
) -> tuple[threading.Thread, threading.Event] | None:
    """Start a reader on a duplicate of ``stream``'s fd. ``stream`` keeps its own."""

    if stream is None:
        return None
    try:
        source = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    owned = os.dup(source)
    try:
        os.set_blocking(owned, False)
        stop = threading.Event()
        thread = threading.Thread(
            target=_read_fd,
            args=(owned, limit, box, stop),
            daemon=True,
        )
        thread.start()
    except BaseException:
        os.close(owned)
        raise
    return thread, stop


def _finish_pipe(
    reader: tuple[threading.Thread, threading.Event] | None,
    stream: Any,
) -> None:
    """Stop the reader, join it, then close the stream that owns the original fd."""

    if reader is None:
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        return
    thread, stop = reader
    stop.set()
    thread.join(timeout=_PIPE_JOIN_SECONDS)
    if thread.is_alive():
        return
    if stream is not None:
        try:
            stream.close()
        except OSError:
            pass


def run_json_command(
    argv: list[str],
    *,
    env: Mapping[str, str],
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
    on_spawn: Callable[[int], None] | None = None,
) -> ChildResult:
    """Run ``argv`` with ``shell=False``, a bounded timeout, and bounded stdout.

    ``on_spawn`` runs after the process exists and before the wait, so the caller
    can persist the pid before a crash. The owned process group is reaped when
    the leader exits or the timeout expires, including descendants that still
    hold the pipes. Pipe readers are joined before their streams are closed.
    This function does not start another process.
    """

    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or not item or "\x00" in item for item in argv)
    ):
        raise PrepareConfigError("argv must be a non-empty list of strings")
    if timeout_seconds <= 0:
        raise PrepareConfigError("timeout_seconds must be positive")
    child_env = {str(key): str(value) for key, value in env.items() if value is not None}
    cwd: str | None = None
    if len(argv) >= 2 and argv[1].startswith(os.sep):
        parent = Path(argv[1]).resolve().parent
        if parent.is_dir():
            cwd = str(parent)
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env=child_env,
        cwd=cwd,
        shell=False,
        start_new_session=True,
    )
    out_box: dict[str, tuple[bytes, int, bool]] = {}
    err_box: dict[str, tuple[bytes, int, bool]] = {}
    out_reader: tuple[threading.Thread, threading.Event] | None = None
    err_reader: tuple[threading.Thread, threading.Event] | None = None
    timed_out = False
    try:
        if on_spawn is not None:
            on_spawn(proc.pid)
        out_reader = _start_pipe_reader(proc.stdout, stdout_limit_bytes, out_box)
        err_reader = _start_pipe_reader(proc.stderr, stderr_limit_bytes, err_box)
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        _reap_group(proc)
        _finish_pipe(out_reader, proc.stdout)
        _finish_pipe(err_reader, proc.stderr)
    stdout_raw, _, stdout_truncated = out_box.get("value", (b"", 0, False))
    _, stderr_total, stderr_truncated = err_box.get("value", (b"", 0, False))
    return ChildResult(
        returncode=proc.returncode,
        stdout=stdout_raw.decode("utf-8", errors="replace"),
        stdout_truncated=stdout_truncated,
        stderr_byte_count=stderr_total,
        stderr_truncated=stderr_truncated,
        timed_out=timed_out,
        pid=proc.pid,
        still_running=proc.poll() is None,
    )


def _as_absolute(path: Path | None, label: str) -> Path:
    if path is None:
        raise PrepareConfigError(f"{label} is required")
    if not isinstance(path, Path):
        path = Path(path)
    if not path.is_absolute():
        raise PrepareConfigError(f"{label} must be an absolute path")
    return path


def _as_dir(path: Path, label: str, *, must_exist: bool) -> Path:
    if path.is_symlink():
        resolved = path.resolve()
    else:
        resolved = path
    if must_exist:
        if not resolved.is_dir():
            raise PrepareConfigError(f"{label} must be an existing directory")
        return resolved.resolve()
    if resolved.exists() and not resolved.is_dir():
        raise PrepareConfigError(f"{label} must be a directory")
    return resolved.resolve() if resolved.exists() else resolved


def _as_file(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_file():
        raise PrepareConfigError(f"{label} must be an existing file")
    return resolved


def _check_bounds(config: PrepareConfig) -> PrepareConfig:
    timeout = float(config.timeout_seconds)
    if timeout < 1 or timeout > MAX_TIMEOUT_SECONDS:
        raise PrepareConfigError("timeout_seconds is outside 1..3600")
    stdout_limit = int(config.stdout_limit_bytes)
    stderr_limit = int(config.stderr_limit_bytes)
    if isinstance(config.stdout_limit_bytes, bool) or not 1024 <= stdout_limit <= MAX_STDOUT_LIMIT_BYTES:
        raise PrepareConfigError("stdout_limit_bytes is outside 1024..2097152")
    if isinstance(config.stderr_limit_bytes, bool) or not 0 <= stderr_limit <= MAX_STDERR_LIMIT_BYTES:
        raise PrepareConfigError("stderr_limit_bytes is outside 0..65536")
    return PrepareConfig(
        state_root=config.state_root,
        skill_dir=config.skill_dir,
        tmtb_script=config.tmtb_script,
        python_executable=config.python_executable,
        repo_root=config.repo_root,
        digest_output_dir=config.digest_output_dir,
        tmtb_output_dir=config.tmtb_output_dir,
        timeout_seconds=timeout,
        stdout_limit_bytes=stdout_limit,
        stderr_limit_bytes=stderr_limit,
    )


def validate_config(config: PrepareConfig) -> PrepareConfig:
    state_root = _as_dir(_as_absolute(config.state_root, "state_root"), "state_root", must_exist=False)
    skill_dir = _as_dir(_as_absolute(config.skill_dir, "skill_dir"), "skill_dir", must_exist=True)
    tmtb_script = _as_file(_as_absolute(config.tmtb_script, "tmtb_script"), "tmtb_script")
    python_executable = _as_file(
        _as_absolute(config.python_executable, "python"),
        "python",
    )
    if not os.access(python_executable, os.X_OK):
        raise PrepareConfigError("python must be executable")
    repo_root = _as_dir(
        _as_absolute(config.repo_root, "repo_root"),
        "repo_root",
        must_exist=True,
    )
    if config.digest_output_dir is None:
        digest_output = skill_dir / "output"
    else:
        digest_output = _as_dir(
            _as_absolute(config.digest_output_dir, "digest_output_dir"),
            "digest_output_dir",
            must_exist=False,
        )
    if config.tmtb_output_dir is None:
        tmtb_output = tmtb_script.parent.parent / "state" / "tmtb-alphaengine"
    else:
        tmtb_output = _as_dir(
            _as_absolute(config.tmtb_output_dir, "tmtb_output_dir"),
            "tmtb_output_dir",
            must_exist=False,
        )
    digest_script = (skill_dir / "scripts" / "market_digest_cli.py").resolve()
    try:
        digest_script.relative_to(skill_dir.resolve())
    except ValueError as exc:
        raise PrepareConfigError("market_digest_cli.py must stay inside skill_dir") from exc
    if not digest_script.is_file():
        raise PrepareConfigError("skill_dir must contain scripts/market_digest_cli.py")
    checked = _check_bounds(
        PrepareConfig(
            state_root=state_root,
            skill_dir=skill_dir,
            tmtb_script=tmtb_script,
            python_executable=python_executable,
            repo_root=repo_root,
            digest_output_dir=digest_output,
            tmtb_output_dir=tmtb_output,
            timeout_seconds=config.timeout_seconds,
            stdout_limit_bytes=config.stdout_limit_bytes,
            stderr_limit_bytes=config.stderr_limit_bytes,
        )
    )
    if checked.digest_output_dir == checked.state_root or checked.tmtb_output_dir == checked.state_root:
        raise PrepareConfigError("state_root must be distinct from output directories")
    return checked


def _reject_config_mapping(raw: Mapping[str, Any]) -> None:
    unknown = [str(key) for key in raw if str(key) not in _CONFIG_KEYS]
    if unknown:
        raise PrepareConfigError("config contains a key that is not allowed: " + ", ".join(sorted(unknown)))


def _config_path_value(raw: Mapping[str, Any], key: str) -> Path | None:
    if key not in raw or raw[key] is None:
        return None
    value = raw[key]
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value:
        raise PrepareConfigError(f"config {key} must be a path string")
    return Path(value)


def load_prepare_config(
    *,
    config_path: Path | None = None,
    state_root: Path | None = None,
    skill_dir: Path | None = None,
    tmtb_script: Path | None = None,
    python_executable: Path | None = None,
    repo_root: Path | None = None,
    digest_output_dir: Path | None = None,
    tmtb_output_dir: Path | None = None,
    timeout_seconds: float | None = None,
    stdout_limit_bytes: int | None = None,
    stderr_limit_bytes: int | None = None,
) -> PrepareConfig:
    """Merge an explicit JSON config with flags. Flags win. Secrets are rejected."""

    file_values: dict[str, Any] = {}
    if config_path is not None:
        path = _as_file(_as_absolute(config_path, "config"), "config")
        if path.stat().st_size > 64 * 1024:
            raise PrepareConfigError("config file is too large")
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PrepareConfigError("config file is not JSON") from exc
        if not isinstance(loaded, dict):
            raise PrepareConfigError("config file must be a JSON object")
        _reject_config_mapping(loaded)
        file_values = loaded
    merged_state = state_root if state_root is not None else _config_path_value(file_values, "state_root")
    merged_skill = skill_dir if skill_dir is not None else _config_path_value(file_values, "skill_dir")
    merged_tmtb = tmtb_script if tmtb_script is not None else _config_path_value(file_values, "tmtb_script")
    merged_python = (
        python_executable if python_executable is not None else _config_path_value(file_values, "python")
    )
    merged_repo = repo_root if repo_root is not None else _config_path_value(file_values, "repo_root")
    if merged_repo is None:
        merged_repo = default_repo_root()
    merged_digest = (
        digest_output_dir
        if digest_output_dir is not None
        else _config_path_value(file_values, "digest_output_dir")
    )
    merged_tmtb_out = (
        tmtb_output_dir
        if tmtb_output_dir is not None
        else _config_path_value(file_values, "tmtb_output_dir")
    )
    merged_timeout = timeout_seconds
    if merged_timeout is None and "timeout_seconds" in file_values:
        raw_timeout = file_values["timeout_seconds"]
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
            raise PrepareConfigError("config timeout_seconds must be a number")
        merged_timeout = float(raw_timeout)
    if merged_timeout is None:
        merged_timeout = DEFAULT_TIMEOUT_SECONDS
    merged_stdout = stdout_limit_bytes
    if merged_stdout is None and "stdout_limit_bytes" in file_values:
        raw_stdout = file_values["stdout_limit_bytes"]
        if isinstance(raw_stdout, bool) or not isinstance(raw_stdout, int):
            raise PrepareConfigError("config stdout_limit_bytes must be an integer")
        merged_stdout = raw_stdout
    if merged_stdout is None:
        merged_stdout = DEFAULT_STDOUT_LIMIT_BYTES
    merged_stderr = stderr_limit_bytes
    if merged_stderr is None and "stderr_limit_bytes" in file_values:
        raw_stderr = file_values["stderr_limit_bytes"]
        if isinstance(raw_stderr, bool) or not isinstance(raw_stderr, int):
            raise PrepareConfigError("config stderr_limit_bytes must be an integer")
        merged_stderr = raw_stderr
    if merged_stderr is None:
        merged_stderr = DEFAULT_STDERR_LIMIT_BYTES
    return validate_config(
        PrepareConfig(
            state_root=merged_state if merged_state is not None else Path(""),
            skill_dir=merged_skill if merged_skill is not None else Path(""),
            tmtb_script=merged_tmtb if merged_tmtb is not None else Path(""),
            python_executable=merged_python if merged_python is not None else Path(""),
            repo_root=merged_repo,
            digest_output_dir=merged_digest,
            tmtb_output_dir=merged_tmtb_out,
            timeout_seconds=merged_timeout,
            stdout_limit_bytes=merged_stdout,
            stderr_limit_bytes=merged_stderr,
        )
    )


def _attempt_path(state_root: Path, period: str, started_date: str) -> Path:
    return state_root / "attempts" / period / f"{started_date}.json"


def _index_path(state_root: Path, period: str, authoritative_date: str) -> Path:
    return state_root / "index" / period / f"{authoritative_date}.json"


def _load_state_json(path: Path, max_bytes: int) -> dict[str, Any] | None:
    if path.is_symlink():
        raise PrepareConfigError("state record must be a regular file")
    if not path.exists():
        return None
    try:
        raw = _read_regular_bytes(path, max_bytes)
    except _OutputRejected:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if isinstance(value, dict):
        return value
    return None


def _save_attempt(state_root: Path, attempt: dict[str, Any]) -> None:
    _atomic_json(_attempt_path(state_root, attempt["period"], attempt["started_hkt_date"]), attempt)


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _iter_attempts(state_root: Path, period: str) -> list[dict[str, Any]]:
    directory = state_root / "attempts" / period
    if not directory.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for path in sorted(directory.iterdir()):
        if path.name.startswith("."):
            continue
        started = _valid_date(path.stem)
        if started is None:
            continue
        loaded = _load_state_json(path, MAX_ATTEMPT_BYTES)
        if loaded is None:
            continue
        if loaded.get("period") != period or loaded.get("started_hkt_date") != started:
            continue
        found.append(loaded)
    return found


def _batch_dir(state_root: Path, period: str, authoritative_date: str) -> Path:
    return state_root / "batches" / period / authoritative_date


def _digest_filename(date_value: str, period: str) -> str:
    return f"digest_{date_value}_{period}.json"


def _tmtb_filename(date_value: str, period: str) -> str:
    return f"tmtb_{date_value}_{period}.json"


def _expected_file(directory: Path, filename: str) -> Path:
    if Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise _OutputRejected("filename")
    return directory / filename


def _load_json_file(path: Path) -> tuple[dict[str, Any] | None, bytes | None, str | None]:
    # A dangling symlink is not a missing file. exists() would follow it and
    # let a no_new_emails stdout hide an unreadable expected path.
    if path.is_symlink():
        return None, None, "symlink"
    if not path.exists():
        return None, None, "missing"
    try:
        raw = _read_regular_bytes(path, MAX_JSON_BYTES)
    except _OutputRejected as exc:
        return None, None, exc.reason
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None, None, "invalid_json"
    if not isinstance(value, dict):
        return None, None, "not_object"
    return value, raw, None


def _emails_ok(doc: Mapping[str, Any], date_value: str, period: str) -> str | None:
    if doc.get("date") != date_value:
        return "date_mismatch"
    if doc.get("period") != period:
        return "period_mismatch"
    emails = doc.get("emails")
    if not isinstance(emails, list):
        return "emails_missing"
    if any(not isinstance(item, dict) for item in emails):
        return "emails_invalid"
    return None


def _load_digest_file(
    config: PrepareConfig,
    date_value: str,
    period: str,
) -> tuple[dict[str, Any] | None, bytes | None, Path | None, str | None]:
    directory = config.digest_output_dir
    assert directory is not None
    path = _expected_file(directory, _digest_filename(date_value, period))
    doc, raw, error = _load_json_file(path)
    if error:
        return None, None, path, error
    assert doc is not None
    field_error = _emails_ok(doc, date_value, period)
    if field_error:
        return None, None, path, field_error
    return doc, raw, path, None


def _tmtb_file_ok(doc: Mapping[str, Any], date_value: str, period: str) -> str | None:
    if doc.get("date") != date_value:
        return "date_mismatch"
    if doc.get("period") != period:
        return "period_mismatch"
    if "docs" in doc and (
        not isinstance(doc.get("docs"), list) or any(not isinstance(item, dict) for item in doc["docs"])
    ):
        return "docs_invalid"
    if "pending_snippets" in doc and not isinstance(doc.get("pending_snippets"), list):
        return "pending_invalid"
    if "docs" not in doc and "pending_snippets" not in doc:
        return "docs_missing"
    return None


def _load_tmtb_file(
    config: PrepareConfig,
    date_value: str,
    period: str,
) -> tuple[dict[str, Any] | None, bytes | None, Path | None, str | None]:
    directory = config.tmtb_output_dir
    assert directory is not None
    path = _expected_file(directory, _tmtb_filename(date_value, period))
    doc, raw, error = _load_json_file(path)
    if error:
        return None, None, path, error
    assert doc is not None
    field_error = _tmtb_file_ok(doc, date_value, period)
    if field_error:
        return None, None, path, field_error
    return doc, raw, path, None


def _is_fresh(path: Path, started_unix: float) -> bool:
    try:
        modified = path.lstat().st_mtime
    except OSError:
        return False
    return modified + _FRESH_SKEW_SECONDS >= started_unix


def _short_text(value: object, limit: int = _STATUS_TEXT_LIMIT) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    return cleaned[:limit]


def _int_field(obj: Mapping[str, Any], names: tuple[str, ...]) -> int | None:
    for name in names:
        value = obj.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, list) and name.startswith("pending"):
            return len(value)
    return None


def _priority_presence(emails: list[dict[str, Any]]) -> dict[str, bool]:
    presence = {name: False for name in PRIORITY_SENDERS}
    for mail in emails:
        haystack = " ".join(str(mail.get(key) or "") for key in ("from", "sender", "from_name")).lower()
        for name in PRIORITY_SENDERS:
            if name.lower() in haystack:
                presence[name] = True
    return presence


def _funda_metadata(
    emails: list[dict[str, Any]],
    funda_ingestion: object,
) -> dict[str, Any]:
    eligible = 0
    excluded = 0
    for mail in emails:
        if mail.get("source") != FUNDA_SOURCE:
            continue
        if mail.get("research_eligible") is False:
            excluded += 1
        else:
            eligible += 1
    status = None
    pending_documents = None
    received = None
    if isinstance(funda_ingestion, dict):
        status = _short_text(funda_ingestion.get("status"))
        pending_documents = _int_field(funda_ingestion, ("pending_documents", "pending"))
        received = _int_field(funda_ingestion, ("received",))
    index_pending = status in {"partial", "pending"} or (pending_documents or 0) > 0
    return {
        "research_eligible_count": eligible,
        "research_excluded_count": excluded,
        "status": status,
        "received": received,
        "pending_documents": pending_documents,
        "index_pending": index_pending,
    }


def _image_backlog(image_ingestion: object) -> dict[str, Any]:
    status = None
    sources: dict[str, Any] = {}
    if isinstance(image_ingestion, dict):
        status = _short_text(image_ingestion.get("status"))
        raw_sources = image_ingestion.get("sources")
        if isinstance(raw_sources, dict):
            for key in ("funda-ai", "sales-notes"):
                source = raw_sources.get(key)
                if not isinstance(source, dict):
                    continue
                sources[key] = {
                    "pending": _int_field(source, ("pending", "pending_count", "pending_images")),
                    "historical_backlog": _int_field(
                        source,
                        ("backlog", "historical_backlog", "historical"),
                    ),
                    "status": _short_text(source.get("status")),
                }
    return {"status": status, "sources": sources}


def _blank_result(attempt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "ok": False,
        "usable": False,
        "preparation_status": "unresolved",
        "reason": None,
        "period": attempt["period"],
        "started_hkt_date": attempt["started_hkt_date"],
        "authoritative_date": attempt.get("authoritative_date"),
        "execution_id": attempt["execution_id"],
        "digest_outcome": (attempt.get("digest") or {}).get("outcome"),
        "emails_count": None,
        "priority_senders": None,
        "funda": None,
        "image_backlog": None,
        "tmtb": None,
        "hashes": None,
        "batch_id": None,
    }


def _set_result(attempt: dict[str, Any], **updates: Any) -> None:
    result = _blank_result(attempt)
    stored = attempt.get("result")
    if isinstance(stored, dict):
        for key in result:
            if key in stored:
                result[key] = stored[key]
    result.update(updates)
    result["schema"] = SCHEMA
    result["period"] = attempt["period"]
    result["started_hkt_date"] = attempt["started_hkt_date"]
    result["execution_id"] = attempt["execution_id"]
    attempt["result"] = result


def _public_from_attempt(
    attempt: Mapping[str, Any],
    *,
    digest_invoked: bool,
    tmtb_invoked: bool,
    reused: bool,
) -> dict[str, Any]:
    stored = attempt.get("result") if isinstance(attempt.get("result"), dict) else _blank_result(attempt)
    payload = {
        "schema": SCHEMA,
        "ok": bool(stored.get("ok")),
        "usable": bool(stored.get("usable")),
        "preparation_status": stored.get("preparation_status") or "unresolved",
        "reason": stored.get("reason"),
        "period": attempt.get("period"),
        "started_hkt_date": attempt.get("started_hkt_date"),
        "authoritative_date": stored.get("authoritative_date") or attempt.get("authoritative_date"),
        "execution_id": attempt.get("execution_id"),
        "digest_outcome": stored.get("digest_outcome") or (attempt.get("digest") or {}).get("outcome"),
        "digest_invoked": digest_invoked,
        "tmtb_invoked": tmtb_invoked,
        "reused": reused,
        "emails_count": stored.get("emails_count"),
        "priority_senders": stored.get("priority_senders"),
        "funda": stored.get("funda"),
        "image_backlog": stored.get("image_backlog"),
        "tmtb": stored.get("tmtb"),
        "hashes": stored.get("hashes"),
        "batch_id": stored.get("batch_id"),
    }
    return payload


def _mark_unresolved(attempt: dict[str, Any], reason: str, outcome: str | None = None) -> None:
    attempt["phase"] = "unresolved"
    if outcome is not None:
        attempt["digest"]["outcome"] = outcome
    authoritative = attempt.get("authoritative_date")
    _set_result(
        attempt,
        ok=False,
        usable=False,
        preparation_status="unresolved",
        reason=reason,
        authoritative_date=authoritative,
        digest_outcome=attempt["digest"].get("outcome"),
        emails_count=None,
        priority_senders=None,
        funda=None,
        image_backlog=None,
        tmtb=None,
        hashes=None,
        batch_id=None,
    )


def _confirmed_digest_stdout(child: ChildResult, period: str) -> dict[str, Any] | None:
    if child.timed_out or child.stdout_truncated or child.still_running or child.returncode != 0:
        return None
    doc = _parse_json_object(child.stdout)
    if doc is None or doc.get("ok") is not True or doc.get("period") != period:
        return None
    date_value = _valid_date(doc.get("date"))
    status = doc.get("status")
    if date_value is None or status not in DIGEST_FILE_STATUSES | {DIGEST_EMPTY_STATUS}:
        return None
    return {"date": date_value, "status": str(status), "doc": doc}


def _select_digest(
    config: PrepareConfig,
    attempt: Mapping[str, Any],
    confirmed: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, bytes | None, str, str | None]:
    """Return ``(doc, raw, kind, error)``. ``kind`` is file, empty, or error."""

    period = str(attempt["period"])
    if confirmed is not None:
        if confirmed["status"] in DIGEST_FILE_STATUSES:
            directory = config.digest_output_dir
            assert directory is not None
            expected = _expected_file(directory, _digest_filename(confirmed["date"], period))
            if not _confirmed_output_allowed(confirmed["doc"], expected):
                return None, None, "error", "path_mismatch"
        doc, raw, _path, error = _load_digest_file(config, confirmed["date"], period)
        if error == "missing":
            if confirmed["status"] == DIGEST_EMPTY_STATUS:
                return None, None, "empty", None
            return None, None, "error", "invalid"
        if error:
            return None, None, "error", "too_large" if error == "too_large" else "invalid"
        assert doc is not None
        emails = doc.get("emails")
        if confirmed["status"] == DIGEST_EMPTY_STATUS and isinstance(emails, list) and not emails:
            return doc, raw, "empty", None
        return doc, raw, "file", None
    started = str(attempt["started_hkt_date"])
    primary_doc, primary_raw, primary_path, primary_error = _load_digest_file(config, started, period)
    if primary_error not in {None, "missing"}:
        return None, None, "error", "too_large" if primary_error == "too_large" else "invalid"
    secondary: tuple[dict[str, Any], bytes] | None = None
    if attempt.get("may_cross_midnight"):
        next_day = _next_date(started)
        sec_doc, sec_raw, sec_path, sec_error = _load_digest_file(config, next_day, period)
        if sec_error not in {None, "missing"}:
            return None, None, "error", "too_large" if sec_error == "too_large" else "invalid"
        if (
            sec_error is None
            and sec_doc is not None
            and sec_raw is not None
            and sec_path is not None
            and _is_fresh(sec_path, float(attempt["started_unix"]))
        ):
            secondary = (sec_doc, sec_raw)
    primary = None
    if (
        primary_error is None
        and primary_doc is not None
        and primary_raw is not None
        and primary_path is not None
        and _is_fresh(primary_path, float(attempt["started_unix"]))
    ):
        primary = (primary_doc, primary_raw)
    if primary and secondary:
        return None, None, "error", "ambiguous"
    if primary:
        return primary[0], primary[1], "file", None
    if secondary:
        return secondary[0], secondary[1], "file", None
    if primary_path is not None and primary_error == "missing":
        return None, None, "error", "missing"
    return None, None, "error", "missing"


def _accept_mail_metadata(
    attempt: dict[str, Any],
    *,
    outcome: str,
    authoritative_date: str,
    emails: list[dict[str, Any]],
    funda_ingestion: object,
    image_ingestion: object,
) -> None:
    attempt["authoritative_date"] = authoritative_date
    attempt["digest"]["outcome"] = outcome
    attempt["phase"] = "digest_finished"
    attempt["mail"] = {
        "emails_count": len(emails),
        "priority_senders": _priority_presence(emails),
        "funda": _funda_metadata(emails, funda_ingestion),
        "image_backlog": _image_backlog(image_ingestion),
    }
    _write_date_index(attempt)


def _write_date_index(attempt: Mapping[str, Any]) -> None:
    # The caller passes state through the attempt only via a side channel set below.
    state_root = attempt.get("_state_root")
    if not isinstance(state_root, Path):
        return
    authoritative = attempt.get("authoritative_date")
    if not isinstance(authoritative, str):
        return
    payload = {
        "schema": SCHEMA,
        "period": attempt["period"],
        "started_hkt_date": attempt["started_hkt_date"],
        "authoritative_date": authoritative,
        "execution_id": attempt["execution_id"],
    }
    _atomic_json(_index_path(state_root, attempt["period"], authoritative), payload)
    _atomic_json(
        state_root / "associations" / attempt["period"] / f"{attempt['started_hkt_date']}.json",
        payload,
    )


def _attach_state(attempt: dict[str, Any], state_root: Path) -> dict[str, Any]:
    attempt["_state_root"] = state_root
    return attempt


def _persist(state_root: Path, attempt: dict[str, Any]) -> None:
    """Write the attempt without in-memory mail bodies, source bytes, or paths."""

    saved = {key: attempt.pop(key) for key in _RUNTIME_KEYS if key in attempt}
    try:
        _save_attempt(state_root, attempt)
    finally:
        attempt.update(saved)


def _child_env(config: PrepareConfig, env_builder: NonarchiveEnvBuilder) -> dict[str, str]:
    return env_builder(
        None,
        allow_keys=("GEMINI_API_KEY",),
        repo_root=config.repo_root,
    )


def _digest_argv(config: PrepareConfig, period: str) -> list[str]:
    script = (config.skill_dir / "scripts" / "market_digest_cli.py").resolve()
    return [str(config.python_executable), str(script), "--json", "digest", "--period", period]


def _tmtb_argv(config: PrepareConfig, date_value: str, period: str) -> list[str]:
    return [
        str(config.python_executable),
        str(config.tmtb_script),
        "--json",
        "sync",
        "--date",
        date_value,
        "--period",
        period,
    ]


def _note_digest_spawn(state_root: Path, attempt: dict[str, Any], pid: int) -> None:
    attempt["digest"]["pid"] = pid
    attempt["digest"]["spawned"] = True
    attempt["digest"]["still_running"] = True
    _persist(state_root, attempt)


def _note_tmtb_spawn(state_root: Path, attempt: dict[str, Any], pid: int) -> None:
    attempt["tmtb"]["pid"] = pid
    attempt["tmtb"]["spawned"] = True
    attempt["tmtb"]["still_running"] = True
    _persist(state_root, attempt)


def _clear_dead_pid(section: dict[str, Any]) -> None:
    pid = section.get("pid")
    if isinstance(pid, int) and not _pid_alive(pid):
        section["pid"] = None
        section["still_running"] = False


def _apply_child_finished(section: dict[str, Any], child: ChildResult) -> None:
    section["returncode"] = child.returncode
    section["timed_out"] = child.timed_out
    section["still_running"] = child.still_running
    if child.still_running:
        section["pid"] = child.pid
    else:
        section["pid"] = None


def _new_attempt(config: PrepareConfig, period: str, started: str, now: datetime) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "execution_id": uuid.uuid4().hex,
        "period": period,
        "started_hkt_date": started,
        "started_unix": time.time(),
        "may_cross_midnight": _may_cross_midnight(now, config.timeout_seconds),
        "authoritative_date": None,
        "phase": "digest_claimed",
        "digest": {
            "claimed": True,
            "spawned": False,
            "still_running": False,
            "pid": None,
            "outcome": None,
            "timed_out": False,
            "returncode": None,
        },
        "tmtb": {
            "claimed": False,
            "spawned": False,
            "still_running": False,
            "pid": None,
            "outcome": None,
        },
        "mail": None,
        "result": None,
    }


def _covering_attempt(state_root: Path, period: str, started: str) -> dict[str, Any] | None:
    direct = _load_state_json(_attempt_path(state_root, period, started), MAX_ATTEMPT_BYTES)
    if direct is not None:
        return direct
    index = _load_state_json(_index_path(state_root, period, started), MAX_ATTEMPT_BYTES)
    if index is not None:
        owner_date = _valid_date(index.get("started_hkt_date"))
        if owner_date is not None:
            owner = _load_state_json(_attempt_path(state_root, period, owner_date), MAX_ATTEMPT_BYTES)
            if owner is not None:
                return owner
    previous = _load_state_json(
        _attempt_path(state_root, period, _previous_date(started)),
        MAX_ATTEMPT_BYTES,
    )
    if previous is None or not previous.get("may_cross_midnight"):
        return None
    if not (previous.get("digest") or {}).get("claimed"):
        return None
    authoritative = previous.get("authoritative_date")
    if previous.get("phase") != "complete":
        return previous
    if authoritative == started:
        return previous
    return None


def _reconcile_running_digests(state_root: Path, period: str) -> dict[str, Any] | None:
    for attempt in _iter_attempts(state_root, period):
        digest = attempt.get("digest")
        if not isinstance(digest, dict) or not digest.get("still_running"):
            continue
        _attach_state(attempt, state_root)
        _clear_dead_pid(digest)
        if digest.get("still_running"):
            _mark_unresolved(attempt, "digest_child_may_still_be_running", outcome="still_running")
            _persist(state_root, attempt)
            return _public_from_attempt(attempt, digest_invoked=False, tmtb_invoked=False, reused=False)
        _persist(state_root, attempt)
    return None


def _invoke(
    config: PrepareConfig,
    runner: JsonCommandRunner,
    argv: list[str],
    child_env: Mapping[str, str],
    on_spawn: Callable[[int], None],
) -> ChildResult:
    return runner(
        argv,
        env=dict(child_env),
        timeout_seconds=config.timeout_seconds,
        stdout_limit_bytes=config.stdout_limit_bytes,
        stderr_limit_bytes=config.stderr_limit_bytes,
        on_spawn=on_spawn,
    )


def _capture_digest(
    config: PrepareConfig,
    state_root: Path,
    attempt: dict[str, Any],
    child: ChildResult,
) -> str:
    """Record digest outcome. Returns ``ready`` or ``stop``."""

    _apply_child_finished(attempt["digest"], child)
    if child.still_running:
        _mark_unresolved(attempt, "digest_child_may_still_be_running", outcome="still_running")
        _persist(state_root, attempt)
        return "stop"
    confirmed = _confirmed_digest_stdout(child, attempt["period"])
    doc, raw, kind, error = _select_digest(config, attempt, confirmed)
    if error == "ambiguous":
        _mark_unresolved(attempt, "digest_ambiguous_output", outcome="ambiguous")
        _persist(state_root, attempt)
        return "stop"
    if error == "path_mismatch":
        _mark_unresolved(attempt, "digest_output_path_mismatch", outcome="path_mismatch")
        _persist(state_root, attempt)
        return "stop"
    if error == "too_large":
        _mark_unresolved(attempt, "digest_output_too_large", outcome="too_large")
        _persist(state_root, attempt)
        return "stop"
    if kind == "file" and doc is not None:
        outcome = "recovered"
        if confirmed is not None and confirmed["date"] == doc.get("date"):
            outcome = str(confirmed["status"])
            if outcome == "already_fetched":
                outcome = "reused_existing"
        emails = list(doc["emails"])
        if outcome == DIGEST_EMPTY_STATUS and emails:
            outcome = "recovered"
        _accept_mail_metadata(
            attempt,
            outcome=outcome,
            authoritative_date=str(doc["date"]),
            emails=emails,
            funda_ingestion=doc.get("funda_ingestion"),
            image_ingestion=doc.get("image_ingestion"),
        )
        attempt["_digest_doc"] = doc
        attempt["_digest_raw"] = raw
        _write_date_index(attempt)
        _persist(state_root, attempt)
        return "ready"
    if kind == "empty" and confirmed is not None:
        source = confirmed["doc"]
        _accept_mail_metadata(
            attempt,
            outcome=DIGEST_EMPTY_STATUS,
            authoritative_date=confirmed["date"],
            emails=[],
            funda_ingestion=source.get("funda_ingestion"),
            image_ingestion=source.get("image_ingestion"),
        )
        attempt["_digest_doc"] = None
        attempt["_digest_raw"] = None
        _write_date_index(attempt)
        _persist(state_root, attempt)
        return "ready"
    parsed = _parse_json_object(child.stdout)
    if error == "invalid":
        _mark_unresolved(attempt, "digest_invalid_output", outcome="invalid_output")
    elif child.timed_out:
        _mark_unresolved(attempt, "digest_timeout_no_output", outcome="timeout")
    elif parsed is not None and parsed.get("ok") is False:
        _mark_unresolved(attempt, "digest_failed", outcome="failed")
    else:
        _mark_unresolved(attempt, "digest_no_output", outcome="no_output")
    _persist(state_root, attempt)
    return "stop"


def _confirmed_tmtb_stdout(child: ChildResult, period: str, date_value: str) -> dict[str, Any] | None:
    if child.timed_out or child.stdout_truncated or child.still_running:
        return None
    doc = _parse_json_object(child.stdout)
    if doc is None or doc.get("period") != period or doc.get("date") != date_value:
        return None
    status = doc.get("status")
    if status == TMTB_UNAVAILABLE and doc.get("ok") is False:
        return doc
    if child.returncode not in {0, None} and status != TMTB_UNAVAILABLE:
        if status not in TMTB_STATUSES:
            return None
    if status not in TMTB_STATUSES and status != TMTB_UNAVAILABLE:
        return None
    return doc


def _tmtb_metadata(
    *,
    status: str,
    docs: list[dict[str, Any]],
    pending: list[Any],
    quota_hit: bool,
    failure_count: int,
    new_count: int | None,
    degraded: bool,
    reason: str | None,
) -> dict[str, Any]:
    return {
        "status": status,
        "count": len(docs),
        "new_count": new_count,
        "pending_snippets_count": len(pending),
        "quota_hit": quota_hit,
        "failure_count": failure_count,
        "degraded": degraded,
        "reason": reason,
        "summary_only": bool(quota_hit and pending),
    }


def _names_expected_output(reported: str, expected: Path) -> bool:
    """True when ``reported`` names ``expected`` and no other file.

    The reported string is not opened. Directory identity accepts only the
    configured directory, including a path prefix that is the same directory.
    """

    if not reported or "\x00" in reported or "\n" in reported:
        return False
    candidate = Path(reported)
    if not candidate.is_absolute() or candidate.name != expected.name:
        return False
    if os.path.normpath(reported) == os.path.normpath(str(expected)):
        return True
    parent = candidate.parent
    try:
        if not parent.is_dir():
            return False
        return os.path.samefile(parent, expected.parent)
    except OSError:
        return False


def _confirmed_output_allowed(stdout: Mapping[str, Any], expected: Path) -> bool:
    """A missing output_file still means the expected path. Any other path does not."""

    if "output_file" not in stdout or stdout.get("output_file") is None:
        return True
    reported = stdout.get("output_file")
    return isinstance(reported, str) and _names_expected_output(reported, expected)


def _reported_output_matches(stdout: Mapping[str, Any] | None, expected: Path | None) -> bool:
    if stdout is None or expected is None:
        return False
    reported = stdout.get("output_file")
    return isinstance(reported, str) and bool(reported) and reported == str(expected)


def _assign_tmtb(
    attempt: dict[str, Any],
    *,
    date_value: str,
    period: str,
    status: str,
    docs: list[dict[str, Any]],
    pending: list[Any],
    source_raw: bytes | None,
    quota_hit: bool,
    failure_count: int,
    new_count: int | None,
    degraded: bool,
    reason: str | None,
    summary_only: bool,
) -> None:
    meta = _tmtb_metadata(
        status=status,
        docs=docs,
        pending=pending,
        quota_hit=quota_hit,
        failure_count=failure_count,
        new_count=new_count,
        degraded=degraded,
        reason=reason,
    )
    meta["summary_only"] = summary_only
    attempt["tmtb"]["outcome"] = status
    attempt["tmtb"]["record"] = _durable_tmtb_record(meta)
    attempt["_tmtb_snapshot"] = {
        "date": date_value,
        "period": period,
        "status": status,
        "ok": not degraded,
        "quota_hit": quota_hit,
        "docs": docs,
        "pending_snippets": pending,
        "failure_count": failure_count,
        "new_count": new_count,
        "degraded": degraded,
        "reason": reason,
        "summary_only": summary_only,
    }
    attempt["_tmtb_source"] = source_raw
    attempt["_tmtb_meta"] = meta


def _durable_tmtb_record(meta: Mapping[str, Any]) -> dict[str, Any]:
    """Sanitized TMTB facts. No docs, snippets, stdout, or failure objects."""

    reason = meta.get("reason")
    if reason is not None:
        reason = _short_text(reason)
    status = meta.get("status")
    status_text = ""
    if isinstance(status, str):
        status_text = "".join(ch for ch in status if ch.isalnum() or ch == "_")[:_STATUS_TEXT_LIMIT]
    new_count = meta.get("new_count")
    if isinstance(new_count, bool) or not isinstance(new_count, int):
        new_count = None

    def _count(name: str) -> int:
        value = meta.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return value

    return {
        "status": status_text or "unresolved_no_output",
        "count": _count("count"),
        "new_count": new_count,
        "pending_snippets_count": _count("pending_snippets_count"),
        "quota_hit": bool(meta.get("quota_hit")),
        "failure_count": _count("failure_count"),
        "degraded": bool(meta.get("degraded")),
        "reason": reason,
        "summary_only": bool(meta.get("summary_only")),
    }


def _load_tmtb_record(attempt: Mapping[str, Any]) -> dict[str, Any] | None:
    tmtb = attempt.get("tmtb")
    if not isinstance(tmtb, dict):
        return None
    raw = tmtb.get("record")
    if not isinstance(raw, dict):
        return None
    status = raw.get("status")
    if not isinstance(status, str) or not status or len(status) > _STATUS_TEXT_LIMIT:
        return None
    if not all(ch.isalnum() or ch == "_" for ch in status):
        return None
    count = raw.get("count")
    pending_count = raw.get("pending_snippets_count")
    failure_count = raw.get("failure_count")
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in (count, pending_count, failure_count)):
        return None
    new_count = raw.get("new_count")
    if new_count is not None and (isinstance(new_count, bool) or not isinstance(new_count, int)):
        return None
    reason = raw.get("reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > _STATUS_TEXT_LIMIT or "\n" in reason):
        return None
    for flag in ("quota_hit", "degraded", "summary_only"):
        if not isinstance(raw.get(flag), bool):
            return None
    return {
        "status": status,
        "count": count,
        "new_count": new_count,
        "pending_snippets_count": pending_count,
        "quota_hit": raw["quota_hit"],
        "failure_count": failure_count,
        "degraded": raw["degraded"],
        "reason": reason,
        "summary_only": raw["summary_only"],
    }


def _restore_tmtb_inputs(
    config: PrepareConfig,
    date_value: str,
    period: str,
    record: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[Any], bytes | None]:
    status = record.get("status")
    if status in {TMTB_UNAVAILABLE, "timeout", "unresolved_no_output"}:
        return [], [], None
    if int(record.get("count") or 0) == 0 and int(record.get("pending_snippets_count") or 0) == 0:
        return [], [], None
    loaded_doc, loaded_raw, _path, error = _load_tmtb_file(config, date_value, period)
    if error is not None or loaded_doc is None:
        return [], [], None
    raw_docs = loaded_doc.get("docs")
    raw_pending = loaded_doc.get("pending_snippets")
    docs = [item for item in raw_docs if isinstance(item, dict)] if isinstance(raw_docs, list) else []
    pending = list(raw_pending) if isinstance(raw_pending, list) else []
    return docs, pending, loaded_raw


def _capture_tmtb(
    config: PrepareConfig,
    state_root: Path,
    attempt: dict[str, Any],
    child: ChildResult | None,
) -> str:
    """Record one TMTB result. Returns ``ready`` when that result is terminal.

    A validated expected file is adopted for ``new_docs`` and ``reused_existing``
    even when it is older than this claim. ``no_new_docs`` adopts a file only
    when it is fresh or stdout names that exact path. ``alphaengine_unavailable``
    never copies an older file. Reported paths are compared, never opened.
    Sanitized outcome facts are stored on the attempt before snapshots so a
    crash in that window can reuse them without another TMTB call.
    """

    date_value = str(attempt["authoritative_date"])
    period = str(attempt["period"])
    if child is not None:
        _apply_child_finished(attempt["tmtb"], child)
        if child.still_running:
            attempt["phase"] = "tmtb_claimed"
            _set_result(
                attempt,
                ok=False,
                usable=False,
                preparation_status="unresolved",
                reason="tmtb_child_may_still_be_running",
                authoritative_date=date_value,
                digest_outcome=attempt["digest"].get("outcome"),
                emails_count=(attempt.get("mail") or {}).get("emails_count"),
                priority_senders=(attempt.get("mail") or {}).get("priority_senders"),
                funda=(attempt.get("mail") or {}).get("funda"),
                image_backlog=(attempt.get("mail") or {}).get("image_backlog"),
            )
            _persist(state_root, attempt)
            return "stop"
    if child is None:
        saved = _load_tmtb_record(attempt)
        if saved is not None:
            docs, pending, source_raw = _restore_tmtb_inputs(config, date_value, period, saved)
            new_count = saved.get("new_count")
            _assign_tmtb(
                attempt,
                date_value=date_value,
                period=period,
                status=str(saved["status"]),
                docs=docs,
                pending=pending,
                source_raw=source_raw,
                quota_hit=bool(saved["quota_hit"]),
                failure_count=int(saved["failure_count"]),
                new_count=new_count if isinstance(new_count, int) and not isinstance(new_count, bool) else None,
                degraded=bool(saved["degraded"]),
                reason=saved["reason"] if isinstance(saved["reason"], str) else None,
                summary_only=bool(saved["summary_only"]),
            )
            _persist(state_root, attempt)
            return "ready"
    stdout = _confirmed_tmtb_stdout(child, period, date_value) if child is not None else None
    loaded_doc, loaded_raw, loaded_path, loaded_error = _load_tmtb_file(config, date_value, period)
    claimed_unix = float(attempt.get("tmtb_claimed_unix") or attempt["started_unix"])
    file_ok = loaded_error is None and loaded_doc is not None and loaded_path is not None
    fresh = bool(file_ok and loaded_path is not None and _is_fresh(loaded_path, claimed_unix))
    status_name = stdout.get("status") if stdout is not None and isinstance(stdout.get("status"), str) else None
    matched = _reported_output_matches(stdout, loaded_path)
    prior = attempt["tmtb"].get("outcome")
    prior_name = prior if isinstance(prior, str) else None
    use_file = False
    if file_ok and status_name != TMTB_UNAVAILABLE:
        if status_name in {"new_docs", "reused_existing"}:
            use_file = True
        elif status_name == "no_new_docs" and (fresh or matched):
            use_file = True
        elif status_name is None and fresh:
            use_file = True
        elif (
            child is None
            and prior_name in {"new_docs", "reused_existing", "recovered", "recovered_after_timeout"}
        ):
            use_file = True
    if status_name == TMTB_UNAVAILABLE:
        use_file = False
    docs: list[dict[str, Any]] = []
    pending: list[Any] = []
    source_raw: bytes | None = None
    if use_file and loaded_doc is not None:
        raw_docs = loaded_doc.get("docs")
        raw_pending = loaded_doc.get("pending_snippets")
        docs = list(raw_docs) if isinstance(raw_docs, list) else []
        pending = list(raw_pending) if isinstance(raw_pending, list) else []
        source_raw = loaded_raw
    quota_hit = False
    failure_count = 0
    new_count: int | None = None
    if stdout is not None:
        quota_hit = bool(stdout.get("quota_hit"))
        failures = stdout.get("failures")
        if isinstance(failures, list):
            failure_count = len(failures)
        raw_new = stdout.get("new_count")
        if isinstance(raw_new, int) and not isinstance(raw_new, bool):
            new_count = raw_new
    elif use_file and loaded_doc is not None and bool(loaded_doc.get("quota_hit")):
        quota_hit = True
    degraded = False
    reason: str | None = None
    if status_name == TMTB_UNAVAILABLE:
        status = TMTB_UNAVAILABLE
        degraded = True
        reason = TMTB_UNAVAILABLE
        docs = []
        pending = []
        source_raw = None
        quota_hit = False
    elif child is not None and child.timed_out:
        status = "recovered_after_timeout" if use_file else "timeout"
        degraded = True
        reason = "timeout"
        if not use_file:
            docs = []
            pending = []
            source_raw = None
    elif status_name in {"new_docs", "reused_existing"} and not use_file:
        status = status_name
        degraded = True
        reason = "output_missing"
        docs = []
        pending = []
        source_raw = None
    elif status_name is not None:
        status = status_name
        if not use_file:
            docs = []
            pending = []
            source_raw = None
        if quota_hit or failure_count or (stdout is not None and stdout.get("ok") is False):
            degraded = True
            if quota_hit:
                reason = "quota"
            elif failure_count:
                reason = "partial"
            else:
                reason = "failed"
    elif use_file:
        status = prior_name if prior_name in {"recovered", "recovered_after_timeout", "new_docs", "reused_existing"} else "recovered"
        if status == "recovered_after_timeout":
            degraded = True
            reason = "timeout"
        elif quota_hit:
            degraded = True
            reason = "quota"
    else:
        kept = prior_name if prior_name in {"timeout", "no_new_docs", TMTB_UNAVAILABLE, "unresolved_no_output"} else None
        status = kept or "unresolved_no_output"
        degraded = status != "no_new_docs"
        reason = None if status == "no_new_docs" else status
        docs = []
        pending = []
        source_raw = None
    summary_only = bool(pending) and (quota_hit or stdout is None)
    _assign_tmtb(
        attempt,
        date_value=date_value,
        period=period,
        status=status,
        docs=docs,
        pending=pending,
        source_raw=source_raw,
        quota_hit=quota_hit,
        failure_count=failure_count,
        new_count=new_count,
        degraded=degraded,
        reason=reason,
        summary_only=summary_only,
    )
    _persist(state_root, attempt)
    return "ready"


def _write_snapshots(state_root: Path, attempt: dict[str, Any]) -> dict[str, Any]:
    authoritative = str(attempt["authoritative_date"])
    period = str(attempt["period"])
    directory = _batch_dir(state_root, period, authoritative)
    emails_doc = attempt.get("_digest_doc")
    emails: list[Any]
    if isinstance(emails_doc, dict) and isinstance(emails_doc.get("emails"), list):
        emails = list(emails_doc["emails"])
    elif attempt["digest"].get("outcome") == DIGEST_EMPTY_STATUS:
        emails = []
    else:
        emails = []
    emails_bytes = (json.dumps(emails, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    tmtb_snapshot = attempt.get("_tmtb_snapshot")
    if not isinstance(tmtb_snapshot, dict):
        tmtb_snapshot = {
            "date": authoritative,
            "period": period,
            "status": attempt["tmtb"].get("outcome"),
            "docs": [],
            "pending_snippets": [],
        }
    tmtb_bytes = (json.dumps(tmtb_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write_bytes(directory / "emails.json", emails_bytes)
    _atomic_write_bytes(directory / "tmtb.json", tmtb_bytes)
    digest_raw = attempt.get("_digest_raw")
    digest_hash = None
    if isinstance(digest_raw, bytes):
        _atomic_write_bytes(directory / "digest_source.json", digest_raw)
        digest_hash = _sha256_bytes(digest_raw)
    tmtb_source = attempt.get("_tmtb_source")
    tmtb_source_hash = None
    if isinstance(tmtb_source, bytes):
        _atomic_write_bytes(directory / "tmtb_source.json", tmtb_source)
        tmtb_source_hash = _sha256_bytes(tmtb_source)
    parts = [("emails", emails_bytes), ("tmtb", tmtb_bytes)]
    if isinstance(digest_raw, bytes):
        parts.append(("digest_source", digest_raw))
    if isinstance(tmtb_source, bytes):
        parts.append(("tmtb_source", tmtb_source))
    hashes = {
        "input_sha256": _combined_hash(parts),
        "emails_sha256": _sha256_bytes(emails_bytes),
        "tmtb_sha256": _sha256_bytes(tmtb_bytes),
        "digest_source_sha256": digest_hash,
        "tmtb_source_sha256": tmtb_source_hash,
    }
    mail = attempt.get("mail") or {}
    meta = attempt.get("_tmtb_meta") or {}
    degraded = bool(meta.get("degraded"))
    preparation_status = "ready_degraded" if degraded else "ready"
    manifest = {
        "schema": SCHEMA,
        "execution_id": attempt["execution_id"],
        "period": period,
        "started_hkt_date": attempt["started_hkt_date"],
        "authoritative_date": authoritative,
        "preparation_status": preparation_status,
        "digest_outcome": attempt["digest"].get("outcome"),
        "emails_count": mail.get("emails_count"),
        "priority_senders": mail.get("priority_senders"),
        "funda": mail.get("funda"),
        "image_backlog": mail.get("image_backlog"),
        "tmtb": meta,
        "hashes": hashes,
    }
    _atomic_json(directory / "manifest.json", manifest)
    _set_result(
        attempt,
        ok=True,
        usable=True,
        preparation_status=preparation_status,
        reason=meta.get("reason"),
        authoritative_date=authoritative,
        digest_outcome=attempt["digest"].get("outcome"),
        emails_count=mail.get("emails_count"),
        priority_senders=mail.get("priority_senders"),
        funda=mail.get("funda"),
        image_backlog=mail.get("image_backlog"),
        tmtb=meta,
        hashes=hashes,
        batch_id=f"{period}/{authoritative}",
    )
    attempt["phase"] = "complete"
    return hashes


def _hashes_match(state_root: Path, attempt: Mapping[str, Any]) -> bool:
    result = attempt.get("result")
    if not isinstance(result, dict):
        return False
    authoritative = result.get("authoritative_date") or attempt.get("authoritative_date")
    if not isinstance(authoritative, str) or _valid_date(authoritative) is None:
        return False
    recorded = result.get("hashes")
    if not isinstance(recorded, dict) or not isinstance(recorded.get("input_sha256"), str):
        return False
    directory = _batch_dir(state_root, str(attempt["period"]), authoritative)
    emails_path = directory / "emails.json"
    tmtb_path = directory / "tmtb.json"
    if not emails_path.is_file() or emails_path.is_symlink() or not tmtb_path.is_file() or tmtb_path.is_symlink():
        return False
    try:
        emails_bytes = _read_regular_bytes(emails_path, MAX_JSON_BYTES)
        tmtb_bytes = _read_regular_bytes(tmtb_path, MAX_JSON_BYTES)
    except _OutputRejected:
        return False
    parts = [("emails", emails_bytes), ("tmtb", tmtb_bytes)]
    digest_path = directory / "digest_source.json"
    if recorded.get("digest_source_sha256"):
        if not digest_path.is_file() or digest_path.is_symlink():
            return False
        try:
            digest_bytes = _read_regular_bytes(digest_path, MAX_JSON_BYTES)
        except _OutputRejected:
            return False
        if _sha256_bytes(digest_bytes) != recorded.get("digest_source_sha256"):
            return False
        parts.append(("digest_source", digest_bytes))
    tmtb_source_path = directory / "tmtb_source.json"
    if recorded.get("tmtb_source_sha256"):
        if not tmtb_source_path.is_file() or tmtb_source_path.is_symlink():
            return False
        try:
            source_bytes = _read_regular_bytes(tmtb_source_path, MAX_JSON_BYTES)
        except _OutputRejected:
            return False
        if _sha256_bytes(source_bytes) != recorded.get("tmtb_source_sha256"):
            return False
        parts.append(("tmtb_source", source_bytes))
    if _sha256_bytes(emails_bytes) != recorded.get("emails_sha256"):
        return False
    if _sha256_bytes(tmtb_bytes) != recorded.get("tmtb_sha256"):
        return False
    return _combined_hash(parts) == recorded.get("input_sha256")


def _finish_tmtb(
    config: PrepareConfig,
    state_root: Path,
    attempt: dict[str, Any],
    runner: JsonCommandRunner,
    env_builder: NonarchiveEnvBuilder,
    *,
    allow_call: bool,
) -> dict[str, Any]:
    if attempt["tmtb"].get("still_running"):
        _clear_dead_pid(attempt["tmtb"])
        if attempt["tmtb"].get("still_running"):
            _set_result(
                attempt,
                ok=False,
                usable=False,
                preparation_status="unresolved",
                reason="tmtb_child_may_still_be_running",
                authoritative_date=attempt.get("authoritative_date"),
                digest_outcome=attempt["digest"].get("outcome"),
                emails_count=(attempt.get("mail") or {}).get("emails_count"),
                priority_senders=(attempt.get("mail") or {}).get("priority_senders"),
                funda=(attempt.get("mail") or {}).get("funda"),
                image_backlog=(attempt.get("mail") or {}).get("image_backlog"),
            )
            _persist(state_root, attempt)
            return _public_from_attempt(attempt, digest_invoked=False, tmtb_invoked=False, reused=False)
    invoked = False
    child: ChildResult | None = None
    if not attempt["tmtb"].get("claimed") and allow_call:
        child_env = _child_env(config, env_builder)
        attempt["tmtb"]["claimed"] = True
        attempt["tmtb_claimed_unix"] = time.time()
        attempt["phase"] = "tmtb_claimed"
        _persist(state_root, attempt)
        try:
            child = _invoke(
                config,
                runner,
                _tmtb_argv(config, str(attempt["authoritative_date"]), str(attempt["period"])),
                child_env,
                lambda pid: _note_tmtb_spawn(state_root, attempt, pid),
            )
        except Exception:
            attempt["tmtb"]["outcome"] = "runner_exception"
            _clear_dead_pid(attempt["tmtb"])
            if attempt["tmtb"].get("still_running"):
                _set_result(
                    attempt,
                    ok=False,
                    usable=False,
                    preparation_status="unresolved",
                    reason="tmtb_child_may_still_be_running",
                    authoritative_date=attempt.get("authoritative_date"),
                    digest_outcome=attempt["digest"].get("outcome"),
                    emails_count=(attempt.get("mail") or {}).get("emails_count"),
                    priority_senders=(attempt.get("mail") or {}).get("priority_senders"),
                    funda=(attempt.get("mail") or {}).get("funda"),
                    image_backlog=(attempt.get("mail") or {}).get("image_backlog"),
                )
                _persist(state_root, attempt)
                return _public_from_attempt(
                    attempt,
                    digest_invoked=False,
                    tmtb_invoked=True,
                    reused=False,
                )
            child = None
            attempt["tmtb"]["claimed"] = True
        invoked = True
    if _capture_tmtb(config, state_root, attempt, child) != "ready":
        return _public_from_attempt(attempt, digest_invoked=False, tmtb_invoked=invoked, reused=False)
    _write_snapshots(state_root, attempt)
    _persist(state_root, attempt)
    _drop_runtime(attempt)
    return _public_from_attempt(attempt, digest_invoked=False, tmtb_invoked=invoked, reused=False)


def _drop_runtime(attempt: dict[str, Any]) -> None:
    for key in _RUNTIME_KEYS:
        attempt.pop(key, None)


def _mail_terminal(attempt: Mapping[str, Any]) -> bool:
    if attempt.get("digest", {}).get("outcome") not in _TERMINAL_DIGEST_OUTCOMES:
        return False
    if not isinstance(attempt.get("mail"), dict):
        return False
    return _valid_date(attempt.get("authoritative_date")) is not None


def _digest_failure_reason(error: str | None, outcome: object) -> str:
    if error == "too_large":
        return "digest_output_too_large"
    if error == "ambiguous":
        return "digest_ambiguous_output"
    if error == "invalid":
        return "digest_invalid_output"
    if outcome == "timeout":
        return "digest_timeout_no_output"
    return "digest_no_output"


def _recover_digest_file(config: PrepareConfig, state_root: Path, attempt: dict[str, Any]) -> bool:
    """Adopt one fresh expected digest file. Never invokes the digest command."""

    doc, raw, kind, error = _select_digest(config, attempt, None)
    if kind == "file" and doc is not None:
        emails = list(doc["emails"])
        _accept_mail_metadata(
            attempt,
            outcome="recovered",
            authoritative_date=str(doc["date"]),
            emails=emails,
            funda_ingestion=doc.get("funda_ingestion"),
            image_ingestion=doc.get("image_ingestion"),
        )
        attempt["_digest_doc"] = doc
        attempt["_digest_raw"] = raw
        _persist(state_root, attempt)
        return True
    reason = _digest_failure_reason(error, attempt["digest"].get("outcome"))
    outcome = {
        "digest_output_too_large": "too_large",
        "digest_ambiguous_output": "ambiguous",
        "digest_invalid_output": "invalid_output",
        "digest_timeout_no_output": "timeout",
    }.get(reason, "no_output")
    _mark_unresolved(attempt, reason, outcome=outcome)
    _persist(state_root, attempt)
    return False


def _load_terminal_emails(config: PrepareConfig, state_root: Path, attempt: dict[str, Any]) -> bool:
    if attempt["digest"].get("outcome") == DIGEST_EMPTY_STATUS:
        attempt["_digest_doc"] = None
        attempt["_digest_raw"] = None
        return True
    if isinstance(attempt.get("_digest_doc"), dict):
        return True
    saved = _load_saved_emails(state_root, attempt)
    if saved is not None:
        attempt["_digest_doc"] = {
            "emails": saved,
            "date": attempt.get("authoritative_date"),
            "period": attempt["period"],
        }
        attempt["_digest_raw"] = None
        return True
    doc, raw, _path, error = _load_digest_file(
        config,
        str(attempt["authoritative_date"]),
        str(attempt["period"]),
    )
    if error is None and doc is not None:
        attempt["_digest_doc"] = doc
        attempt["_digest_raw"] = raw
        return True
    _mark_unresolved(attempt, "digest_no_output", outcome="no_output")
    _persist(state_root, attempt)
    return False


def _load_saved_emails(state_root: Path, attempt: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    authoritative = attempt.get("authoritative_date")
    if not isinstance(authoritative, str):
        return None
    path = _batch_dir(state_root, str(attempt["period"]), authoritative) / "emails.json"
    if not path.is_file() or path.is_symlink():
        return None
    try:
        raw = _read_regular_bytes(path, MAX_JSON_BYTES)
        value = json.loads(raw.decode("utf-8"))
    except (_OutputRejected, UnicodeError, json.JSONDecodeError):
        return None
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    return None


def _resume(
    config: PrepareConfig,
    state_root: Path,
    attempt: dict[str, Any],
    runner: JsonCommandRunner,
    env_builder: NonarchiveEnvBuilder,
) -> dict[str, Any]:
    """Continue one claimed batch. This never starts another digest command."""

    _attach_state(attempt, state_root)
    if attempt.get("phase") == "complete":
        if _hashes_match(state_root, attempt):
            return _public_from_attempt(attempt, digest_invoked=False, tmtb_invoked=False, reused=True)
        _set_result(
            attempt,
            ok=False,
            usable=False,
            preparation_status="snapshot_hash_mismatch",
            reason="snapshot_hash_mismatch",
            hashes=None,
        )
        return _public_from_attempt(attempt, digest_invoked=False, tmtb_invoked=False, reused=False)
    digest = attempt.get("digest")
    if not isinstance(digest, dict):
        _mark_unresolved(attempt, "digest_no_output", outcome="no_output")
        _persist(state_root, attempt)
        return _public_from_attempt(attempt, digest_invoked=False, tmtb_invoked=False, reused=False)
    if digest.get("still_running"):
        _clear_dead_pid(digest)
        if digest.get("still_running"):
            _mark_unresolved(attempt, "digest_child_may_still_be_running", outcome="still_running")
            _persist(state_root, attempt)
            return _public_from_attempt(attempt, digest_invoked=False, tmtb_invoked=False, reused=False)
    if not _mail_terminal(attempt):
        if not _recover_digest_file(config, state_root, attempt):
            return _public_from_attempt(attempt, digest_invoked=False, tmtb_invoked=False, reused=False)
    elif not _load_terminal_emails(config, state_root, attempt):
        return _public_from_attempt(attempt, digest_invoked=False, tmtb_invoked=False, reused=False)
    tmtb = attempt.get("tmtb")
    claimed = isinstance(tmtb, dict) and bool(tmtb.get("claimed"))
    return _finish_tmtb(
        config,
        state_root,
        attempt,
        runner,
        env_builder,
        allow_call=not claimed,
    )


def _begin(
    config: PrepareConfig,
    state_root: Path,
    period: str,
    now: datetime,
    runner: JsonCommandRunner,
    env_builder: NonarchiveEnvBuilder,
) -> dict[str, Any]:
    child_env = _child_env(config, env_builder)
    attempt = _new_attempt(config, period, hong_kong_date(now), now)
    _attach_state(attempt, state_root)
    _persist(state_root, attempt)
    try:
        child = _invoke(
            config,
            runner,
            _digest_argv(config, period),
            child_env,
            lambda pid: _note_digest_spawn(state_root, attempt, pid),
        )
    except Exception:
        _clear_dead_pid(attempt["digest"])
        if attempt["digest"].get("still_running"):
            _mark_unresolved(attempt, "digest_child_may_still_be_running", outcome="still_running")
        else:
            _mark_unresolved(attempt, "digest_runner_exception", outcome="runner_exception")
        _persist(state_root, attempt)
        return _public_from_attempt(attempt, digest_invoked=True, tmtb_invoked=False, reused=False)
    if _capture_digest(config, state_root, attempt, child) != "ready":
        return _public_from_attempt(attempt, digest_invoked=True, tmtb_invoked=False, reused=False)
    finished = _finish_tmtb(
        config,
        state_root,
        attempt,
        runner,
        env_builder,
        allow_call=True,
    )
    finished["digest_invoked"] = True
    return finished


def prepare_inputs(
    config: PrepareConfig,
    period: str,
    *,
    now: datetime | None = None,
    runner: JsonCommandRunner | None = None,
    env_builder: NonarchiveEnvBuilder | None = None,
) -> dict[str, Any]:
    """Prepare one batch. Concurrent callers serialize on the period lock."""

    checked = validate_config(config)
    selected = _normalize_period(period)
    current = now if now is not None else datetime.now(HKT)
    if current.tzinfo is None:
        raise PrepareConfigError("now must be timezone-aware")
    command_runner = runner or run_json_command
    builder = env_builder or build_nonarchive_subprocess_env
    _mkdir(checked.state_root)
    with _period_lock(checked.state_root, selected):
        blocked = _reconcile_running_digests(checked.state_root, selected)
        if blocked is not None:
            return blocked
        existing = _covering_attempt(checked.state_root, selected, hong_kong_date(current))
        if existing is None:
            return _begin(checked, checked.state_root, selected, current, command_runner, builder)
        return _resume(checked, checked.state_root, existing, command_runner, builder)


def _lookup_attempt(state_root: Path, period: str, date_value: str) -> dict[str, Any] | None:
    direct = _load_state_json(_attempt_path(state_root, period, date_value), MAX_ATTEMPT_BYTES)
    if direct is not None:
        return direct
    index = _load_state_json(_index_path(state_root, period, date_value), MAX_ATTEMPT_BYTES)
    if index is not None:
        owner_date = _valid_date(index.get("started_hkt_date"))
        if owner_date is not None:
            owner = _load_state_json(_attempt_path(state_root, period, owner_date), MAX_ATTEMPT_BYTES)
            if owner is not None:
                return owner
    previous = _load_state_json(
        _attempt_path(state_root, period, _previous_date(date_value)),
        MAX_ATTEMPT_BYTES,
    )
    if previous is not None and previous.get("authoritative_date") == date_value:
        return previous
    return None


def status_inputs(
    state_root: Path,
    period: str,
    *,
    date: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read metadata for one batch. Does not fetch mail or open unrelated paths."""

    root = _as_dir(_as_absolute(state_root, "state_root"), "state_root", must_exist=True)
    selected = _normalize_period(period)
    if date is None:
        date_value = hong_kong_date(now)
    else:
        date_value = _valid_date(date)
        if date_value is None:
            raise PrepareConfigError("date must be YYYY-MM-DD")
    attempt = _lookup_attempt(root, selected, date_value)
    if attempt is None:
        return {
            "schema": SCHEMA,
            "ok": False,
            "usable": False,
            "preparation_status": "absent",
            "reason": "no_record",
            "period": selected,
            "started_hkt_date": None,
            "authoritative_date": None,
            "execution_id": None,
            "digest_outcome": None,
            "digest_invoked": False,
            "tmtb_invoked": False,
            "reused": False,
            "emails_count": None,
            "priority_senders": None,
            "funda": None,
            "image_backlog": None,
            "tmtb": None,
            "hashes": None,
            "batch_id": None,
        }
    if attempt.get("phase") == "complete" and not _hashes_match(root, attempt):
        public = _public_from_attempt(attempt, digest_invoked=False, tmtb_invoked=False, reused=False)
        public["ok"] = False
        public["usable"] = False
        public["preparation_status"] = "snapshot_hash_mismatch"
        public["reason"] = "snapshot_hash_mismatch"
        public["hashes"] = None
        return public
    return _public_from_attempt(attempt, digest_invoked=False, tmtb_invoked=False, reused=False)


def read_inputs(
    state_root: Path,
    period: str,
    *,
    date: str | None = None,
    now: datetime | None = None,
    max_bytes: int = DEFAULT_READ_MAX_BYTES,
) -> dict[str, Any]:
    """Return one saved batch. ``max_bytes`` bounds the JSON payload, not a path."""

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= HARD_READ_MAX_BYTES:
        raise PrepareConfigError("max_bytes is outside 1..67108864")
    public = status_inputs(state_root, period, date=date, now=now)
    if public["preparation_status"] != "ready" and public["preparation_status"] != "ready_degraded":
        public["emails"] = None
        public["tmtb_docs"] = None
        public["pending_snippets"] = None
        public["truncated"] = False
        return public
    root = _as_dir(_as_absolute(state_root, "state_root"), "state_root", must_exist=True)
    authoritative = public.get("authoritative_date")
    if not isinstance(authoritative, str):
        public["ok"] = False
        public["reason"] = "not_ready"
        return public
    directory = _batch_dir(root, str(public["period"]), authoritative)
    try:
        emails_raw = _read_regular_bytes(directory / "emails.json", MAX_JSON_BYTES)
        tmtb_raw = _read_regular_bytes(directory / "tmtb.json", MAX_JSON_BYTES)
    except _OutputRejected:
        public["ok"] = False
        public["usable"] = False
        public["preparation_status"] = "snapshot_hash_mismatch"
        public["reason"] = "snapshot_hash_mismatch"
        public["emails"] = None
        public["tmtb_docs"] = None
        public["pending_snippets"] = None
        return public
    try:
        emails = json.loads(emails_raw.decode("utf-8"))
        tmtb_doc = json.loads(tmtb_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        public["ok"] = False
        public["usable"] = False
        public["preparation_status"] = "unresolved"
        public["reason"] = "snapshot_unreadable"
        public["emails"] = None
        public["tmtb_docs"] = None
        public["pending_snippets"] = None
        public["truncated"] = False
        return public
    if not isinstance(emails, list) or not isinstance(tmtb_doc, dict):
        public["ok"] = False
        public["usable"] = False
        public["preparation_status"] = "unresolved"
        public["reason"] = "snapshot_unreadable"
        public["emails"] = None
        public["tmtb_docs"] = None
        public["pending_snippets"] = None
        public["truncated"] = False
        return public
    docs = tmtb_doc.get("docs")
    pending = tmtb_doc.get("pending_snippets")
    payload = dict(public)
    payload["emails"] = emails
    payload["tmtb_docs"] = docs if isinstance(docs, list) else []
    payload["pending_snippets"] = pending if isinstance(pending, list) else []
    payload["truncated"] = False
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded) > max_bytes:
        withheld = dict(public)
        withheld["ok"] = False
        withheld["usable"] = False
        withheld["preparation_status"] = "content_exceeds_bound"
        withheld["reason"] = "content_exceeds_bound"
        withheld["emails"] = None
        withheld["tmtb_docs"] = None
        withheld["pending_snippets"] = None
        withheld["truncated"] = True
        withheld["bytes"] = len(encoded)
        return withheld
    payload["bytes"] = len(encoded)
    return payload


def _emit(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    sys.stdout.write("\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market_digest_inputs")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="Fetch one AM or PM batch, or return the saved batch")
    prepare.add_argument("--period", required=True, choices=["AM", "PM"])
    prepare.add_argument("--config", type=Path)
    prepare.add_argument("--state-root", type=Path)
    prepare.add_argument("--skill-dir", type=Path)
    prepare.add_argument("--tmtb-script", type=Path)
    prepare.add_argument("--python", type=Path)
    prepare.add_argument("--repo-root", type=Path)
    prepare.add_argument("--digest-output-dir", type=Path)
    prepare.add_argument("--tmtb-output-dir", type=Path)
    prepare.add_argument("--timeout-seconds", type=float)

    status = sub.add_parser("status", help="Print safe metadata and hashes")
    status.add_argument("--period", required=True, choices=["AM", "PM"])
    status.add_argument("--state-root", type=Path, required=True)
    status.add_argument("--date")

    read = sub.add_parser("read", help="Print one saved batch for the research step")
    read.add_argument("--period", required=True, choices=["AM", "PM"])
    read.add_argument("--state-root", type=Path, required=True)
    read.add_argument("--date")
    read.add_argument("--max-bytes", type=int)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    runner: JsonCommandRunner | None = None,
    env_builder: NonarchiveEnvBuilder | None = None,
    now: datetime | None = None,
) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code in {0, None}:
            return 0
        return 1
    try:
        if args.command == "prepare":
            config = load_prepare_config(
                config_path=args.config,
                state_root=args.state_root,
                skill_dir=args.skill_dir,
                tmtb_script=args.tmtb_script,
                python_executable=args.python,
                repo_root=args.repo_root,
                digest_output_dir=args.digest_output_dir,
                tmtb_output_dir=args.tmtb_output_dir,
                timeout_seconds=args.timeout_seconds,
            )
            result = prepare_inputs(
                config,
                args.period,
                now=now,
                runner=runner,
                env_builder=env_builder,
            )
            _emit(result)
            if result.get("preparation_status") in {"ready", "ready_degraded"}:
                return 0
            return 2
        if args.command == "status":
            result = status_inputs(args.state_root, args.period, date=args.date, now=now)
            _emit(result)
            if result.get("preparation_status") == "absent":
                return 2
            return 0
        result = read_inputs(
            args.state_root,
            args.period,
            date=args.date,
            now=now,
            max_bytes=args.max_bytes if args.max_bytes is not None else DEFAULT_READ_MAX_BYTES,
        )
        _emit(result)
        return 0 if result.get("ok") else 2
    except PrepareConfigError as exc:
        _emit({"ok": False, "error": "config", "reason": str(exc)})
        return 1
    except Exception as exc:
        _emit({"ok": False, "error": "internal", "reason": type(exc).__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
