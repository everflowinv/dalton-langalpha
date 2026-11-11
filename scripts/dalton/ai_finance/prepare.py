#!/usr/bin/env python3
"""Prepare one fixed AI-finance collection batch.

The function is an internal helper. It does not schedule a job, update the
broadcast seen file, write the signal database, or send a message.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

try:
    from .ai_finance_github_momentum import resolve_explicit_path
    from .ai_finance_github_trending import (
        UPSTREAM_MIN_REPOS,
        UPSTREAM_WINDOW_HOURS,
        collect_trending,
    )
    from .alphaengine_earnings_ai_scan import scan
except ImportError:
    from ai_finance_github_momentum import resolve_explicit_path
    from ai_finance_github_trending import (
        UPSTREAM_MIN_REPOS,
        UPSTREAM_WINDOW_HOURS,
        collect_trending,
    )
    from alphaengine_earnings_ai_scan import scan


class PrepareError(ValueError):
    """The caller supplied a batch that cannot be prepared."""


HKT = ZoneInfo("Asia/Hong_Kong")
_BATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_STAGE_FILES = {
    "github": "github_candidates.json",
    "earnings": "earnings_scan.json",
}
_PROCESS_LOCK = threading.Lock()


def prepare_batch(
    *,
    state_root: Path,
    batch_id: str,
    started_at: datetime,
    transport: Any = None,
    github_collector: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    earnings_collector: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    hours: int = 24,
    github_limit: int = 20,
    verify_limit: int = 100,
    max_documents: int = 80,
    workers: int = 4,
    queries: list[str] | None = None,
) -> dict[str, Any]:
    """Collect one batch under state_root/batches/{batch_id}.

    A later call reuses a stage whose completion record still matches its
    artifact, candidate texts, and frozen window. A failed stage is collected
    again only when that stage has not written its artifact. An artifact
    without a matching completion record is left on disk and reported
    inconsistent.
    """
    _validate_request(batch_id, started_at, hours)
    root = _require_state_root(state_root)
    batch_dir = root / "batches" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "candidates").mkdir(exist_ok=True)
    with _batch_lock(root / "locks" / f"batch-{batch_id}.lock"):
        return _prepare_locked(
            root=root,
            batch_dir=batch_dir,
            batch_id=batch_id,
            started_at=started_at,
            hours=hours,
            transport=transport,
            github_collector=github_collector or _default_github,
            earnings_collector=earnings_collector or _default_earnings,
            github_limit=github_limit,
            verify_limit=verify_limit,
            max_documents=max_documents,
            workers=workers,
            queries=queries,
        )


def _validate_request(batch_id: str, started_at: datetime, hours: int) -> None:
    if not isinstance(batch_id, str) or _BATCH_ID.fullmatch(batch_id) is None or ".." in batch_id:
        raise PrepareError("batch_id must be one safe path segment")
    if not isinstance(started_at, datetime) or started_at.tzinfo is None or started_at.tzinfo.utcoffset(started_at) is None:
        raise PrepareError("started_at must be timezone-aware")
    if type(hours) is not int or not 1 <= hours <= 168:
        raise PrepareError("hours must be an integer from 1 to 168")


def _require_state_root(state_root: Path) -> Path:
    if not isinstance(state_root, Path):
        state_root = Path(state_root)
    if not state_root.is_absolute():
        raise PrepareError("state_root must be an absolute path")
    if state_root.is_symlink():
        raise PrepareError("state_root must not be a symlink")
    try:
        resolved = resolve_explicit_path(state_root)
    except ValueError as exc:
        raise PrepareError(str(exc)) from exc
    if not resolved.is_dir():
        raise PrepareError("state_root must be an existing directory")
    return resolved


def _window(started_at: datetime, hours: int) -> dict[str, Any]:
    end = started_at.astimezone(HKT)
    start = end - timedelta(hours=hours)
    return {
        "timezone": "Asia/Hong_Kong",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "hours": hours,
    }


def _blank_metadata(batch_id: str, started_at: datetime, hours: int) -> dict[str, Any]:
    return {
        "schema": 1,
        "batch_id": batch_id,
        "started_at": started_at.isoformat(),
        "hours": hours,
        "window": _window(started_at, hours),
        "stages": {
            name: {
                "status": "pending",
                "artifact": filename,
                "sha256": None,
                "candidate_files": {},
            }
            for name, filename in _STAGE_FILES.items()
        },
        "manifest_sha256": None,
    }


def _prepare_locked(
    *,
    root: Path,
    batch_dir: Path,
    batch_id: str,
    started_at: datetime,
    hours: int,
    transport: Any,
    github_collector: Callable[[dict[str, Any]], dict[str, Any]],
    earnings_collector: Callable[[dict[str, Any]], dict[str, Any]],
    github_limit: int,
    verify_limit: int,
    max_documents: int,
    workers: int,
    queries: list[str] | None,
) -> dict[str, Any]:
    meta_path = batch_dir / "metadata.json"
    metadata, problem = _load_metadata(meta_path, batch_id)
    if problem:
        return _result(
            batch_id=batch_id,
            started_at=started_at.isoformat(),
            window=_window(started_at, hours),
            stages={},
            reused=[],
            manifest=None,
            errors=[problem],
            inconsistent=True,
        )
    if metadata is None:
        metadata = _blank_metadata(batch_id, started_at, hours)
        _atomic_write(meta_path, _canonical_bytes(metadata))
    else:
        conflict = _clock_conflict(metadata, started_at, hours)
        if conflict:
            return _result(
                batch_id=batch_id,
                started_at=str(metadata.get("started_at")),
                window=metadata.get("window"),
                stages=_stage_view(metadata),
                reused=[],
                manifest=None,
                errors=[conflict],
                inconsistent=False,
            )

    frozen = _parse_instant(metadata.get("started_at"))
    if frozen is None or not isinstance(metadata.get("window"), dict):
        return _result(
            batch_id=batch_id,
            started_at=started_at.isoformat(),
            window=_window(started_at, hours),
            stages=_stage_view(metadata),
            reused=[],
            manifest=None,
            errors=["metadata_inconsistent"],
            inconsistent=True,
        )
    request = {
        "state_file": root / "github_trending_history.json",
        "seen_file": root / "github_seen.json",
        "now": frozen,
        "limit": github_limit,
        "verify_limit": verify_limit,
        "window": metadata["window"],
        "candidate_dir": batch_dir / "candidates",
        "relative_root": batch_dir,
        "transport": transport,
        "hours": metadata["hours"],
        "workers": workers,
        "max_documents": max_documents,
        "queries": queries,
        "upstream_window_hours": UPSTREAM_WINDOW_HOURS,
        "min_cluster_repos": UPSTREAM_MIN_REPOS,
    }
    collectors = {"github": github_collector, "earnings": earnings_collector}
    reused: list[str] = []
    errors: list[str] = []
    for name in ("github", "earnings"):
        outcome, detail = _handle_stage(
            name=name,
            batch_dir=batch_dir,
            meta_path=meta_path,
            metadata=metadata,
            collector=collectors[name],
            request=request,
        )
        if outcome == "inconsistent":
            return _result(
                batch_id=batch_id,
                started_at=str(metadata.get("started_at")),
                window=metadata.get("window"),
                stages=_stage_view(metadata),
                reused=reused,
                manifest=None,
                errors=[detail or f"{name}_inconsistent"],
                inconsistent=True,
            )
        if outcome == "reused":
            reused.append(name)
        elif outcome == "failed":
            errors.append(f"{name}_failed:{detail}")
    if errors:
        return _result(
            batch_id=batch_id,
            started_at=str(metadata.get("started_at")),
            window=metadata.get("window"),
            stages=_stage_view(metadata),
            reused=reused,
            manifest=None,
            errors=errors,
            inconsistent=False,
        )
    manifest_error = _finalize_manifest(batch_dir, meta_path, metadata)
    if manifest_error:
        return _result(
            batch_id=batch_id,
            started_at=str(metadata.get("started_at")),
            window=metadata.get("window"),
            stages=_stage_view(metadata),
            reused=reused,
            manifest=None,
            errors=[manifest_error],
            inconsistent=True,
        )
    return _result(
        batch_id=batch_id,
        started_at=str(metadata.get("started_at")),
        window=metadata.get("window"),
        stages=_stage_view(metadata),
        reused=reused,
        manifest="manifest.json",
        errors=[],
        inconsistent=False,
    )


def _handle_stage(
    *,
    name: str,
    batch_dir: Path,
    meta_path: Path,
    metadata: dict[str, Any],
    collector: Callable[[dict[str, Any]], dict[str, Any]],
    request: dict[str, Any],
) -> tuple[str, str | None]:
    stage = metadata["stages"][name]
    status = stage.get("status")
    if status == "complete":
        problem = _stage_problem(batch_dir, name, stage)
        if problem:
            return "inconsistent", problem
        return "reused", None
    if status not in {"pending", "failed"}:
        return "inconsistent", f"{name}_inconsistent"
    artifact_path = batch_dir / _STAGE_FILES[name]
    if _entry_exists(artifact_path):
        # Hashes stored for this batch are the completion record. The artifact's
        # presence is not. Reuse a record that still matches the files and the
        # frozen window; otherwise leave the artifact and candidate files alone.
        problem = _completion_problem(batch_dir, name, stage, metadata)
        if problem:
            return "inconsistent", problem
        stage["status"] = "complete"
        stage.pop("error", None)
        _atomic_write(meta_path, _canonical_bytes(metadata))
        return "reused", None
    wrote_artifact = False
    try:
        payload = collector(request)
        if not isinstance(payload, dict):
            raise PrepareError(f"{name} payload must be an object")
        if name == "earnings":
            _require_earnings_window(payload, request["window"])
            files = _candidate_files(payload, batch_dir)
        else:
            files = {}
        body = _canonical_bytes(payload)
        digest = hashlib.sha256(body).hexdigest()
        _atomic_write(artifact_path, body)
        wrote_artifact = True
        stage["sha256"] = digest
        stage["candidate_files"] = files
        stage.pop("error", None)
        stage["status"] = "complete"
        _atomic_write(meta_path, _canonical_bytes(metadata))
        return "ran", None
    except Exception as exc:
        stage["status"] = "failed"
        stage["error"] = type(exc).__name__
        # Hashes taken from an artifact already on disk stay as its completion
        # record. Only a stage that has not written its artifact is cleared so
        # a later call can collect it.
        if not wrote_artifact:
            stage["sha256"] = None
            stage["candidate_files"] = {}
        _atomic_write(meta_path, _canonical_bytes(metadata))
        return "failed", type(exc).__name__


def _stage_problem(batch_dir: Path, name: str, stage: dict[str, Any]) -> str | None:
    if stage.get("artifact") != _STAGE_FILES[name] or not isinstance(stage.get("sha256"), str):
        return f"{name}_inconsistent"
    try:
        actual = _hash_file(batch_dir / _STAGE_FILES[name])
    except PrepareError:
        return f"{name}_inconsistent"
    if actual != stage["sha256"]:
        return f"{name}_inconsistent"
    files = stage.get("candidate_files")
    if not isinstance(files, dict):
        return f"{name}_inconsistent"
    for rel, digest in files.items():
        if not isinstance(rel, str) or not isinstance(digest, str) or _safe_candidate_parts(rel) is None:
            return f"{name}_inconsistent"
        try:
            actual_file = _hash_file(batch_dir.joinpath(*rel.split("/")))
        except PrepareError:
            return f"{name}_inconsistent"
        if actual_file != digest:
            return f"{name}_inconsistent"
    return None


def _entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _completion_problem(
    batch_dir: Path,
    name: str,
    stage: dict[str, Any],
    metadata: dict[str, Any],
) -> str | None:
    """Return an error when this batch has no usable completion record.

    The record is the artifact hash and candidate-file hashes in this batch's
    metadata. Those hashes must match the files on disk. An earnings artifact
    must also repeat the batch's frozen window. A file with the stage's name
    is not a record.
    """
    recorded_hash = stage.get("sha256")
    recorded_files = stage.get("candidate_files")
    if not isinstance(recorded_hash, str) or not isinstance(recorded_files, dict):
        return f"{name}_uncommitted"
    problem = _stage_problem(batch_dir, name, stage)
    if problem:
        return problem
    if name != "earnings":
        return None
    try:
        payload = json.loads(_read_regular(batch_dir / _STAGE_FILES[name]))
    except (OSError, PrepareError, json.JSONDecodeError, UnicodeError):
        return f"{name}_inconsistent"
    if not isinstance(payload, dict):
        return f"{name}_inconsistent"
    window = metadata.get("window")
    if not isinstance(window, dict):
        return f"{name}_inconsistent"
    try:
        _require_earnings_window(payload, window)
        files = _candidate_files(payload, batch_dir)
    except PrepareError:
        return f"{name}_inconsistent"
    if files != recorded_files:
        return f"{name}_inconsistent"
    return None


def _require_earnings_window(payload: dict[str, Any], window: dict[str, Any]) -> None:
    got = payload.get("window")
    if not isinstance(got, dict):
        raise PrepareError("earnings window is missing")
    for key in ("timezone", "start", "end", "hours"):
        if got.get(key) != window[key]:
            raise PrepareError("earnings window does not match the frozen batch window")


def _candidate_files(payload: dict[str, Any], batch_dir: Path) -> dict[str, str]:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise PrepareError("earnings candidates must be a list")
    files: dict[str, str] = {}
    seen: set[str] = set()
    for item in candidates:
        if not isinstance(item, dict):
            raise PrepareError("earnings candidate must be an object")
        doc_id = item.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id or doc_id in seen:
            raise PrepareError("earnings candidate doc_id must be unique")
        seen.add(doc_id)
        rel = item.get("full_text_path")
        parts = _safe_candidate_parts(rel)
        if parts is None:
            raise PrepareError("candidate text must use a relative path under candidates/")
        path = batch_dir.joinpath(*parts)
        if path.parent != batch_dir / "candidates":
            raise PrepareError("candidate text must use a relative path under candidates/")
        files[rel] = _hash_file(path)
    return files


def _safe_candidate_parts(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str) or "\\" in value or value.startswith("/"):
        return None
    parts = value.split("/")
    if len(parts) != 2 or any(part in {"", ".", ".."} for part in parts):
        return None
    if parts[0] != "candidates":
        return None
    return parts[0], parts[1]


def _finalize_manifest(batch_dir: Path, meta_path: Path, metadata: dict[str, Any]) -> str | None:
    try:
        manifest = _manifest_payload(batch_dir, metadata)
        body = _canonical_bytes(manifest)
    except (PrepareError, json.JSONDecodeError, OSError):
        return "manifest_inconsistent"
    digest = hashlib.sha256(body).hexdigest()
    path = batch_dir / "manifest.json"
    recorded = metadata.get("manifest_sha256")
    if isinstance(recorded, str):
        try:
            actual = _hash_file(path)
        except PrepareError:
            return "manifest_inconsistent"
        if actual != recorded:
            return "manifest_inconsistent"
        return None
    _atomic_write(path, body)
    metadata["manifest_sha256"] = digest
    _atomic_write(meta_path, _canonical_bytes(metadata))
    return None


def _manifest_payload(batch_dir: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(_read_regular(batch_dir / "earnings_scan.json"))
    recorded = metadata["stages"]["earnings"].get("candidate_files") or {}
    rows = []
    for item in payload.get("candidates") or []:
        rel = item.get("full_text_path")
        doc_id = item.get("doc_id")
        if not isinstance(rel, str) or not isinstance(doc_id, str) or rel not in recorded:
            raise PrepareError("manifest candidate is not in the earnings stage")
        rows.append({"doc_id": doc_id, "path": rel, "sha256": recorded[rel]})
    rows.sort(key=lambda row: row["doc_id"])
    return {
        "schema": 1,
        "batch_id": metadata["batch_id"],
        "started_at": metadata["started_at"],
        "window": metadata["window"],
        "artifacts": {
            "github_candidates": {
                "path": "github_candidates.json",
                "sha256": metadata["stages"]["github"]["sha256"],
            },
            "earnings_scan": {
                "path": "earnings_scan.json",
                "sha256": metadata["stages"]["earnings"]["sha256"],
            },
        },
        "candidates": rows,
    }


def _load_metadata(path: Path, batch_id: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        path.lstat()
    except FileNotFoundError:
        return None, None
    try:
        data = json.loads(_read_regular(path))
    except (OSError, PrepareError, json.JSONDecodeError, UnicodeError):
        return None, "metadata_inconsistent"
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("batch_id") != batch_id:
        return None, "metadata_inconsistent"
    stages = data.get("stages")
    if not isinstance(stages, dict) or set(stages) != set(_STAGE_FILES):
        return None, "metadata_inconsistent"
    for name, stage in stages.items():
        if not isinstance(stage, dict) or stage.get("artifact") != _STAGE_FILES[name]:
            return None, "metadata_inconsistent"
    if _parse_instant(data.get("started_at")) is None or type(data.get("hours")) is not int:
        return None, "metadata_inconsistent"
    expected = _window(_parse_instant(data["started_at"]), data["hours"])
    if data.get("window") != expected:
        return None, "metadata_inconsistent"
    return data, None


def _clock_conflict(metadata: dict[str, Any], started_at: datetime, hours: int) -> str | None:
    frozen = _parse_instant(metadata.get("started_at"))
    if frozen is None or metadata.get("hours") != hours or frozen != _as_utc(started_at):
        return "started_at_conflict"
    return None


def _parse_instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _stage_view(metadata: dict[str, Any]) -> dict[str, str]:
    stages = metadata.get("stages")
    if not isinstance(stages, dict):
        return {}
    return {
        name: str(stage.get("status"))
        for name, stage in stages.items()
        if isinstance(stage, dict)
    }


def _result(
    *,
    batch_id: str,
    started_at: str,
    window: Any,
    stages: dict[str, str],
    reused: list[str],
    manifest: str | None,
    errors: list[str],
    inconsistent: bool,
) -> dict[str, Any]:
    return {
        "ok": not errors and not inconsistent and stages.get("github") == "complete" and stages.get("earnings") == "complete",
        "batch_id": batch_id,
        "started_at": started_at,
        "window": window,
        "stages": stages,
        "reused": reused,
        "manifest": manifest,
        "errors": errors,
        "inconsistent": inconsistent,
    }


def _default_github(request: dict[str, Any]) -> dict[str, Any]:
    return collect_trending(
        limit=request["limit"],
        verify_limit=request["verify_limit"],
        state_file=request["state_file"],
        seen_file=request["seen_file"],
        output=None,
        upstream_window_hours=request["upstream_window_hours"],
        min_cluster_repos=request["min_cluster_repos"],
        update_state_file=True,
        now=request["now"],
    )


def _default_earnings(request: dict[str, Any]) -> dict[str, Any]:
    return scan(
        SimpleNamespace(
            transport=request["transport"],
            now=request["now"],
            relative_root=request["relative_root"],
            candidate_dir=request["candidate_dir"],
            hours=request["hours"],
            workers=request["workers"],
            max_documents=request["max_documents"],
            query=request["queries"],
        )
    )


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _read_regular(path: Path) -> bytes:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise PrepareError(f"missing regular file: {path.name}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise PrepareError(f"not a regular file: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PrepareError(f"not a regular file: {path.name}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PrepareError(f"not a regular file: {path.name}")
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _hash_file(path: Path) -> str:
    return hashlib.sha256(_read_regular(path)).hexdigest()


@contextlib.contextmanager
def _batch_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with _PROCESS_LOCK:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
