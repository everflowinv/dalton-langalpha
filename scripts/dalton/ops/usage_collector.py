#!/usr/bin/env python3
"""Harvest recent volatile model usage evidence via Dalton's model-usage-cost skill."""

from __future__ import annotations

import datetime as dt
import errno
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 120
HOMEBREW_PYTHON_312 = Path("/opt/homebrew/bin/python3.12")
SYSTEM_PYTHON_PATH = "/usr/bin/python3"

_REPO_SKILL_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "dalton"
    / "openclaw_overlays"
    / "skills"
    / "model-usage-cost"
    / "scripts"
)
if str(_REPO_SKILL_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_REPO_SKILL_SCRIPTS_DIR))
from usage_state import resolve_usage_state_dir  # noqa: E402


def resolve_repo_root() -> Path:
    raw = (os.environ.get("DALTON_REPO_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


def resolve_skill_dir(repo_root: Path | None = None) -> Path:
    raw = (os.environ.get("DALTON_USAGE_SKILL_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    root = repo_root if repo_root is not None else resolve_repo_root()
    return root / "scripts" / "dalton" / "openclaw_overlays" / "skills" / "model-usage-cost"


def resolve_state_dir() -> Path:
    return resolve_usage_state_dir()


def resolve_timeout_seconds() -> int:
    raw = (os.environ.get("DALTON_USAGE_COLLECTOR_TIMEOUT") or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return val if val > 0 else DEFAULT_TIMEOUT_SECONDS


def resolve_skill_python(
    skill_dir: Path,
    *,
    homebrew_python: Path = HOMEBREW_PYTHON_312,
    override_python: str | Path | None = None,
) -> str:
    """Pick DALTON_USAGE_PYTHON, the skill's own venv python, or Homebrew Python 3.12; never /usr/bin/python3."""
    raw_override = str(
        override_python if override_python is not None else (os.environ.get("DALTON_USAGE_PYTHON") or "")
    ).strip()
    if raw_override:
        cand = Path(raw_override).expanduser()
        if cand.is_file() and os.access(cand, os.X_OK):
            if os.path.realpath(str(cand)) != os.path.realpath(SYSTEM_PYTHON_PATH):
                return str(cand)
            raise FileNotFoundError(f"Refusing system Python for DALTON_USAGE_PYTHON: {cand}")
        raise FileNotFoundError(f"Configured DALTON_USAGE_PYTHON is not executable: {cand}")
    venv_py = skill_dir / "venv" / "bin" / "python"
    if venv_py.is_file() and os.access(venv_py, os.X_OK):
        return str(venv_py)
    if homebrew_python.is_file() and os.access(homebrew_python, os.X_OK):
        resolved = str(homebrew_python)
        if os.path.realpath(resolved) != os.path.realpath(SYSTEM_PYTHON_PATH):
            return resolved
    raise FileNotFoundError(
        f"No usable Python found: checked {venv_py} and {homebrew_python} (refusing {SYSTEM_PYTHON_PATH})"
    )


def _read_prior_status(status_path: Path) -> tuple[int, str | None]:
    if not status_path.is_file():
        return 0, None
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return 0, None
    if not isinstance(data, dict):
        return 0, None
    raw_failures = data.get("consecutive_failures")
    if isinstance(raw_failures, int) and not isinstance(raw_failures, bool) and raw_failures >= 0:
        prior_failures = raw_failures
    else:
        prior_failures = 0
    raw_last_ok = data.get("last_ok_at")
    if isinstance(raw_last_ok, str) and raw_last_ok.strip():
        prior_last_ok = raw_last_ok.strip()
    elif data.get("ok") is True and isinstance(data.get("collected_at"), str) and data["collected_at"].strip():
        prior_last_ok = data["collected_at"].strip()
    else:
        prior_last_ok = None
    return prior_failures, prior_last_ok


def write_status_atomic(status_path: Path, payload: dict[str, Any]) -> None:
    """Atomically write collector status JSON with mode 0600."""
    status_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".collector-status-", dir=status_path.parent)
    try:
        os.chmod(temp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, status_path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _acquire_nonblocking_lock(lock_path: Path) -> int | None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
    except OSError:
        pass
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            return None
        raise
    return fd


def collect_once(
    *,
    skill_dir: Path | None = None,
    state_dir: Path | None = None,
    timeout_seconds: int | None = None,
    homebrew_python: Path = HOMEBREW_PYTHON_312,
    run_fn: Any = subprocess.run,
    now_fn: Any = None,
) -> int:
    """Acquire non-blocking flock, run collect-broker --days 2 --json, and atomically update status."""
    resolved_skill_dir = skill_dir if skill_dir is not None else resolve_skill_dir()
    resolved_state_dir = state_dir if state_dir is not None else resolve_state_dir()
    resolved_timeout = timeout_seconds if timeout_seconds is not None else resolve_timeout_seconds()
    resolved_state_dir.mkdir(parents=True, exist_ok=True)

    lock_fd = _acquire_nonblocking_lock(resolved_state_dir / "collector.lock")
    if lock_fd is None:
        print("usage_collector: previous run still active, skipping", file=sys.stderr)
        return 0

    status_path = resolved_state_dir / "collector-status.json"
    try:
        prior_failures, prior_last_ok = _read_prior_status(status_path)
        cli_path = resolved_skill_dir / "scripts" / "model_usage_cost_cli.py"
        code = 1
        try:
            python_bin = resolve_skill_python(resolved_skill_dir, homebrew_python=homebrew_python)
            if not cli_path.is_file():
                raise FileNotFoundError(f"model_usage_cost_cli.py not found: {cli_path}")
            cmd = [python_bin, str(cli_path), "collect-broker", "--days", "2", "--json"]
            child_env = dict(os.environ)
            child_env["DALTON_USAGE_STATE_DIR"] = str(resolved_state_dir)
            result = run_fn(
                cmd,
                capture_output=True,
                text=True,
                timeout=resolved_timeout,
                env=child_env,
            )
            try:
                parsed = json.loads(result.stdout)
                status: dict[str, Any] = parsed if isinstance(parsed, dict) else {"ok": False, "error": "collector JSON must be an object"}
            except (ValueError, TypeError):
                status = {
                    "ok": False,
                    "error": "collector did not return JSON",
                    "hint": "Run model-usage-cost collect-broker --days 2 --json manually.",
                }
            code = int(result.returncode)
            if code != 0 and status.get("ok"):
                status["ok"] = False
        except subprocess.TimeoutExpired:
            status = {
                "ok": False,
                "error": f"collector timed out after {resolved_timeout}s",
                "hint": "Check local SQLite locks and source log sizes.",
            }
            code = 1
        except OSError as exc:
            status = {
                "ok": False,
                "error": str(exc),
                "hint": "Check skill installation, Python runtime, and local SQLite locks.",
            }
            code = 1

        now_dt = now_fn() if now_fn is not None else dt.datetime.now(dt.timezone.utc)
        now_iso = now_dt.isoformat()
        status["collected_at"] = now_iso
        if status.get("ok") is True and code == 0:
            status["consecutive_failures"] = 0
            status["last_ok_at"] = now_iso
        else:
            status["ok"] = False
            status["consecutive_failures"] = prior_failures + 1
            status["last_ok_at"] = prior_last_ok
            if code == 0:
                code = 1

        write_status_atomic(status_path, status)
        return code
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


def main() -> int:
    return collect_once()


if __name__ == "__main__":
    raise SystemExit(main())
