"""Shared Guidepoint exclusive owner lock and persistent startup ownership helpers (§6.4b (3)).

Pure Python standard library so both `guidepoint_mcp_proxy.py` (`:8943`) and
`host_connectors/guidepoint.py` (`:8951`) import the exact same lock and
`handoff-journal.json` ownership contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any

OWNER_LEGACY_PROXY: str = "legacy_proxy_8943"
OWNER_RESEARCH_HOST: str = "research_host_8951"
VALID_TARGET_OWNERS: frozenset[str] = frozenset(
    {OWNER_LEGACY_PROXY, OWNER_RESEARCH_HOST}
)

DEFAULT_OWNERSHIP_REL_PATH = Path(
    "var/dalton/cutover-snapshots/research-host/handoff-journal.json"
)


class GuidepointTokenLockBusyError(RuntimeError):
    """Raised when `.guidepoint.owner.lock` is already held by another owner process."""


class GuidepointStandbyError(RuntimeError):
    """Raised when a Guidepoint service is in standby or does not match `target_owner`."""


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def resolve_ownership_file(
    *,
    ownership_file: Path | str | None = None,
    repo_root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the canonical `handoff-journal.json` path used by both `:8943` and `:8951`."""
    if ownership_file is not None and str(ownership_file).strip():
        return Path(ownership_file).expanduser().resolve()
    env = os.environ if environ is None else environ
    explicit = (
        str(env.get("DALTON_GUIDEPOINT_OWNERSHIP_FILE") or "").strip()
        or str(env.get("DALTON_HOST_OWNERSHIP_FILE") or "").strip()
    )
    if explicit:
        return Path(explicit).expanduser().resolve()
    root = (
        Path(repo_root).expanduser().resolve()
        if repo_root is not None and str(repo_root).strip()
        else _default_repo_root()
    )
    return (root / DEFAULT_OWNERSHIP_REL_PATH).resolve()


def read_ownership_journal(
    *,
    ownership_file: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Read the persisted `handoff-journal.json` dict (empty dict if absent or unreadable)."""
    path = resolve_ownership_file(ownership_file=ownership_file, environ=environ)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_target_owner(
    *,
    ownership_file: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    default: str = OWNER_LEGACY_PROXY,
) -> str:
    """Read persisted `target_owner` from `handoff-journal.json` (defaults to `legacy_proxy_8943`)."""
    journal = read_ownership_journal(ownership_file=ownership_file, environ=environ)
    raw = str(journal.get("target_owner") or "").strip()
    if raw in VALID_TARGET_OWNERS:
        return raw
    return default


def write_ownership_journal_atomic(
    data: Mapping[str, Any],
    *,
    ownership_file: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Atomically persist `handoff-journal.json` via `fsync` + `os.replace` (mode 0600)."""
    path = resolve_ownership_file(ownership_file=ownership_file, environ=environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = dict(data)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".handoff-journal.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise
    return path


def set_target_owner(
    target_owner: str,
    *,
    ownership_file: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    extra_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Update `target_owner` (and optional fields) atomically in `handoff-journal.json`."""
    cleaned = str(target_owner or "").strip()
    if cleaned not in VALID_TARGET_OWNERS:
        raise ValueError(
            f"Invalid target_owner {target_owner!r}; expected one of {sorted(VALID_TARGET_OWNERS)}"
        )
    current = read_ownership_journal(ownership_file=ownership_file, environ=environ)
    if extra_fields:
        current.update(extra_fields)
    current["target_owner"] = cleaned
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_ownership_journal_atomic(
        current,
        ownership_file=ownership_file,
        environ=environ,
    )
    return current


def resolve_guidepoint_tokens_file(
    *,
    data_root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the canonical Guidepoint OAuth token JSON path."""
    env = os.environ if environ is None else environ
    explicit = (
        str(env.get("GUIDEPOINT_TOKENS_FILE") or "").strip()
        or str(env.get("DALTON_GUIDEPOINT_TOKENS_FILE") or "").strip()
    )
    if explicit:
        return Path(explicit).expanduser().resolve()
    if data_root is not None:
        base = Path(data_root).expanduser().resolve()
    else:
        raw_home = str(env.get("DALTON_RESEARCH_HOME") or "").strip()
        if raw_home:
            base = Path(raw_home).expanduser().resolve()
        else:
            base = (Path.home() / "Library" / "Application Support" / "Everflow" / "research").resolve()
    return base / "config" / "guidepoint_mcp_tokens.json"


def resolve_guidepoint_lock_file(token_file: Path | str) -> Path:
    """Return the shared `.guidepoint.owner.lock` path alongside `token_file`."""
    return Path(token_file).expanduser().resolve().parent / ".guidepoint.owner.lock"


class GuidepointOwnerLock:
    """Process-scoped exclusive `fcntl.flock` guard for `guidepoint_mcp_tokens.json`."""

    def __init__(self, token_file: Path | str, *, service_name: str = "guidepoint") -> None:
        self.token_file = Path(token_file).expanduser().resolve()
        self.lock_file = resolve_guidepoint_lock_file(self.token_file)
        self.service_name = service_name
        self._lock_fd: int | None = None

    @property
    def held(self) -> bool:
        return self._lock_fd is not None

    def acquire(self, *, timeout_s: float = 2.0) -> None:
        if self._lock_fd is not None:
            return
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_file), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        acquired = False
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise GuidepointTokenLockBusyError(
                            f"Guidepoint token owner lock busy: {self.lock_file}"
                        ) from None
                    time.sleep(0.05)
            meta = {
                "pid": os.getpid(),
                "service": self.service_name,
                "token_file": str(self.token_file),
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            payload = (json.dumps(meta) + "\n").encode("utf-8")
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload)
            os.fsync(fd)
            self._lock_fd = fd
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
        fd = self._lock_fd
        self._lock_fd = None
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


__all__ = [
    "DEFAULT_OWNERSHIP_REL_PATH",
    "GuidepointOwnerLock",
    "GuidepointStandbyError",
    "GuidepointTokenLockBusyError",
    "OWNER_LEGACY_PROXY",
    "OWNER_RESEARCH_HOST",
    "VALID_TARGET_OWNERS",
    "read_ownership_journal",
    "read_target_owner",
    "resolve_guidepoint_lock_file",
    "resolve_guidepoint_tokens_file",
    "resolve_ownership_file",
    "set_target_owner",
    "write_ownership_journal_atomic",
]
