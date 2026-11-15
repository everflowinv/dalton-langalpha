"""Workspace-scoped file and JSON persistence helpers for richard-coverage-weekly."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


def load_sibling(module_name: str, filename: str) -> Any:
    """Load a sibling script module under ``module_name`` without mutating ``sys.path``."""
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    target = Path(__file__).resolve().with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, target)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load sibling module {target}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _walk_checked(root: Path, rel_parts: tuple[str, ...], raw_label: str) -> Path:
    cur = root
    for part in rel_parts:
        if part in ("", "."):
            continue
        cur = cur / part
        if cur.is_symlink():
            raise ValueError(f"path crosses symlink inside workspace: {raw_label}")
    target = cur.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes workspace root: {raw_label}") from exc
    return target


def workspace_file(workspace: Path, relative: str) -> Path:
    """Resolve ``relative`` inside ``workspace``, rejecting traversal or symlink escapes."""
    root = workspace.resolve()
    rel = Path(relative)
    if rel.is_absolute():
        raise ValueError(f"path escapes workspace root: {relative}")
    return _walk_checked(root, rel.parts, relative)


def resolve_workspace_target(workspace: Path | None, raw_path: str) -> Path:
    """Resolve a relative or absolute path bounded inside ``workspace`` (defaults to ``Path.cwd()``)."""
    ws = workspace if workspace is not None else Path.cwd()
    root = ws.resolve()
    ws_orig = Path(os.path.abspath(str(ws)))
    p_raw = Path(raw_path)
    if not p_raw.is_absolute():
        return _walk_checked(root, p_raw.parts, raw_path)

    candidate_rel: Path | None = None
    for prefix in (ws_orig, root, Path(ws)):
        try:
            candidate_rel = p_raw.relative_to(prefix)
            break
        except ValueError:
            continue
    if candidate_rel is None:
        abs_raw = Path(os.path.abspath(raw_path))
        for prefix in (ws_orig, root):
            try:
                candidate_rel = abs_raw.relative_to(prefix)
                break
            except ValueError:
                continue
    if candidate_rel is None:
        raise ValueError(f"path escapes workspace root: {raw_path}")
    return _walk_checked(root, candidate_rel.parts, raw_path)


def write_bytes_exclusive(path: Path, data: bytes) -> None:
    """Atomically write ``data`` to a new file; fail if ``path`` already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_path, path)
        except OSError:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            dest_fd = os.open(path, flags, 0o644)
            try:
                os.write(dest_fd, data)
                os.fsync(dest_fd)
            finally:
                os.close(dest_fd)
    finally:
        tmp_path.unlink(missing_ok=True)


def _atomic_write_text(target: Path, text: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def write_text(workspace: Path, relative: str, text: str) -> Path:
    """Atomically write UTF-8 text inside ``workspace``."""
    return _atomic_write_text(workspace_file(workspace, relative), text)


def write_target_text(workspace: Path | None, raw_path: str, text: str) -> Path:
    """Atomically write UTF-8 text to a workspace-bounded relative or absolute path."""
    return _atomic_write_text(resolve_workspace_target(workspace, raw_path), text)


def write_json(workspace: Path, relative: str, payload: Any) -> Path:
    """Serialize ``payload`` cleanly as UTF-8 JSON inside ``workspace``."""
    return write_text(workspace, relative, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
