"""Workspace path checks for the global consumer weekly skill.

Writes go through a new temp file so an existing symlink is not followed.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def workspace_file(workspace: Path, relative: str) -> Path:
    """Resolve a workspace-relative file and reject links and path escape."""
    raw = Path(relative)
    if raw.is_absolute() or not raw.parts or any(part in ("", ".", "..") for part in raw.parts):
        raise ValueError("path must stay inside the workspace")
    path = workspace.joinpath(*raw.parts)
    if path.is_symlink():
        raise ValueError("refusing symlink")
    parent = path.parent.resolve()
    root = workspace.resolve()
    if parent != root and root not in parent.parents:
        raise ValueError("path must stay inside the workspace")
    return path


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("incomplete write")
        view = view[written:]


def write_bytes_exclusive(path: Path, payload: bytes) -> None:
    """Replace ``path`` via a new temp file so an existing symlink is not followed."""
    if path.is_symlink():
        raise ValueError("refusing symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        try:
            _write_all(fd, payload)
        finally:
            os.close(fd)
        os.replace(name, path)
    except OSError:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
