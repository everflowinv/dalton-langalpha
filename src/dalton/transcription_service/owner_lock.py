"""Exclusive advisory file lock (``.owner.lock``) for ASR job directory ownership.

Both the legacy ``scripts/dalton/researchdata_bridge.py`` and the standalone
``src/dalton/transcription_service`` acquire ``<DALTON_ASR_JOBS_DIR>/.owner.lock``
BEFORE constructing ``AsrJobManager``, running ``recover_incomplete_jobs()``, or
accepting transcription requests. This eliminates dual-recovery and concurrent
write races during cutover and rollback.

Design invariants:
- Pure Python standard library only (safe under system Homebrew Python).
- Uses ``fcntl.flock`` (per open-file-description advisory lock), which enforces
  mutual exclusion both across OS processes and across independent handles inside
  the same process (e.g., unit tests).
- Never opens with ``O_TRUNC`` before holding the lock and never unlinks the
  lock file on release, preventing inode-swap races and preserving holder PID
  diagnostics for contending callers.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

OWNER_LOCK_FILENAME: str = ".owner.lock"
DEFAULT_OWNER_LOCK_TIMEOUT_S: float = 5.0
DEFAULT_OWNER_LOCK_POLL_S: float = 0.05


@dataclass(frozen=True)
class OwnerLockMetadata:
    """Diagnostic metadata stored inside ``<DALTON_ASR_JOBS_DIR>/.owner.lock``."""

    pid: int | None = None
    service: str = "unknown"
    hostname: str = "unknown"
    started_at: str = "unknown"
    jobs_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "service": self.service,
            "hostname": self.hostname,
            "started_at": self.started_at,
            "jobs_dir": self.jobs_dir,
        }


def read_owner_lock_metadata(lock_path: Path) -> OwnerLockMetadata:
    """Best-effort, non-blocking read of holder metadata from ``lock_path``."""
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return OwnerLockMetadata()
    if not raw:
        return OwnerLockMetadata()
    try:
        parsed = json.loads(raw)
    except Exception:
        return OwnerLockMetadata()
    if not isinstance(parsed, dict):
        return OwnerLockMetadata()
    raw_pid = parsed.get("pid")
    pid = int(raw_pid) if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else None
    return OwnerLockMetadata(
        pid=pid,
        service=str(parsed.get("service") or "unknown").strip() or "unknown",
        hostname=str(parsed.get("hostname") or "unknown").strip() or "unknown",
        started_at=str(parsed.get("started_at") or "unknown").strip() or "unknown",
        jobs_dir=str(parsed.get("jobs_dir") or "").strip(),
    )


def format_owner_lock_busy_diagnostic(
    lock_path: Path,
    holder: OwnerLockMetadata,
    *,
    timeout_s: float,
) -> str:
    """Format the canonical diagnostic message when ``.owner.lock`` is held by another owner."""
    pid_str = str(holder.pid) if holder.pid is not None else "unknown"
    return (
        f"ASR owner lock busy: path={lock_path} "
        f"holder_pid={pid_str} "
        f"holder_service={holder.service} "
        f"holder_started_at={holder.started_at} "
        f"timeout_s={timeout_s:.2f}"
    )


class OwnerLockBusyError(RuntimeError):
    """Raised when ``<DALTON_ASR_JOBS_DIR>/.owner.lock`` cannot be acquired before timeout."""

    def __init__(
        self,
        lock_path: Path,
        holder: OwnerLockMetadata,
        *,
        timeout_s: float,
    ) -> None:
        self.lock_path = Path(lock_path)
        self.holder = holder
        self.holder_pid = holder.pid
        self.holder_service = holder.service
        self.holder_started_at = holder.started_at
        self.timeout_s = float(timeout_s)
        super().__init__(
            format_owner_lock_busy_diagnostic(self.lock_path, holder, timeout_s=self.timeout_s)
        )


def resolve_owner_lock_timeout_s(default: float = DEFAULT_OWNER_LOCK_TIMEOUT_S) -> float:
    """Parse ``DALTON_ASR_LOCK_TIMEOUT_S`` from environment (clamped to >= 0.0)."""
    raw = os.environ.get("DALTON_ASR_LOCK_TIMEOUT_S", "").strip()
    if not raw:
        return max(0.0, float(default))
    try:
        return max(0.0, float(raw))
    except ValueError:
        return max(0.0, float(default))


class AsrOwnerLock:
    """Exclusive owner lock bound to ``<jobs_dir>/.owner.lock``."""

    def __init__(
        self,
        jobs_dir: Path | str,
        *,
        service_name: str = "dalton-transcription",
        poll_interval_s: float = DEFAULT_OWNER_LOCK_POLL_S,
    ) -> None:
        self.jobs_dir = Path(jobs_dir).expanduser().resolve()
        self.lock_path = self.jobs_dir / OWNER_LOCK_FILENAME
        self.service_name = str(service_name or "dalton-transcription").strip()
        self.poll_interval_s = max(0.005, float(poll_interval_s))
        self._fd: int | None = None
        self._metadata: OwnerLockMetadata | None = None

    @property
    def is_held(self) -> bool:
        return self._fd is not None

    @property
    def metadata(self) -> OwnerLockMetadata | None:
        return self._metadata

    def acquire(self, *, timeout_s: float | None = None) -> OwnerLockMetadata:
        """Acquire the exclusive lock or raise :class:`OwnerLockBusyError` with holder diagnostics."""
        if self._fd is not None and self._metadata is not None:
            return self._metadata

        eff_timeout = (
            resolve_owner_lock_timeout_s()
            if timeout_s is None
            else max(0.0, float(timeout_s))
        )
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + eff_timeout
        acquired = False
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        holder = read_owner_lock_metadata(self.lock_path)
                        raise OwnerLockBusyError(
                            self.lock_path,
                            holder,
                            timeout_s=eff_timeout,
                        ) from None
                    time.sleep(min(self.poll_interval_s, remaining))

            meta = OwnerLockMetadata(
                pid=os.getpid(),
                service=self.service_name,
                hostname=socket.gethostname(),
                started_at=datetime.now(UTC).isoformat(),
                jobs_dir=str(self.jobs_dir),
            )
            payload_bytes = (
                json.dumps(meta.to_dict(), ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload_bytes)
            os.fsync(fd)
            self._fd = fd
            self._metadata = meta
            return meta
        except BaseException:
            if acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def release(self) -> None:
        """Release the exclusive lock (idempotent; never unlinks the lock file)."""
        fd = self._fd
        self._fd = None
        self._metadata = None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> AsrOwnerLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


__all__ = [
    "AsrOwnerLock",
    "DEFAULT_OWNER_LOCK_POLL_S",
    "DEFAULT_OWNER_LOCK_TIMEOUT_S",
    "OWNER_LOCK_FILENAME",
    "OwnerLockBusyError",
    "OwnerLockMetadata",
    "format_owner_lock_busy_diagnostic",
    "read_owner_lock_metadata",
    "resolve_owner_lock_timeout_s",
]
