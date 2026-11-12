#!/usr/bin/env python3
"""Import a bull/bear tracker snapshot into a Dalton workspace.

Offline and deterministic. Source and workspace paths are explicit arguments.
The default is a dry run; ``--apply`` writes. A conflict writes nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

SUPPORTED_TICKERS = ("MSFT", "META", "AMZN", "GOOGL")
MANIFEST_SCHEMA = "bullbear-tracker-import/1"
LEGACY_MANIFEST_SCHEMA = "msft-tracker-import/1"
SEEN_REL = "research/tracker/seen.json"
LAST_RUN_REL = "research/tracker/last_run.json"
LOG_REL = "research/tracker/updates.log"
WORKING_THESIS_REL = "research/tracker/thesis.md"
X_POOL_REL = "research/tracker/x_pool.json"
X_AUTHORS_REL = "research/tracker/x_authors.json"
FORBIDDEN_TARGETS = frozenset({"thesis.md", "data/thesis.json", "research/thesis.md"})

CONSENSUS_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}\.json$")
RUN_ID_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}) \| "
    r"(?P<run_id>\d{8}-\d{4}) \| material=(?P<material>[^|\s]+) \| "
    r"(?P<summary>.*)$"
)
CREDENTIAL_RE = re.compile(
    r"(token|password|secret|credential|api[_-]?key|authorization|cookie|bearer)",
    re.IGNORECASE,
)
POOL_FIELDS = (
    "name",
    "bio",
    "followers",
    "tickers",
    "reason",
    "active",
    "added_at",
    "added_by",
)
AUTHOR_FIELDS = ("handle", "name", "bio", "followers", "rest_id", "fetched_at")
SOURCE_FILES = ("seen.json", "last_run.json", "updates.log", "thesis.md", "deliveries.json")
_UNSAFE = object()


def import_bullbear_tracker(
    source_dir: Path | str,
    workspace_dir: Path | str,
    shared_state_dir: Path | str | None = None,
    apply: bool = False,
    ticker: str = "MSFT",
) -> dict[str, Any]:
    """Plan, and optionally write, one company tracker import.

    Returns counts, workspace-relative paths, source hashes, and conflicts.
    File bodies are not included. ``apply`` false leaves both trees unchanged.
    """
    source = Path(source_dir)
    workspace = Path(workspace_dir)
    shared = None if shared_state_dir is None else Path(shared_state_dir)
    plan: dict[str, Any] = {
        "conflicts": [],
        "hashes": {},
        "writes": [],
        "reused": [],
        "targets": {},
        "counts": _empty_counts(),
        "last_run_action": None,
        "working_thesis_action": None,
    }
    chosen = str(ticker or "").strip().upper()
    if chosen not in SUPPORTED_TICKERS:
        _conflict(plan, "ticker", "unsupported_ticker")
        return _result(plan, apply, written=[])
    plan["ticker"] = chosen
    plan["archive"] = f"research/tracker/imported-{chosen.lower()}"

    if not _real_dir(source):
        _conflict(plan, "source", "symlink_escape" if source.is_symlink() else "missing_source")
        return _result(plan, apply, written=[])
    if not _real_dir(workspace):
        reason = "symlink_escape" if workspace.is_symlink() else "missing_workspace"
        _conflict(plan, "workspace", reason)
        return _result(plan, apply, written=[])
    if shared is not None and not _real_dir(shared):
        reason = "symlink_escape" if shared.is_symlink() else "missing_source"
        _conflict(plan, "shared-state", reason)
        return _result(plan, apply, written=[])
    # Source and shared may nest (MSFT state under the stock-tracker root).
    # Workspace must not be either root, or a directory inside one of them.
    if _overlaps(source, workspace) or (shared is not None and _overlaps(workspace, shared)):
        _conflict(plan, "workspace", "overlapping_roots")
        return _result(plan, apply, written=[])

    files = _read_source_files(source, plan)
    consensus = _read_consensus(source, plan)
    if files is None or consensus is None:
        return _result(plan, apply, written=[])

    _plan_seen(plan, workspace, files["seen.json"])
    _plan_consensus(plan, workspace, consensus)
    _plan_archives(plan, workspace, files)
    _plan_working_thesis(plan, workspace, files["thesis.md"])
    log_index = _plan_log(plan, workspace, files["updates.log"])
    _plan_last_run(plan, workspace, files["last_run.json"], consensus, log_index)
    if shared is not None:
        _plan_shared(plan, workspace, shared)
    if plan["conflicts"]:
        return _result(plan, apply, written=[])

    _plan_manifest(plan, workspace, files["last_run.json"])
    if plan["conflicts"]:
        return _result(plan, apply, written=[])

    if not apply:
        return _result(plan, apply, written=[])

    written: list[str] = []
    for item in plan["writes"]:
        rel = item["path"]
        try:
            _atomic_write(workspace, rel, item["data"])
        except (OSError, ValueError):
            _conflict(plan, rel, "write_failed")
            failed = _result(plan, apply, written=written)
            failed["ok"] = False
            return failed
        written.append(rel)
    return _result(plan, apply, written=written)


def _empty_counts() -> dict[str, int]:
    return {
        "seen_source": 0,
        "seen_target": 0,
        "seen_merged": 0,
        "consensus_files": 0,
        "log_appended": 0,
        "log_reused": 0,
        "log_archive_only": 0,
        "x_accounts": 0,
        "x_authors": 0,
        "files_planned": 0,
        "files_written": 0,
        "files_reused": 0,
    }


def _conflict(plan: dict[str, Any], path: str, reason: str) -> None:
    plan["conflicts"].append({"path": path, "reason": reason})


def _real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _overlaps(left: Path, right: Path) -> bool:
    left_root = left.resolve()
    right_root = right.resolve()
    return (
        left_root == right_root
        or left_root in right_root.parents
        or right_root in left_root.parents
    )


def is_under(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _read_under(root: Path, rel: str, plan: dict[str, Any], label: str) -> bytes | None:
    parts = Path(rel).parts
    if Path(rel).is_absolute() or any(part in ("", ".", "..") for part in parts):
        _conflict(plan, label, "path_escape")
        return None
    path = root.joinpath(*parts)
    if path.is_symlink():
        try:
            resolved = path.resolve()
        except OSError:
            _conflict(plan, label, "symlink_escape")
            return None
        if not is_under(root, resolved):
            _conflict(plan, label, "symlink_escape")
            return None
    if not path.is_file():
        _conflict(plan, label, "missing_source")
        return None
    return path.read_bytes()


def _read_source_files(source: Path, plan: dict[str, Any]) -> dict[str, bytes] | None:
    files: dict[str, bytes] = {}
    for name in SOURCE_FILES:
        raw = _read_under(source, name, plan, name)
        if raw is None:
            return None
        files[name] = raw
        plan["hashes"][name] = hashlib.sha256(raw).hexdigest()
    return files


def _read_consensus(source: Path, plan: dict[str, Any]) -> dict[str, bytes] | None:
    consensus_dir = source / "consensus"
    if consensus_dir.is_symlink():
        try:
            resolved = consensus_dir.resolve()
        except OSError:
            _conflict(plan, "consensus", "symlink_escape")
            return None
        if not is_under(source, resolved):
            _conflict(plan, "consensus", "symlink_escape")
            return None
    if not consensus_dir.is_dir():
        _conflict(plan, "consensus", "missing_source")
        return None
    found: dict[str, bytes] = {}
    for entry in sorted(consensus_dir.iterdir(), key=lambda item: item.name):
        name = entry.name
        if name.startswith(".") or not CONSENSUS_NAME.match(name):
            continue
        if entry.is_symlink():
            try:
                resolved = entry.resolve()
            except OSError:
                _conflict(plan, f"consensus/{name}", "symlink_escape")
                return None
            if not is_under(source, resolved):
                _conflict(plan, f"consensus/{name}", "symlink_escape")
                return None
        if not entry.is_file():
            _conflict(plan, f"consensus/{name}", "path_escape")
            return None
        raw = entry.read_bytes()
        found[name] = raw
        plan["hashes"][f"consensus/{name}"] = hashlib.sha256(raw).hexdigest()
    plan["counts"]["consensus_files"] = len(found)
    return found


def _target_bytes(workspace: Path, rel: str, plan: dict[str, Any]) -> bytes | None | object:
    parts = Path(rel).parts
    if Path(rel).is_absolute() or any(part in ("", ".", "..") for part in parts):
        _conflict(plan, rel, "path_escape")
        return _UNSAFE
    current = workspace
    for part in parts:
        current = current / part
        if current.is_symlink():
            try:
                resolved = current.resolve()
            except OSError:
                _conflict(plan, rel, "symlink_escape")
                return _UNSAFE
            if not is_under(workspace, resolved):
                _conflict(plan, rel, "symlink_escape")
                return _UNSAFE
        if not current.exists():
            return None
    if not current.is_file():
        _conflict(plan, rel, "path_escape")
        return _UNSAFE
    return current.read_bytes()


def _queue(plan: dict[str, Any], rel: str, data: bytes, existing: bytes | None | object) -> None:
    if rel in FORBIDDEN_TARGETS:
        _conflict(plan, rel, "forbidden_target")
        return
    if existing is _UNSAFE:
        return
    if existing == data:
        plan["reused"].append(rel)
        return
    plan["writes"].append({"path": rel, "data": data})


def _json_bytes(obj: Any) -> bytes:
    text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return text.encode("utf-8")


def _parse_seen(raw: bytes) -> list[str] | None:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(obj, list) or any(not isinstance(item, str) for item in obj):
        return None
    return obj


def _plan_seen(plan: dict[str, Any], workspace: Path, raw: bytes) -> None:
    source_ids = _parse_seen(raw)
    if source_ids is None:
        _conflict(plan, "seen.json", "invalid_seen")
        return
    existing = _target_bytes(workspace, SEEN_REL, plan)
    if existing is _UNSAFE:
        return
    if existing is None:
        target_ids: list[str] = []
    else:
        parsed = _parse_seen(existing)
        if parsed is None:
            _conflict(plan, SEEN_REL, "invalid_seen")
            return
        target_ids = parsed
    merged = sorted(set(source_ids) | set(target_ids))
    plan["counts"]["seen_source"] = len(set(source_ids))
    plan["counts"]["seen_target"] = len(set(target_ids))
    plan["counts"]["seen_merged"] = len(merged)
    plan["targets"]["seen"] = SEEN_REL
    if existing is not None and set(target_ids) == set(merged):
        plan["reused"].append(SEEN_REL)
        return
    _queue(plan, SEEN_REL, _json_bytes(merged), None)


def _plan_consensus(plan: dict[str, Any], workspace: Path, consensus: dict[str, bytes]) -> None:
    paths: list[str] = []
    for name in sorted(consensus):
        rel = f"data/consensus/{name}"
        paths.append(rel)
        existing = _target_bytes(workspace, rel, plan)
        if existing is _UNSAFE:
            continue
        if existing is not None and existing != consensus[name]:
            _conflict(plan, rel, "consensus_conflict")
            continue
        _queue(plan, rel, consensus[name], existing)
    plan["targets"]["consensus"] = paths


def _plan_archives(plan: dict[str, Any], workspace: Path, files: dict[str, bytes]) -> None:
    archive = str(plan["archive"])
    mapping = {
        "thesis.md": f"{archive}/thesis.md",
        "updates.log": f"{archive}/updates.log",
        "deliveries.json": f"{archive}/deliveries.json",
    }
    keys = {
        "thesis.md": "archive_thesis",
        "updates.log": "archive_updates_log",
        "deliveries.json": "archive_deliveries",
    }
    for name, rel in mapping.items():
        plan["targets"][keys[name]] = rel
        existing = _target_bytes(workspace, rel, plan)
        if existing is _UNSAFE:
            continue
        if existing is not None and existing != files[name]:
            _conflict(plan, rel, "archive_conflict")
            continue
        _queue(plan, rel, files[name], existing)


def _plan_working_thesis(plan: dict[str, Any], workspace: Path, thesis: bytes) -> None:
    """Create the daily working thesis once. An existing file is kept as-is."""
    rel = WORKING_THESIS_REL
    plan["targets"]["working_thesis"] = rel
    existing = _target_bytes(workspace, rel, plan)
    if existing is _UNSAFE:
        return
    if existing is None:
        _queue(plan, rel, thesis, None)
        plan["working_thesis_action"] = "created"
        return
    plan["reused"].append(rel)
    plan["working_thesis_action"] = "reused"


def _valid_run_id(value: str) -> bool:
    match = RUN_ID_RE.match(value)
    if match is None:
        return False
    year, month, day, hour, minute = (int(part) for part in match.groups())
    try:
        date(year, month, day)
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


def _index_log(text: str, plan: dict[str, Any], label: str) -> dict[str, dict[str, str]] | None:
    indexed: dict[str, dict[str, str]] = {}
    archive_only = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        match = LOG_RE.match(line)
        if match is None or not _valid_run_id(match.group("run_id")):
            archive_only += 1
            continue
        run_id = match.group("run_id")
        row = {
            "line": line,
            "material": match.group("material"),
            "summary": match.group("summary").strip(),
        }
        prior = indexed.get(run_id)
        if prior is not None and prior["line"] != line:
            _conflict(plan, label, "log_run_conflict")
            return None
        indexed[run_id] = row
    if label == "updates.log":
        plan["counts"]["log_archive_only"] = archive_only
    return indexed


def _plan_log(plan: dict[str, Any], workspace: Path, raw: bytes) -> dict[str, dict[str, str]] | None:
    plan["targets"]["updates_log"] = LOG_REL
    try:
        source_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _conflict(plan, "updates.log", "invalid_log")
        return None
    source_index = _index_log(source_text, plan, "updates.log")
    if source_index is None:
        return None
    existing = _target_bytes(workspace, LOG_REL, plan)
    if existing is _UNSAFE:
        return source_index
    if existing is None:
        target_text = ""
        target_raw: bytes | None = None
    else:
        try:
            target_text = existing.decode("utf-8")
        except UnicodeDecodeError:
            _conflict(plan, LOG_REL, "invalid_log")
            return source_index
        target_raw = existing
    target_index = _index_log(target_text, plan, LOG_REL)
    if target_index is None:
        return source_index

    append: list[str] = []
    reused = 0
    for run_id, row in source_index.items():
        current = target_index.get(run_id)
        if current is None:
            append.append(row["line"])
            continue
        if current["line"] != row["line"]:
            _conflict(plan, LOG_REL, "log_run_conflict")
            continue
        reused += 1
    plan["counts"]["log_appended"] = len(append)
    plan["counts"]["log_reused"] = reused
    if plan["conflicts"] or not append:
        if target_raw is not None and not any(
            item["path"] == LOG_REL and item["reason"] == "log_run_conflict" for item in plan["conflicts"]
        ):
            plan["reused"].append(LOG_REL)
        return source_index
    extra = ("".join(f"{line}\n" for line in append)).encode("utf-8")
    base = target_raw or b""
    if base and not base.endswith(b"\n"):
        base += b"\n"
    _queue(plan, LOG_REL, base + extra, None)
    return source_index


def _parse_date(value: object) -> str | None:
    if not isinstance(value, str) or DATE_RE.match(value) is None:
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed


def _consensus_filename(snapshot_file: object, consensus: dict[str, bytes]) -> str | None:
    """Use only a matching basename. Absolute snapshot paths are not opened."""
    if not isinstance(snapshot_file, str) or "\x00" in snapshot_file:
        return None
    name = Path(snapshot_file).name
    if CONSENSUS_NAME.match(name) is None or name not in consensus:
        return None
    return name


def _summary_text(value: object) -> str | None | object:
    if value is None:
        return None
    if not isinstance(value, str):
        return _UNSAFE
    text = value.strip()
    return text or None


def _plan_last_run(
    plan: dict[str, Any],
    workspace: Path,
    raw: bytes,
    consensus: dict[str, bytes],
    log_index: dict[str, dict[str, str]] | None,
) -> None:
    plan["targets"]["last_run"] = LAST_RUN_REL
    try:
        source = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _conflict(plan, "last_run.json", "invalid_last_run")
        return
    if not isinstance(source, dict):
        _conflict(plan, "last_run.json", "invalid_last_run")
        return
    run_id = source.get("run_id")
    since = _parse_date(source.get("since"))
    until = _parse_date(source.get("until"))
    material = source.get("material")
    committed = _parse_timestamp(source.get("committed_at"))
    filename = _consensus_filename(source.get("snapshot_file"), consensus)
    if (
        not isinstance(run_id, str)
        or not _valid_run_id(run_id)
        or since is None
        or until is None
        or since > until
        or material not in ("yes", "no")
        or committed is None
        or filename is None
    ):
        _conflict(plan, "last_run.json", "invalid_timestamp" if committed is None else "invalid_last_run")
        return
    ticker = source.get("ticker", plan["ticker"])
    if not isinstance(ticker, str) or ticker.upper() != plan["ticker"]:
        _conflict(plan, "last_run.json", "invalid_last_run")
        return
    summary = _summary_text(source.get("summary")) if "summary" in source else None
    if summary is _UNSAFE:
        _conflict(plan, "last_run.json", "invalid_last_run")
        return
    if log_index is not None and run_id in log_index:
        logged = log_index[run_id]
        if logged["material"] != material:
            _conflict(plan, "last_run.json", "invalid_last_run")
            return
        logged_summary = logged["summary"] or None
        if summary is not None and logged_summary is not None and summary != logged_summary:
            _conflict(plan, "last_run.json", "invalid_last_run")
            return
        if summary is None:
            summary = logged_summary
    if "ae_covered_until" in source and _parse_date(source.get("ae_covered_until")) is None:
        _conflict(plan, "last_run.json", "invalid_last_run")
        return
    if "ae_resume" in source and source.get("ae_resume") is not None and not isinstance(source.get("ae_resume"), dict):
        _conflict(plan, "last_run.json", "invalid_last_run")
        return

    imported: dict[str, Any] = {
        "ticker": plan["ticker"],
        "run_id": run_id,
        "committed_at": source["committed_at"],
        "since": since,
        "until": until,
        "snapshot_file": f"data/consensus/{filename}",
        "material": material,
    }
    if isinstance(summary, str):
        imported["summary"] = summary
    if "ae_covered_until" in source:
        imported["ae_covered_until"] = source["ae_covered_until"]
    if isinstance(source.get("ae_resume"), dict):
        imported["ae_resume"] = source["ae_resume"]

    existing = _target_bytes(workspace, LAST_RUN_REL, plan)
    if existing is _UNSAFE:
        return
    if existing is None:
        plan["last_run_action"] = "create"
        _queue(plan, LAST_RUN_REL, _json_bytes(imported), None)
        return
    try:
        target = json.loads(existing.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _conflict(plan, LAST_RUN_REL, "invalid_last_run")
        return
    if not isinstance(target, dict):
        _conflict(plan, LAST_RUN_REL, "invalid_last_run")
        return
    target_time = _parse_timestamp(target.get("committed_at"))
    if target_time is None:
        _conflict(plan, LAST_RUN_REL, "invalid_timestamp")
        return
    if target_time > committed:
        plan["last_run_action"] = "keep_newer"
        plan["reused"].append(LAST_RUN_REL)
        return
    if target_time < committed:
        plan["last_run_action"] = "advance"
        _queue(plan, LAST_RUN_REL, _json_bytes(imported), None)
        return
    target_summary = _summary_text(target.get("summary")) if "summary" in target else None
    target_name = _consensus_filename(target.get("snapshot_file"), {filename: b""})
    # Equal instants compare the committed fields. A newer target is left as-is.
    same = (
        target.get("run_id") == run_id
        and target.get("since") == since
        and target.get("until") == until
        and target.get("material") == material
        and target_name == filename
        and target_summary == summary
    )
    if not same or target_summary is _UNSAFE:
        _conflict(plan, LAST_RUN_REL, "last_run_conflict")
        return
    plan["last_run_action"] = "reuse_equal"
    plan["reused"].append(LAST_RUN_REL)


def _credential_key(key: object) -> bool:
    return isinstance(key, str) and CREDENTIAL_RE.search(key) is not None


def _norm_handle(handle: str) -> str | None:
    text = handle.strip()
    if text.startswith("@"):
        text = text[1:].strip()
    if not text or "/" in text or "\\" in text or ".." in text or any(char.isspace() for char in text):
        return None
    return text.lower()


def _relevance(entry: object, ticker: str) -> str:
    if not isinstance(entry, dict):
        return "bad"
    active = entry.get("active", True)
    if active is False:
        return "no"
    if active is not True:
        return "bad"
    tickers = entry.get("tickers")
    if not isinstance(tickers, list) or any(not isinstance(item, str) for item in tickers):
        return "bad"
    if any(item.upper() in {ticker, "ALL"} for item in tickers):
        return "yes"
    return "no"


def _pool_fields_ok(entry: dict[str, Any]) -> bool:
    for key in POOL_FIELDS:
        if key not in entry:
            continue
        value = entry[key]
        if key == "tickers":
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                return False
        elif key == "active":
            if not isinstance(value, bool):
                return False
        elif key == "followers":
            if value is not None and type(value) not in (int, float):
                return False
        elif value is not None and not isinstance(value, str):
            return False
    return True


def _author_fields_ok(entry: dict[str, Any]) -> bool:
    for key in AUTHOR_FIELDS:
        if key not in entry:
            continue
        value = entry[key]
        if key == "followers":
            if value is not None and type(value) not in (int, float):
                return False
        elif value is not None and not isinstance(value, str):
            return False
    return True


def _whitelist(entry: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: entry[key] for key in fields if key in entry and not _credential_key(key)}


def _merge_records(
    plan: dict[str, Any],
    label: str,
    target: dict[str, Any],
    selected: dict[str, dict[str, Any]],
    fields: tuple[str, ...],
) -> dict[str, Any] | None:
    by_norm: dict[str, str] = {}
    merged: dict[str, Any] = {}
    for key, entry in target.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            _conflict(plan, label, "invalid_x")
            return None
        norm = _norm_handle(key)
        if norm is None or norm in by_norm:
            _conflict(plan, label, "invalid_x")
            return None
        by_norm[norm] = key
        merged[key] = dict(entry)
    for norm, entry in selected.items():
        incoming = _whitelist(entry, fields)
        if norm not in by_norm:
            merged[norm] = incoming
            continue
        current = merged[by_norm[norm]]
        for field, value in incoming.items():
            if field not in current:
                current[field] = value
                continue
            if field == "handle" and isinstance(current[field], str) and isinstance(value, str):
                if _norm_handle(current[field]) == _norm_handle(value):
                    continue
            if current[field] != value:
                _conflict(plan, label, "x_handle_conflict")
                return None
    return merged


def _load_object(raw: bytes) -> dict[str, Any] | None:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _select_pool(plan: dict[str, Any], pool: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    selected: dict[str, dict[str, Any]] = {}
    for handle, entry in pool.items():
        state = _relevance(entry, str(plan["ticker"]))
        if state == "no":
            continue
        if state == "bad" or not isinstance(handle, str) or not isinstance(entry, dict):
            _conflict(plan, "x_pool.json", "invalid_x")
            return None
        if not _pool_fields_ok(entry):
            _conflict(plan, "x_pool.json", "invalid_x")
            return None
        norm = _norm_handle(handle)
        if norm is None:
            _conflict(plan, "x_pool.json", "invalid_x")
            return None
        prior = selected.get(norm)
        if prior is not None and _whitelist(prior, POOL_FIELDS) != _whitelist(entry, POOL_FIELDS):
            _conflict(plan, "x_pool.json", "x_handle_conflict")
            return None
        selected[norm] = entry
    return selected


def _select_authors(
    plan: dict[str, Any],
    authors: dict[str, Any],
    pool_handles: set[str],
) -> dict[str, dict[str, Any]] | None:
    selected: dict[str, dict[str, Any]] = {}
    for key, entry in authors.items():
        if not isinstance(entry, dict):
            _conflict(plan, "x_authors.json", "invalid_x")
            return None
        handle = entry.get("handle", key)
        if not isinstance(handle, str):
            _conflict(plan, "x_authors.json", "invalid_x")
            return None
        norm = _norm_handle(handle)
        if norm is None or norm not in pool_handles:
            continue
        if not _author_fields_ok(entry):
            _conflict(plan, "x_authors.json", "invalid_x")
            return None
        copied = _whitelist(entry, AUTHOR_FIELDS)
        if "handle" not in copied:
            copied["handle"] = handle.strip().lstrip("@")
        prior = selected.get(norm)
        if prior is not None and _whitelist(prior, AUTHOR_FIELDS) != _whitelist(copied, AUTHOR_FIELDS):
            _conflict(plan, "x_authors.json", "x_handle_conflict")
            return None
        selected[norm] = copied
    return selected


def _plan_json_merge(
    plan: dict[str, Any],
    workspace: Path,
    rel: str,
    merged: dict[str, Any],
) -> None:
    existing = _target_bytes(workspace, rel, plan)
    if existing is _UNSAFE:
        return
    if existing is not None:
        current = _load_object(existing)
        if current is None:
            _conflict(plan, rel, "invalid_x")
            return
        if current == merged:
            plan["reused"].append(rel)
            return
    _queue(plan, rel, _json_bytes(merged), None)


def _plan_shared(plan: dict[str, Any], workspace: Path, shared: Path) -> None:
    pool_raw = _read_under(shared, "x_pool.json", plan, "x_pool.json")
    authors_raw = _read_under(shared, "x_authors.json", plan, "x_authors.json")
    if pool_raw is None or authors_raw is None:
        return
    plan["hashes"]["x_pool.json"] = hashlib.sha256(pool_raw).hexdigest()
    plan["hashes"]["x_authors.json"] = hashlib.sha256(authors_raw).hexdigest()
    pool = _load_object(pool_raw)
    authors = _load_object(authors_raw)
    if pool is None:
        _conflict(plan, "x_pool.json", "invalid_x")
        return
    if authors is None:
        _conflict(plan, "x_authors.json", "invalid_x")
        return
    selected = _select_pool(plan, pool)
    if selected is None:
        return
    selected_authors = _select_authors(plan, authors, set(selected))
    if selected_authors is None:
        return
    plan["counts"]["x_accounts"] = len(selected)
    plan["counts"]["x_authors"] = len(selected_authors)
    plan["targets"]["x_pool"] = X_POOL_REL
    plan["targets"]["x_authors"] = X_AUTHORS_REL
    target_pool = _target_object(plan, workspace, X_POOL_REL)
    target_authors = _target_object(plan, workspace, X_AUTHORS_REL)
    if target_pool is None or target_authors is None:
        return
    merged_pool = _merge_records(plan, X_POOL_REL, target_pool, selected, POOL_FIELDS)
    merged_authors = _merge_records(plan, X_AUTHORS_REL, target_authors, selected_authors, AUTHOR_FIELDS)
    if merged_pool is None or merged_authors is None:
        return
    _plan_json_merge(plan, workspace, X_POOL_REL, merged_pool)
    _plan_json_merge(plan, workspace, X_AUTHORS_REL, merged_authors)


def _target_object(plan: dict[str, Any], workspace: Path, rel: str) -> dict[str, Any] | None:
    existing = _target_bytes(workspace, rel, plan)
    if existing is _UNSAFE:
        return None
    if existing is None:
        return {}
    obj = _load_object(existing)
    if obj is None:
        _conflict(plan, rel, "invalid_x")
        return None
    return obj


def _same_manifest(existing: dict[str, Any], manifest: dict[str, Any]) -> bool:
    """Treat a legacy MSFT manifest as the same import when the payload matches."""
    schema = existing.get("schema")
    allowed = {MANIFEST_SCHEMA}
    if manifest.get("ticker") == "MSFT":
        allowed.add(LEGACY_MANIFEST_SCHEMA)
    if schema not in allowed:
        return False
    for key in ("ticker", "source_committed_at", "source_hashes", "targets"):
        if existing.get(key) != manifest.get(key):
            return False
    return set(existing) <= {"schema", "ticker", "source_committed_at", "source_hashes", "targets"}


def _plan_manifest(plan: dict[str, Any], workspace: Path, last_run_raw: bytes) -> None:
    manifest_rel = f"{plan['archive']}/manifest.json"
    try:
        source = json.loads(last_run_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _conflict(plan, "last_run.json", "invalid_last_run")
        return
    source_committed_at = source.get("committed_at") if isinstance(source, dict) else None
    if not isinstance(source_committed_at, str):
        _conflict(plan, manifest_rel, "invalid_last_run")
        return
    # The source record's committed_at, not the clock, so a repeat import matches.
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "ticker": plan["ticker"],
        "source_committed_at": source_committed_at,
        "source_hashes": dict(sorted(plan["hashes"].items())),
        "targets": plan["targets"],
    }
    plan["targets"]["manifest"] = manifest_rel
    existing = _target_bytes(workspace, manifest_rel, plan)
    if existing is _UNSAFE:
        return
    if isinstance(existing, bytes):
        try:
            previous = json.loads(existing.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            previous = None
        if isinstance(previous, dict) and _same_manifest(previous, manifest):
            plan["reused"].append(manifest_rel)
            return
    _queue(plan, manifest_rel, _json_bytes(manifest), existing)


def _atomic_write(workspace: Path, rel: str, data: bytes) -> None:
    if rel in FORBIDDEN_TARGETS or Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise ValueError(rel)
    path = workspace.joinpath(*Path(rel).parts)
    parent = path.parent
    if parent.exists() and not is_under(workspace, parent):
        raise ValueError(rel)
    parent.mkdir(parents=True, exist_ok=True)
    if not is_under(workspace, parent):
        raise ValueError(rel)
    if path.is_symlink():
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise ValueError(rel) from exc
        if not is_under(workspace, resolved):
            raise ValueError(rel)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _result(plan: dict[str, Any], apply: bool, written: list[str]) -> dict[str, Any]:
    conflicts = plan["conflicts"]
    ok = not conflicts
    planned = [] if conflicts else [item["path"] for item in plan["writes"]]
    reused = [] if conflicts else list(plan["reused"])
    if ok:
        plan["counts"]["files_planned"] = 0 if apply else len(planned)
        plan["counts"]["files_written"] = len(written)
        plan["counts"]["files_reused"] = len(reused)
    else:
        plan["counts"]["files_planned"] = 0
        plan["counts"]["files_written"] = len(written)
        plan["counts"]["files_reused"] = 0
    return {
        "ok": ok,
        "apply": apply,
        "dry_run": not apply,
        "last_run_action": plan["last_run_action"],
        "working_thesis": (
            {"path": WORKING_THESIS_REL, "action": plan.get("working_thesis_action")}
            if ok
            else None
        ),
        "counts": plan["counts"],
        "paths": {
            "targets": {} if conflicts else plan["targets"],
            "planned": planned,
            "written": written,
            "reused": reused,
        },
        "hashes": plan["hashes"],
        "conflicts": conflicts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import a bull/bear tracker snapshot into a Dalton workspace.")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--workspace-dir", required=True, type=Path)
    parser.add_argument("--shared-state-dir", type=Path)
    parser.add_argument("--apply", action="store_true", help="Write the planned files. Omit for a dry run.")
    args = parser.parse_args(argv)
    result = import_bullbear_tracker(
        args.source_dir,
        args.workspace_dir,
        shared_state_dir=args.shared_state_dir,
        apply=args.apply,
        ticker=args.ticker,
    )
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
