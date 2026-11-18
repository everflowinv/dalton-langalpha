"""Shared active usage-accounting state directory pointer and resolution."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

DEFAULT_POINTER_FILE_STR = "~/.config/dalton/usage-state-dir"
LEGACY_STATE_DIR_STR = "~/.openclaw/usage-accounting"
NEW_STATE_DIR_STR = "~/Library/Application Support/Everflow/usage-accounting"


def resolve_pointer_file(pointer_file: Path | str | None = None) -> Path:
    if pointer_file is not None:
        return Path(os.path.expanduser(str(pointer_file)))
    raw = (os.environ.get("DALTON_USAGE_STATE_POINTER_FILE") or "").strip()
    return Path(raw or DEFAULT_POINTER_FILE_STR).expanduser()


def read_usage_state_pointer(pointer_file: Path | str | None = None) -> Path | None:
    target = resolve_pointer_file(pointer_file)
    if not target.is_file():
        return None
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line:
            return Path(line).expanduser()
    return None


def resolve_usage_state_dir(*, pointer_file: Path | str | None = None) -> Path:
    """Resolve active ledger dir: DALTON_USAGE_STATE_DIR -> pointer file -> legacy dir."""
    raw = (os.environ.get("DALTON_USAGE_STATE_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    pointed = read_usage_state_pointer(pointer_file)
    if pointed is not None:
        return pointed
    return Path(LEGACY_STATE_DIR_STR).expanduser()


def write_usage_state_pointer(
    state_dir: Path | str,
    *,
    pointer_file: Path | str | None = None,
) -> Path:
    """Atomically write the one-line active state directory pointer with mode 0600."""
    target = resolve_pointer_file(pointer_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    resolved_dir = os.path.abspath(os.path.expanduser(str(state_dir)))
    fd, tmp_name = tempfile.mkstemp(prefix=".usage-state-dir-", dir=target.parent)
    try:
        os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(resolved_dir + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target
