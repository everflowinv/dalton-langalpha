#!/usr/bin/env python3
"""Shared workspace state, seed bootstrap, path containment, locking, and SEC identity."""
from __future__ import annotations

import fcntl
import json
import os
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

SKILL_DIR = Path(__file__).resolve().parents[1]
SEED_ARCHIVE = SKILL_DIR / "data" / "seed.tar.gz"
STATE_SUBDIR = Path("state") / "dalton-bdc-analyst"
DEFAULT_EDGAR_IDENTITY = "DaltonResearch sec-contact@example.com"


def resolve_sec_identity() -> tuple[str, str]:
    """Resolve SEC EDGAR identity: SEC_USER_AGENT -> EDGAR_IDENTITY -> default."""
    sec_ua = os.getenv("SEC_USER_AGENT", "").strip()
    if sec_ua:
        return sec_ua, "SEC_USER_AGENT"
    edgar_id = os.getenv("EDGAR_IDENTITY", "").strip()
    if edgar_id:
        return edgar_id, "EDGAR_IDENTITY"
    return DEFAULT_EDGAR_IDENTITY, "default"


def is_within_workspace(resolved_path: Path, resolved_workspace: Path) -> bool:
    return resolved_path == resolved_workspace or resolved_path.is_relative_to(resolved_workspace)


def resolve_safe_state_dir(workspace_dir: str | Path) -> tuple[Path, Path]:
    """Validate that state_dir and its parents resolve strictly inside workspace_dir."""
    resolved_ws = Path(workspace_dir).resolve()
    candidates = (
        resolved_ws / "state",
        resolved_ws / STATE_SUBDIR,
        resolved_ws / STATE_SUBDIR / "data",
    )
    for cand in candidates:
        resolved_cand = cand.resolve()
        if not is_within_workspace(resolved_cand, resolved_ws):
            raise PermissionError(
                f"Refusing state path '{cand}' resolving outside workspace '{resolved_ws}' ('{resolved_cand}')"
            )
    return resolved_ws, (resolved_ws / STATE_SUBDIR).resolve()


def verify_safe_target(target: Path, resolved_ws: Path) -> Path:
    """Verify target and its parent resolve inside resolved_ws (blocks symlink escapes)."""
    parent_resolved = target.parent.resolve()
    if not is_within_workspace(parent_resolved, resolved_ws):
        raise PermissionError(
            f"Refusing path '{target.parent}' resolving outside workspace '{resolved_ws}'"
        )
    target_resolved = target.resolve()
    if not is_within_workspace(target_resolved, resolved_ws):
        raise PermissionError(
            f"Refusing target '{target}' resolving outside workspace '{resolved_ws}'"
        )
    return target


@contextmanager
def state_lock(state_dir: Path, resolved_ws: Path, lock_name: str = ".lock"):
    """Acquire an exclusive advisory lock inside state_dir after boundary checks."""
    resolve_safe_state_dir(resolved_ws)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = verify_safe_target(state_dir / lock_name, resolved_ws)
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def atomic_write_bytes(target: Path, data: bytes, resolved_ws: Path) -> None:
    verify_safe_target(target, resolved_ws)
    target.parent.mkdir(parents=True, exist_ok=True)
    verify_safe_target(target, resolved_ws)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(target: Path, payload: Any, resolved_ws: Path) -> None:
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    atomic_write_bytes(target, encoded, resolved_ws)


def _validate_seed_member(name: str) -> PurePosixPath | None:
    rel = PurePosixPath(name)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        return None
    if len(rel.parts) == 1 and rel.parts[0] == "company_db.json":
        return rel
    if len(rel.parts) == 2 and rel.parts[0] == "data" and rel.parts[1].endswith(".json"):
        return rel
    return None


def ensure_workspace_state(workspace_dir: str | Path) -> Path:
    """Ensure <workspace>/state/dalton-bdc-analyst/ has seed files without overwriting existing ones."""
    resolved_ws, state_dir = resolve_safe_state_dir(workspace_dir)
    if not SEED_ARCHIVE.is_file():
        raise FileNotFoundError(f"Missing BDC seed archive: {SEED_ARCHIVE}")

    with state_lock(state_dir, resolved_ws):
        resolve_safe_state_dir(resolved_ws)
        (state_dir / "data").mkdir(parents=True, exist_ok=True)
        with tarfile.open(SEED_ARCHIVE, "r:gz") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                rel = _validate_seed_member(member.name)
                if rel is None:
                    continue
                dest = state_dir / Path(*rel.parts)
                if dest.is_symlink():
                    verify_safe_target(dest, resolved_ws)
                verify_safe_target(dest, resolved_ws)
                if dest.exists():
                    continue
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                atomic_write_bytes(dest, extracted.read(), resolved_ws)
    return state_dir


def read_seed_json(member_name: str) -> dict[str, Any] | None:
    """Read a JSON member directly from read-only seed.tar.gz without disk writes."""
    if not SEED_ARCHIVE.is_file():
        return None
    target = member_name.lstrip("/")
    with tarfile.open(SEED_ARCHIVE, "r:gz") as tf:
        try:
            member = tf.getmember(target)
        except KeyError:
            return None
        if not member.isfile():
            return None
        f = tf.extractfile(member)
        if f is None:
            return None
        return json.load(f)


def list_seed_tickers() -> list[str]:
    """List BDC ticker names stored in seed.tar.gz."""
    if not SEED_ARCHIVE.is_file():
        return []
    tickers: list[str] = []
    with tarfile.open(SEED_ARCHIVE, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            rel = _validate_seed_member(member.name)
            if rel is not None and len(rel.parts) == 2 and not rel.parts[1].startswith("_"):
                tickers.append(Path(rel.parts[1]).stem)
    return sorted(tickers)
