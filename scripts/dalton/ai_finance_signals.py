"""Save one reviewed AI-finance signal bundle and run compare, ingest, and stats.

The model supplies business JSON only. Paths, the interpreter, the embedding
settings, and the API key come from the operator config and the bridge child
environment. This module does not send a report, write broadcast seen, or
schedule a job.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import stat
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from scripts.dalton.ai_finance.ai_finance_daily_validate import validate_records
    from scripts.dalton.ai_finance.ai_finance_signal_memory import (
        EMBEDDING_DIM,
        EMBEDDING_MODEL,
        canonical_record,
    )
    from scripts.dalton.bridge_subprocess_env import build_nonarchive_subprocess_env
    from scripts.dalton.market_digest_inputs import ChildResult, run_json_command
except ImportError:
    from ai_finance.ai_finance_daily_validate import validate_records
    from ai_finance.ai_finance_signal_memory import EMBEDDING_DIM, EMBEDDING_MODEL, canonical_record
    from bridge_subprocess_env import build_nonarchive_subprocess_env
    from market_digest_inputs import ChildResult, run_json_command

PROFILE = "ai-finance-daily"
HKT = ZoneInfo("Asia/Hong_Kong")
COMPARE_LIMIT = 8
SIGNAL_SUBPROCESS_TIMEOUT_SECONDS = 900.0
STATS_SUBPROCESS_TIMEOUT_SECONDS = 60.0
MAX_RESPONSE_BYTES = 128 * 1024
DEFAULT_MAX_CHARS = 16000
MAX_CHARS = 32000
_OBJECT_MAX_BYTES = 256 * 1024
# A prepared earnings scan still holds at most 80 transcripts. Signal rows may
# add GitHub records on top of those 80, so the bundle ceiling is 256. The
# original validator still matches each row to the scan and the candidate review.
_EARNINGS_SCAN_MAX = 80
_MAX_RECORDS = 256
_MAX_STRING = 4000
_MAX_DEPTH = 8
_SOURCE_MAX_BYTES = 32 * 1024 * 1024
_STDOUT_LIMIT = 256 * 1024
_STDERR_LIMIT = 64 * 1024
# CLI compare output is indent-2 UTF-8 JSON: one current record plus up to
# COMPARE_LIMIT historical hits, for at most _MAX_RECORDS currents. Each side
# repeats the long text fields at _MAX_STRING characters, and non-ASCII text
# is up to 4 bytes per character. HTTP pages stay at MAX_RESPONSE_BYTES.
_COMPARE_TEXT_FIELDS = 9
_COMPARE_UTF8_BYTES_PER_CHAR = 4
_COMPARE_RECORD_BYTES = (
    _COMPARE_TEXT_FIELDS * _MAX_STRING * _COMPARE_UTF8_BYTES_PER_CHAR
    + 8192
)
_COMPARE_FILE_MAX = (
    _MAX_RECORDS * (1 + COMPARE_LIMIT) * _COMPARE_RECORD_BYTES
    + 65_536
)
_EMBEDDING_EXCEPTIONS = frozenset({
    "RuntimeError",
    "HTTPError",
    "URLError",
    "TimeoutError",
    "JSONDecodeError",
})
_ACTIONS = frozenset({"save", "compare", "ingest", "stats", "read"})
_COMMON_KEYS = frozenset({
    "action",
    "batch_id",
    "automation_execution_id",
    "owner_user_id",
    "user_id",
    "workspace_id",
    "thread_id",
    "profile",
})
_SAVE_KEYS = _COMMON_KEYS | {
    "github_decisions",
    "earnings_dispositions",
    "signal_records",
}
_READ_KEYS = _COMMON_KEYS | {"offset", "max_chars"}
_SEARCH_MODES = frozenset({"empty_history", "embedding", "fts", "none"})
_HISTORY_STATUS = frozenset({"empty", "complete", "partial", "none"})
_DIRECTIONS = frozenset({"positive", "negative", "mixed"})
_RESPONSE_STATUS = frozenset({"direct", "partial", "evasive_or_unanswered"})
_SIGNAL_KEYS = frozenset({
    "record_key",
    "source_type",
    "external_id",
    "event_time",
    "title",
    "entity",
    "industry",
    "url",
    "summary",
    "evidence",
    "metrics",
    "signal_types",
    "signal_direction",
    "evidence_status",
    "qa_concern",
    "management_response",
    "management_response_status",
    "tam_mapping",
    "investment_implication",
    "tags",
})
_DECISION_KEYS = frozenset({
    "repo",
    "decision",
    "reason",
    "readme_url",
    "displayed",
    "upstream",
    "upstream_evidence",
})
_DISPOSITION_KEYS = frozenset({
    "doc_id",
    "entity",
    "disposition",
    "signal_direction",
    "evidence_status",
    "reason",
    "key_evidence",
    "qa_checked",
})
_FORBIDDEN_KEYS = frozenset({
    "full_text",
    "transcript",
    "content",
    "body",
    "text",
    "document",
    "raw_text",
    "path",
    "state_root",
    "file",
    "db",
    "api_key",
})
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
_DOC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_GENERATION = re.compile(r"^[a-f0-9]{32}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_DB_THREAD = threading.Lock()
_BATCH_GUARD = threading.Lock()
_BATCH_THREADS: dict[str, threading.Lock] = {}
_MESSAGES = {
    "not_configured": "AI-finance signals are not configured.",
    "config_invalid": "AI-finance config is invalid.",
    "interpreter_not_configured": "AI-finance signal interpreter is not configured.",
    "invalid_request": "The request is not valid.",
    "unknown_field": "Request keys are not allowed.",
    "invalid_action": "action must be save, compare, ingest, stats, or read.",
    "invalid_batch": "batch_id must be a real YYYY-MM-DD business date.",
    "invalid_owner": "owner_user_id must be a canonical UUID.",
    "invalid_identity": "Server run identity is not a canonical UUID.",
    "invalid_automation_execution": "automation_execution_id must be a canonical UUID.",
    "profile_mismatch": "This batch profile is not ai-finance-daily.",
    "owner_mismatch": "This batch belongs to another owner.",
    "not_found": "The prepared batch is not available.",
    "batch_not_ready": "The prepared batch is not complete.",
    "hash_mismatch": "The prepared batch failed the hash check.",
    "clock_inconsistent": "The saved batch clock could not be read.",
    "bundle_invalid": "The signal bundle does not match the prepared batch.",
    "bundle_frozen": "This bundle is frozen because ingest has started.",
    "bundle_missing": "Save a signal bundle before this action.",
    "compare_required": "Compare this bundle before ingest.",
    "compare_unavailable": "The saved comparison cannot be read.",
    "commit_inconsistent": "The saved signal bundle could not be read.",
    "signal_failed": "The signal operation did not finish.",
    "signal_timeout": "The signal operation did not finish before the timeout.",
    "offset_past_end": "offset is past the end of the comparison.",
    "invalid_offset": "offset must be a nonnegative integer.",
    "invalid_max_chars": "max_chars must be an integer from 1 to 32000.",
    "response_exceeds_bound": "The page does not fit in the response bound.",
    "path_escape": "The signal path is not inside this state root.",
}


def operation_timeout_seconds(action: str) -> float:
    """Outer wait. It is longer than the child timeout so the child is reaped first."""
    if action in {"compare", "ingest"}:
        return SIGNAL_SUBPROCESS_TIMEOUT_SECONDS + 15.0
    if action == "stats":
        return STATS_SUBPROCESS_TIMEOUT_SECONDS + 15.0
    return 60.0


def signal_script_path() -> Path:
    """Absolute path of the signal-memory CLI next to this file."""
    return Path(__file__).resolve().parent / "ai_finance" / "ai_finance_signal_memory.py"


def build_signal_subprocess_env(
    base_env: Mapping[str, str],
    *,
    api_key: str | None,
    embedding_model: str,
    embedding_dim: int,
    repo_root: Path,
) -> dict[str, str]:
    """Child environment for the signal CLI.

    The model and dimension are the operator pair. ``GEMINI_API_KEY`` is the
    only secret copied, and only when the bridge already resolved it.
    """
    if embedding_model != EMBEDDING_MODEL or embedding_dim != EMBEDDING_DIM:
        raise ValueError("embedding settings are not the configured pair")
    if not isinstance(repo_root, Path):
        raise ValueError("repo_root is not a path")
    extra = {
        "AI_SIGNAL_EMBEDDING_MODEL": embedding_model,
        "AI_SIGNAL_EMBEDDING_DIM": str(embedding_dim),
    }
    allow: tuple[str, ...] = ()
    if api_key is not None:
        if not isinstance(api_key, str) or not api_key.strip() or "\n" in api_key or "\r" in api_key:
            raise ValueError("api key is not usable")
        extra["GEMINI_API_KEY"] = api_key.strip()
        allow = ("GEMINI_API_KEY",)
    env = build_nonarchive_subprocess_env(
        base_env,
        extra_env=extra,
        allow_keys=allow,
        repo_root=repo_root,
    )
    if env.get("AI_SIGNAL_EMBEDDING_MODEL") != embedding_model:
        raise ValueError("embedding model was not kept")
    if env.get("AI_SIGNAL_EMBEDDING_DIM") != str(embedding_dim):
        raise ValueError("embedding dimension was not kept")
    return env


def kill_owned_group(pid: int | None) -> None:
    """Stop a process group this module started. Other groups are left alone."""
    if not isinstance(pid, int) or pid <= 1:
        return
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    except OSError:
        return
    if pgid != pid or pgid <= 1 or pgid == os.getpgrp() or pgid == os.getpid():
        return
    for signum in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, signum)
        except OSError:
            return


def parse_signal_request(body: object) -> dict[str, Any]:
    """Validate one internal signals body. Business JSON is accepted only for save."""
    if not isinstance(body, Mapping):
        return _error("invalid_request")
    action = body.get("action")
    if action not in _ACTIONS:
        if set(body) - (_SAVE_KEYS | _READ_KEYS):
            return _error("unknown_field")
        return _error("invalid_action")
    if action == "save":
        allowed = _SAVE_KEYS
    elif action == "read":
        allowed = _READ_KEYS
    else:
        allowed = _COMMON_KEYS
    if set(body) - allowed:
        return _error("unknown_field")
    if action == "save" and set(body) != _SAVE_KEYS:
        return _error("invalid_request")
    if not _COMMON_KEYS <= set(body):
        return _error("invalid_request")
    if body.get("profile") != PROFILE:
        return _error("profile_mismatch")
    batch_id = _iso_date(body.get("batch_id"))
    if batch_id is None:
        return _error("invalid_batch")
    execution_id = _canonical_uuid(body.get("automation_execution_id"))
    owner = _canonical_uuid(body.get("owner_user_id"))
    user_id = _canonical_uuid(body.get("user_id"))
    workspace_id = _canonical_uuid(body.get("workspace_id"))
    thread_id = _canonical_uuid(body.get("thread_id"))
    if execution_id is None:
        return _error("invalid_automation_execution")
    if owner is None:
        return _error("invalid_owner")
    if user_id is None or workspace_id is None or thread_id is None:
        return _error("invalid_identity")
    parsed: dict[str, Any] = {
        "ok": True,
        "action": action,
        "batch_id": batch_id,
        "execution_id": execution_id,
        "owner_user_id": owner,
        "user_id": user_id,
        "workspace_id": workspace_id,
        "thread_id": thread_id,
        "profile": PROFILE,
    }
    if action == "save":
        for key in ("github_decisions", "earnings_dispositions", "signal_records"):
            value = body.get(key)
            if not isinstance(value, dict):
                return _error("bundle_invalid", reason="bundle_invalid")
            parsed[key] = value
        return parsed
    if action == "read":
        offset = body.get("offset", 0)
        max_chars = body.get("max_chars", DEFAULT_MAX_CHARS)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or offset > _SOURCE_MAX_BYTES:
            return _error("invalid_offset")
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= MAX_CHARS:
            return _error("invalid_max_chars")
        parsed["offset"] = offset
        parsed["max_chars"] = max_chars
    return parsed


def execute_signal(
    parsed: Mapping[str, Any],
    *,
    state_root: Path,
    python_executable: Path | None,
    embedding_model: str,
    embedding_dim: int,
    child_env: Mapping[str, str] | None,
    command: Callable[..., ChildResult] = run_json_command,
    on_spawn: Callable[[int], None] | None = None,
    cancelled: threading.Event | None = None,
) -> dict[str, Any]:
    """Run one signal action inside the batch lock, then the shared database lock.

    Lock order is the per-date signal lock, then ``ai-signal-memory.lock``.
    The GitHub observation lock is not taken. Save and read stop after the
    date lock. Compare, ingest, and stats also hold the database lock for the
    whole child process.
    """
    if parsed.get("ok") is not True or parsed.get("action") not in _ACTIONS:
        return _error("invalid_request")
    if embedding_model != EMBEDDING_MODEL or embedding_dim != EMBEDDING_DIM:
        return _error("config_invalid")
    root = _require_root(state_root)
    if isinstance(root, dict):
        return root
    action = str(parsed["action"])
    batch_id = str(parsed["batch_id"])
    locks = root / "locks"
    if locks.exists() and _real_dir(locks) is not True:
        return _error("path_escape")
    preview = _load_prepared(root, batch_id, str(parsed["owner_user_id"]))
    if preview.get("ok") is not True:
        return preview
    if action in {"compare", "ingest", "stats"}:
        if not isinstance(python_executable, Path) or not python_executable.is_absolute():
            return _error("interpreter_not_configured")
        if not isinstance(child_env, Mapping):
            return _error("config_invalid")
        if child_env.get("AI_SIGNAL_EMBEDDING_MODEL") != embedding_model:
            return _error("config_invalid")
        if child_env.get("AI_SIGNAL_EMBEDDING_DIM") != str(embedding_dim):
            return _error("config_invalid")
    with _batch_lock(root, batch_id):
        if _cancelled(cancelled):
            return _error("signal_timeout")
        prepared = _load_prepared(root, batch_id, str(parsed["owner_user_id"]))
        if prepared.get("ok") is not True:
            return prepared
        if action == "save":
            return _save(root, parsed, prepared, embedding_model, embedding_dim, cancelled)
        if action == "read":
            return _read(root, parsed, prepared)
        with _db_lock(root):
            if _cancelled(cancelled):
                return _error("signal_timeout")
            if action == "compare":
                return _compare(
                    root, parsed, prepared, python_executable, embedding_model,
                    embedding_dim, child_env or {}, command, on_spawn, cancelled,
                )
            if action == "ingest":
                return _ingest(
                    root, parsed, prepared, python_executable, embedding_model,
                    embedding_dim, child_env or {}, command, on_spawn, cancelled,
                )
            return _stats(
                root, parsed, prepared, python_executable, embedding_model,
                embedding_dim, child_env or {}, command, on_spawn, cancelled,
            )


def _save(
    root: Path,
    parsed: Mapping[str, Any],
    prepared: Mapping[str, Any],
    embedding_model: str,
    embedding_dim: int,
    cancelled: threading.Event | None,
) -> dict[str, Any]:
    checked = _check_bundle(
        parsed["github_decisions"],
        parsed["earnings_dispositions"],
        parsed["signal_records"],
        prepared,
    )
    if checked.get("ok") is not True:
        return checked
    digest = str(checked["bundle_hash"])
    current = _read_commit(root, str(parsed["batch_id"]))
    if isinstance(current, dict) and current.get("ok") is False:
        return current
    if isinstance(current, dict):
        if current["manifest_sha256"] != prepared["manifest_sha256"]:
            return _error("hash_mismatch", batch_id=parsed["batch_id"])
        if current["owner_user_id"] != parsed["owner_user_id"] or current["profile"] != PROFILE:
            return _error("owner_mismatch", batch_id=parsed["batch_id"])
        if current["status"] in {"ingesting", "ingested"} and current["bundle_hash"] != digest:
            return _error("bundle_frozen", batch_id=parsed["batch_id"], status=current["status"])
        if current["bundle_hash"] == digest and _files_match(root, current):
            return _save_summary(parsed, current, reused=True)
        if current["status"] != "open":
            return _error("commit_inconsistent", batch_id=parsed["batch_id"], status=current["status"])
    if _cancelled(cancelled):
        return _error("signal_timeout")
    generation = uuid.uuid4().hex
    directory = _generation_dir(root, str(parsed["batch_id"]), generation)
    if isinstance(directory, dict):
        return directory
    _atomic_write(directory / "github_decisions.json", checked["github_bytes"])
    _atomic_write(directory / "earnings_dispositions.json", checked["earnings_bytes"])
    _atomic_write(directory / "signal_records.json", checked["signal_bytes"])
    document = {
        "schema": 1,
        "batch_id": parsed["batch_id"],
        "owner_user_id": parsed["owner_user_id"],
        "profile": PROFILE,
        "manifest_sha256": prepared["manifest_sha256"],
        "bundle_hash": digest,
        "generation": generation,
        "status": "open",
        "embedding_model": embedding_model,
        "embedding_dim": embedding_dim,
        "compare_sha256": None,
        "outcome_sha256": None,
    }
    _atomic_write(_commit_path(root, str(parsed["batch_id"])), _canonical(document))
    return _save_summary(parsed, document, reused=False, counts=checked["counts"])


def _compare(
    root: Path,
    parsed: Mapping[str, Any],
    prepared: Mapping[str, Any],
    python_executable: Path | None,
    embedding_model: str,
    embedding_dim: int,
    child_env: Mapping[str, str],
    command: Callable[..., ChildResult],
    on_spawn: Callable[[int], None] | None,
    cancelled: threading.Event | None,
) -> dict[str, Any]:
    current = _require_bundle(root, parsed, prepared)
    if current.get("ok") is False:
        return current
    existing_raw = _compare_bytes(root, current)
    if existing_raw is not None:
        existing = _parse_compare(existing_raw)
        if existing.get("ok") is not True:
            return _error("compare_unavailable", batch_id=parsed["batch_id"], status=current["status"])
        return _compare_summary(parsed, current, existing["result"], reused=True, raw=existing_raw)
    if current["status"] in {"ingesting", "ingested"}:
        return _error("compare_unavailable", batch_id=parsed["batch_id"], status=current["status"])
    if _cancelled(cancelled):
        return _error("signal_timeout")
    directory = _existing_generation(root, current)
    if isinstance(directory, dict):
        return directory
    output = directory / f".compare-out-{uuid.uuid4().hex}"
    argv = _argv(
        python_executable,
        "--db",
        str(_db_path(root)),
        "compare",
        "--input",
        str(directory / "signal_records.json"),
        "--output",
        str(output),
        "--limit",
        str(COMPARE_LIMIT),
    )
    if isinstance(argv, dict):
        return argv
    child = _run_child(command, argv, child_env, SIGNAL_SUBPROCESS_TIMEOUT_SECONDS, on_spawn)
    raw = _take_output(output)
    if child.get("ok") is not True:
        return child
    if isinstance(raw, dict):
        return raw
    parsed_result = _parse_compare(raw)
    if parsed_result.get("ok") is not True:
        return parsed_result
    if _cancelled(cancelled):
        return _error("signal_timeout")
    _atomic_write(directory / "compare_result.json", raw)
    digest = hashlib.sha256(raw).hexdigest()
    updated = dict(current)
    updated["compare_sha256"] = digest
    if not _replace_commit(root, current, updated):
        return _error("commit_inconsistent", batch_id=parsed["batch_id"])
    return _compare_summary(parsed, updated, parsed_result["result"], reused=False, raw=raw)


def _ingest(
    root: Path,
    parsed: Mapping[str, Any],
    prepared: Mapping[str, Any],
    python_executable: Path | None,
    embedding_model: str,
    embedding_dim: int,
    child_env: Mapping[str, str],
    command: Callable[..., ChildResult],
    on_spawn: Callable[[int], None] | None,
    cancelled: threading.Event | None,
) -> dict[str, Any]:
    current = _require_bundle(root, parsed, prepared)
    if current.get("ok") is False:
        return current
    if _load_compare(root, current) is None:
        return _error("compare_required", batch_id=parsed["batch_id"], status=current["status"])
    compare_bytes = _compare_bytes(root, current)
    saved = _load_outcome(root, current)
    if (
        current["status"] == "ingested"
        and saved is not None
        and not _outcome_has_embedding_errors(saved)
    ):
        return _ingest_summary(parsed, current, saved, reused=True)
    if (
        current["status"] == "ingesting"
        and saved is not None
        and _adopt_written_outcome(current, saved)
    ):
        if _compare_bytes(root, current) != compare_bytes:
            return _error("compare_unavailable", batch_id=parsed["batch_id"])
        updated = dict(current)
        updated["status"] = "ingested"
        updated["outcome_sha256"] = saved["file_sha256"]
        if not _replace_commit(root, current, updated):
            return _error("commit_inconsistent", batch_id=parsed["batch_id"])
        return _ingest_summary(parsed, updated, saved, reused=True)
    if (
        current["status"] == "ingested"
        and saved is not None
        and _outcome_has_embedding_errors(saved)
    ):
        if _cancelled(cancelled):
            return _error("signal_timeout")
        cleared = dict(current)
        cleared["status"] = "ingesting"
        cleared["outcome_sha256"] = None
        if not _replace_commit(root, current, cleared):
            return _error("commit_inconsistent", batch_id=parsed["batch_id"])
        reread = _read_commit(root, str(parsed["batch_id"]))
        if (
            not isinstance(reread, dict)
            or reread.get("status") != "ingesting"
            or reread.get("outcome_sha256") is not None
            or reread.get("bundle_hash") != current["bundle_hash"]
            or reread.get("compare_sha256") != current["compare_sha256"]
            or reread.get("generation") != current["generation"]
        ):
            return _error("commit_inconsistent", batch_id=parsed["batch_id"])
        current = reread
    elif current["status"] == "open":
        frozen = dict(current)
        frozen["status"] = "ingesting"
        if _cancelled(cancelled):
            return _error("signal_timeout")
        if not _replace_commit(root, current, frozen):
            return _error("commit_inconsistent", batch_id=parsed["batch_id"])
        reread = _read_commit(root, str(parsed["batch_id"]))
        if not isinstance(reread, dict) or reread.get("status") != "ingesting":
            return _error("commit_inconsistent", batch_id=parsed["batch_id"])
        current = reread
    if current["status"] != "ingesting":
        return _error("commit_inconsistent", batch_id=parsed["batch_id"], status=current["status"])
    if _cancelled(cancelled):
        return _error("signal_timeout")
    directory = _existing_generation(root, current)
    if isinstance(directory, dict):
        return directory
    signal_input = str(directory / "signal_records.json")
    db_path = str(_db_path(root))
    ingest_argv = _argv(python_executable, "--db", db_path, "ingest", "--input", signal_input)
    if isinstance(ingest_argv, dict):
        return ingest_argv
    ingest_child = _run_child(
        command, ingest_argv, child_env, SIGNAL_SUBPROCESS_TIMEOUT_SECONDS, on_spawn,
    )
    if ingest_child.get("ok") is not True:
        return ingest_child
    ingest_body = _parse_ingest(ingest_child.get("stdout"))
    if ingest_body.get("ok") is not True:
        return ingest_body
    if _cancelled(cancelled):
        return _error("signal_timeout")
    stats_argv = _argv(python_executable, "--db", db_path, "stats")
    if isinstance(stats_argv, dict):
        return stats_argv
    stats_child = _run_child(
        command, stats_argv, child_env, STATS_SUBPROCESS_TIMEOUT_SECONDS, on_spawn,
    )
    if stats_child.get("ok") is not True:
        return stats_child
    stats_body = _parse_stats(stats_child.get("stdout"))
    if stats_body.get("ok") is not True:
        return stats_body
    if _cancelled(cancelled):
        return _error("signal_timeout")
    if _compare_bytes(root, current) != compare_bytes:
        return _error("compare_unavailable", batch_id=parsed["batch_id"])
    outcome = {
        "schema": 1,
        "bundle_hash": current["bundle_hash"],
        "embedding_model": embedding_model,
        "embedding_dim": embedding_dim,
        "ingest": ingest_body["ingest"],
        "stats": stats_body["stats"],
    }
    raw = _canonical(outcome)
    _atomic_write(directory / "outcome.json", raw)
    updated = dict(current)
    updated["status"] = "ingested"
    updated["outcome_sha256"] = hashlib.sha256(raw).hexdigest()
    if not _replace_commit(root, current, updated):
        return _error("commit_inconsistent", batch_id=parsed["batch_id"])
    return _ingest_summary(parsed, updated, outcome, reused=False)


def _stats(
    root: Path,
    parsed: Mapping[str, Any],
    prepared: Mapping[str, Any],
    python_executable: Path | None,
    embedding_model: str,
    embedding_dim: int,
    child_env: Mapping[str, str],
    command: Callable[..., ChildResult],
    on_spawn: Callable[[int], None] | None,
    cancelled: threading.Event | None,
) -> dict[str, Any]:
    del embedding_model, embedding_dim
    current = _require_bundle(root, parsed, prepared)
    if current.get("ok") is False:
        return current
    saved = _load_outcome(root, current)
    if current["status"] == "ingested" and saved is not None:
        return _stats_summary(parsed, current, saved["stats"], reused=True)
    if _cancelled(cancelled):
        return _error("signal_timeout")
    argv = _argv(python_executable, "--db", str(_db_path(root)), "stats")
    if isinstance(argv, dict):
        return argv
    child = _run_child(command, argv, child_env, STATS_SUBPROCESS_TIMEOUT_SECONDS, on_spawn)
    if child.get("ok") is not True:
        return child
    stats_body = _parse_stats(child.get("stdout"))
    if stats_body.get("ok") is not True:
        return stats_body
    return _stats_summary(parsed, current, stats_body["stats"], reused=False)


def _read(root: Path, parsed: Mapping[str, Any], prepared: Mapping[str, Any]) -> dict[str, Any]:
    current = _require_bundle(root, parsed, prepared)
    if current.get("ok") is False:
        return current
    raw = _compare_bytes(root, current)
    if raw is None:
        return _error("compare_unavailable", batch_id=parsed["batch_id"], status=current["status"])
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return _error("compare_unavailable", batch_id=parsed["batch_id"])
    digest = hashlib.sha256(raw).hexdigest()
    return _page(
        text,
        digest,
        batch_id=str(parsed["batch_id"]),
        bundle_hash=str(current["bundle_hash"]),
        offset=int(parsed["offset"]),
        max_chars=int(parsed["max_chars"]),
    )


def _check_bundle(
    github: Mapping[str, Any],
    earnings: Mapping[str, Any],
    signals: Mapping[str, Any],
    prepared: Mapping[str, Any],
) -> dict[str, Any]:
    github_records = _records(github, "github_decisions")
    earnings_records = _records(earnings, "earnings_dispositions")
    signal_records = _records(signals, "signal_records")
    if isinstance(github_records, dict):
        return github_records
    if isinstance(earnings_records, dict):
        return earnings_records
    if isinstance(signal_records, dict):
        return signal_records
    disposition_error = _check_dispositions(earnings_records)
    if disposition_error is not None:
        return disposition_error
    decision_error = _check_decisions(github_records, prepared["github_candidates"])
    if decision_error is not None:
        return decision_error
    field_error = _check_signal_fields(signal_records, github_records, prepared["github_candidates"])
    if field_error is not None:
        return field_error
    github_bytes = _canonical({"records": github_records})
    earnings_bytes = _canonical({"records": earnings_records})
    signal_bytes = _canonical({"records": signal_records})
    for blob in (github_bytes, earnings_bytes, signal_bytes):
        if len(blob) > _OBJECT_MAX_BYTES:
            return _error("bundle_invalid", reason="bundle_oversize")
    with tempfile.TemporaryDirectory(prefix="ai-finance-signals-") as temporary:
        root = Path(temporary)
        scan = root / "earnings_scan.json"
        dispositions = root / "earnings_dispositions.json"
        signal_path = root / "signal_records.json"
        decisions = root / "github_decisions.json"
        scan.write_bytes(prepared["earnings_bytes"])
        dispositions.write_bytes(earnings_bytes)
        signal_path.write_bytes(signal_bytes)
        decisions.write_bytes(github_bytes)
        violations: list[str] = []
        validate_records(scan, dispositions, signal_path, violations, decisions)
    if violations:
        return _error("bundle_invalid", reason=_reason(violations))
    return {
        "ok": True,
        "bundle_hash": _bundle_hash(github_bytes, earnings_bytes, signal_bytes),
        "github_bytes": github_bytes,
        "earnings_bytes": earnings_bytes,
        "signal_bytes": signal_bytes,
        "counts": {
            "github_decisions": len(github_records),
            "earnings_dispositions": len(earnings_records),
            "signal_records": len(signal_records),
        },
    }


def _check_dispositions(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Validate one disposition and its structured metadata per scanned transcript."""
    if len(records) > _EARNINGS_SCAN_MAX:
        return _error("bundle_invalid", reason="bundle_oversize")
    for record in records:
        if _contains_forbidden_key(record):
            return _error("bundle_invalid", reason="transcript_rejected")
        if set(record) - _DISPOSITION_KEYS or not {"doc_id", "disposition"} <= set(record):
            return _error("bundle_invalid", reason="bundle_invalid")
        doc_id = record.get("doc_id")
        if (
            not isinstance(doc_id, str)
            or not doc_id.strip()
            or len(doc_id) > 128
            or doc_id != doc_id.strip()
            or ".." in doc_id
            or "/" in doc_id
            or "\\" in doc_id
            or "\x00" in doc_id
        ):
            return _error("bundle_invalid", reason="bundle_invalid")
        if record.get("disposition") not in {"signal", "watchlist", "reject"}:
            return _error("bundle_invalid", reason="bundle_invalid")
        if "signal_direction" in record and record.get("signal_direction") not in _DIRECTIONS:
            return _error("bundle_invalid", reason="bundle_invalid")
        if "qa_checked" in record and type(record.get("qa_checked")) is not bool:
            return _error("bundle_invalid", reason="bundle_invalid")
        for key in ("entity", "evidence_status", "reason", "key_evidence"):
            if key not in record:
                continue
            value = record.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > _MAX_STRING or "\x00" in value:
                return _error("bundle_invalid", reason="bundle_invalid")
    return None


def _check_decisions(
    records: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    by_name: dict[str, dict[str, Any]] = {}
    for item in candidates:
        name = item.get("full_name")
        if not isinstance(name, str) or _REPO.fullmatch(name) is None:
            return _error("hash_mismatch")
        key = name.lower()
        if key in by_name:
            return _error("bundle_invalid", reason="github_duplicate_candidate")
        by_name[key] = item
    seen: set[str] = set()
    required = {"repo", "decision", "reason", "readme_url"}
    for record in records:
        if _contains_forbidden_key(record):
            return _error("bundle_invalid", reason="transcript_rejected")
        if set(record) - _DECISION_KEYS or not required <= set(record):
            return _error("bundle_invalid", reason="github_decision_invalid")
        repo = record.get("repo")
        if not isinstance(repo, str) or _REPO.fullmatch(repo) is None:
            return _error("bundle_invalid", reason="github_unknown_repo")
        key = repo.lower()
        if key in seen:
            return _error("bundle_invalid", reason="github_duplicate_review")
        seen.add(key)
        candidate = by_name.get(key)
        if candidate is None:
            return _error("bundle_invalid", reason="github_unknown_repo")
        if record.get("decision") not in {"include", "exclude"}:
            return _error("bundle_invalid", reason="github_decision_invalid")
        if not isinstance(record.get("reason"), str) or not record["reason"].strip():
            return _error("bundle_invalid", reason="github_decision_invalid")
        if not _https_url(record.get("readme_url")):
            return _error("bundle_invalid", reason="github_decision_invalid")
        if "displayed" in record and type(record.get("displayed")) is not bool:
            return _error("bundle_invalid", reason="github_decision_invalid")
        for field in ("upstream", "upstream_evidence"):
            if field not in record:
                continue
            value = record.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > _MAX_STRING or "\x00" in value:
                return _error("bundle_invalid", reason="github_decision_invalid")
        if record["decision"] == "include" and not _eligible(candidate):
            return _error("bundle_invalid", reason="github_not_eligible")
    if seen != set(by_name):
        return _error("bundle_invalid", reason="github_review_incomplete")
    return None


def _check_signal_fields(
    records: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    included = {
        str(record["repo"]).lower()
        for record in decisions
        if record.get("decision") == "include"
    }
    eligible = {
        str(item["full_name"]).lower()
        for item in candidates
        if _eligible(item)
    }
    if len(records) > _MAX_RECORDS:
        return _error("bundle_invalid", reason="bundle_oversize")
    earnings_count = sum(1 for record in records if record.get("source_type") == "earnings")
    if earnings_count > _EARNINGS_SCAN_MAX:
        return _error("bundle_invalid", reason="bundle_oversize")
    for record in records:
        if _contains_forbidden_key(record):
            return _error("bundle_invalid", reason="transcript_rejected")
        if set(record) - _SIGNAL_KEYS:
            return _error("bundle_invalid", reason="signal_fields_invalid")
        if record.get("source_type") not in {"github", "earnings"}:
            return _error("bundle_invalid", reason="signal_fields_invalid")
        if record.get("signal_direction") not in _DIRECTIONS:
            return _error("bundle_invalid", reason="signal_fields_invalid")
        if not isinstance(record.get("investment_implication"), str) or not record["investment_implication"].strip():
            return _error("bundle_invalid", reason="signal_fields_invalid")
        if not isinstance(record.get("tam_mapping"), str) or not record["tam_mapping"].strip():
            return _error("bundle_invalid", reason="signal_fields_invalid")
        if not isinstance(record.get("summary"), str) or not record["summary"].strip():
            return _error("bundle_invalid", reason="signal_fields_invalid")
        if not isinstance(record.get("external_id"), str) or not record["external_id"].strip():
            return _error("bundle_invalid", reason="signal_fields_invalid")
        event_time = _parse_instant(record.get("event_time"))
        if event_time is None:
            return _error("bundle_invalid", reason="signal_fields_invalid")
        if "url" in record and record["url"] not in ("", None) and not _https_url(record.get("url")):
            return _error("bundle_invalid", reason="signal_fields_invalid")
        if record["source_type"] == "earnings":
            concern = record.get("qa_concern")
            response = record.get("management_response")
            status = record.get("management_response_status")
            if not isinstance(concern, str) or not isinstance(response, str) or not isinstance(status, str):
                return _error("bundle_invalid", reason="signal_fields_invalid")
            if (
                len(concern) > _MAX_STRING
                or len(response) > _MAX_STRING
                or "\x00" in concern
                or "\x00" in response
            ):
                return _error("bundle_invalid", reason="signal_fields_invalid")
            if status == "":
                if concern != "" or response != "":
                    return _error("bundle_invalid", reason="signal_fields_invalid")
            elif status not in _RESPONSE_STATUS or not concern.strip():
                return _error("bundle_invalid", reason="signal_fields_invalid")
            elif status != "evasive_or_unanswered" and not response.strip():
                return _error("bundle_invalid", reason="signal_fields_invalid")
        if record["source_type"] == "github":
            key = record["external_id"].lower()
            if key not in included or key not in eligible:
                return _error("bundle_invalid", reason="github_not_eligible")
        try:
            canonical = canonical_record(dict(record))
        except (TypeError, ValueError):
            return _error("bundle_invalid", reason="signal_fields_invalid")
        external_id = record.get("external_id")
        if record["source_type"] == "earnings":
            if (
                not isinstance(external_id, str)
                or not external_id
                or external_id != external_id.strip()
                or len(external_id) > 128
                or ".." in external_id
                or "/" in external_id
                or "\\" in external_id
                or "\x00" in external_id
            ):
                return _error("bundle_invalid", reason="signal_fields_invalid")
            expected = f"earnings:{external_id}"
        else:
            if not isinstance(external_id, str) or _REPO.fullmatch(external_id) is None:
                return _error("bundle_invalid", reason="signal_fields_invalid")
            day = _iso_date(str(record.get("event_time"))[:10])
            if day is None:
                return _error("bundle_invalid", reason="signal_fields_invalid")
            expected = f"github:{external_id}:{day}"
        if record.get("record_key") != expected or canonical["record_key"] != expected:
            return _error("bundle_invalid", reason="record_key_invalid")
        if canonical["signal_direction"] not in _DIRECTIONS:
            return _error("bundle_invalid", reason="signal_fields_invalid")
    return None


def _records(value: Mapping[str, Any], label: str) -> list[dict[str, Any]] | dict[str, Any]:
    del label
    if set(value) != {"records"} or not isinstance(value.get("records"), list):
        return _error("bundle_invalid", reason="bundle_invalid")
    rows = value["records"]
    if len(rows) > _MAX_RECORDS:
        return _error("bundle_invalid", reason="bundle_oversize")
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not _json_safe(row):
            return _error("bundle_invalid", reason="bundle_invalid")
        if _contains_forbidden_key(row):
            return _error("bundle_invalid", reason="transcript_rejected")
        cleaned.append(row)
    return cleaned


def _load_prepared(root: Path, batch_id: str, owner_user_id: str) -> dict[str, Any]:
    clock = _read_clock(root / "batch_clocks" / f"{batch_id}.json", batch_id)
    if clock.get("ok") is not True:
        return clock
    if clock["owner_user_id"] != owner_user_id:
        return _error("owner_mismatch", batch_id=batch_id)
    if clock["profile"] != PROFILE:
        return _error("profile_mismatch", batch_id=batch_id)
    batch_dir = root / "batches" / batch_id
    if _real_dir(batch_dir) is False:
        return _error("not_found", batch_id=batch_id)
    metadata = _read_json(batch_dir / "metadata.json")
    if metadata is None or metadata.get("schema") != 1 or metadata.get("batch_id") != batch_id:
        return _error("hash_mismatch", batch_id=batch_id)
    manifest_sha = metadata.get("manifest_sha256")
    manifest_raw = _read_named(batch_dir, "manifest.json")
    if not isinstance(manifest_sha, str) or _HEX64.fullmatch(manifest_sha) is None or manifest_raw is None:
        return _error("hash_mismatch", batch_id=batch_id)
    if hashlib.sha256(manifest_raw).hexdigest() != manifest_sha:
        return _error("hash_mismatch", batch_id=batch_id)
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return _error("hash_mismatch", batch_id=batch_id)
    if not isinstance(manifest, dict) or manifest.get("batch_id") != batch_id:
        return _error("hash_mismatch", batch_id=batch_id)
    stages = metadata.get("stages")
    artifacts = manifest.get("artifacts")
    if not isinstance(stages, dict) or not isinstance(artifacts, dict):
        return _error("hash_mismatch", batch_id=batch_id)
    github_raw = _hashed_artifact(
        batch_dir, stages.get("github"), artifacts.get("github_candidates"), "github_candidates.json",
    )
    earnings_raw = _hashed_artifact(
        batch_dir, stages.get("earnings"), artifacts.get("earnings_scan"), "earnings_scan.json",
    )
    if isinstance(github_raw, dict):
        return github_raw
    if isinstance(earnings_raw, dict):
        return earnings_raw
    try:
        github = json.loads(github_raw.decode("utf-8"))
        earnings = json.loads(earnings_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return _error("hash_mismatch", batch_id=batch_id)
    if not isinstance(github, dict) or github.get("version") != 3 or not isinstance(github.get("candidates"), list):
        return _error("hash_mismatch", batch_id=batch_id)
    if not isinstance(earnings, dict):
        return _error("hash_mismatch", batch_id=batch_id)
    candidates = github["candidates"]
    if any(not isinstance(item, dict) for item in candidates):
        return _error("hash_mismatch", batch_id=batch_id)
    return {
        "ok": True,
        "manifest_sha256": manifest_sha,
        "github_candidates": candidates,
        "earnings_bytes": earnings_raw,
    }


def _hashed_artifact(
    directory: Path,
    stage: object,
    artifact: object,
    filename: str,
) -> bytes | dict[str, Any]:
    if not isinstance(stage, dict) or not isinstance(artifact, dict):
        return _error("hash_mismatch")
    if stage.get("status") != "complete" or stage.get("artifact") != filename:
        return _error("batch_not_ready")
    if artifact.get("path") != filename or artifact.get("sha256") != stage.get("sha256"):
        return _error("hash_mismatch")
    raw = _read_named(directory, filename)
    digest = stage.get("sha256")
    if raw is None or not isinstance(digest, str) or hashlib.sha256(raw).hexdigest() != digest:
        return _error("hash_mismatch")
    return raw


def _read_clock(path: Path, batch_id: str) -> dict[str, Any]:
    raw = _read_file(path, max_bytes=64 * 1024)
    if raw is None:
        return _error("not_found", batch_id=batch_id)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return _error("clock_inconsistent", batch_id=batch_id)
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("batch_id") != batch_id:
        return _error("clock_inconsistent", batch_id=batch_id)
    if data.get("profile") != PROFILE:
        return _error("clock_inconsistent", batch_id=batch_id)
    started = _parse_instant(data.get("started_at"))
    scheduled = _parse_instant(data.get("scheduled_at"))
    owner = _canonical_uuid(data.get("owner_user_id"))
    execution_id = _canonical_uuid(data.get("first_execution_id"))
    if started is None or scheduled is None or owner is None or execution_id is None:
        return _error("clock_inconsistent", batch_id=batch_id)
    if started.astimezone(HKT).date() != scheduled.astimezone(HKT).date():
        return _error("clock_inconsistent", batch_id=batch_id)
    if started.astimezone(HKT).date().isoformat() != batch_id:
        return _error("clock_inconsistent", batch_id=batch_id)
    return {"ok": True, "owner_user_id": owner, "profile": PROFILE}


def _require_bundle(
    root: Path,
    parsed: Mapping[str, Any],
    prepared: Mapping[str, Any],
) -> dict[str, Any]:
    current = _read_commit(root, str(parsed["batch_id"]))
    if current is None:
        return _error("bundle_missing", batch_id=parsed["batch_id"])
    if current.get("ok") is False:
        return current
    if current["manifest_sha256"] != prepared["manifest_sha256"]:
        return _error("hash_mismatch", batch_id=parsed["batch_id"])
    if current["owner_user_id"] != parsed["owner_user_id"] or current["profile"] != PROFILE:
        return _error("owner_mismatch", batch_id=parsed["batch_id"])
    if current["embedding_model"] != EMBEDDING_MODEL or current["embedding_dim"] != EMBEDDING_DIM:
        return _error("config_invalid", batch_id=parsed["batch_id"])
    return current


def _read_commit(root: Path, batch_id: str) -> dict[str, Any] | None:
    path = _commit_path(root, batch_id)
    raw = _read_file(path, max_bytes=64 * 1024)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return _error("commit_inconsistent", batch_id=batch_id)
    if not _commit_shape(data, batch_id):
        return _error("commit_inconsistent", batch_id=batch_id)
    data["_sha256"] = hashlib.sha256(raw).hexdigest()
    return data


def _commit_shape(data: object, batch_id: str) -> bool:
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("batch_id") != batch_id:
        return False
    if data.get("profile") != PROFILE or data.get("status") not in {"open", "ingesting", "ingested"}:
        return False
    if _canonical_uuid(data.get("owner_user_id")) is None:
        return False
    if not isinstance(data.get("generation"), str) or _GENERATION.fullmatch(data["generation"]) is None:
        return False
    for key in ("manifest_sha256", "bundle_hash"):
        if not isinstance(data.get(key), str) or _HEX64.fullmatch(data[key]) is None:
            return False
    for key in ("compare_sha256", "outcome_sha256"):
        value = data.get(key)
        if value is not None and (not isinstance(value, str) or _HEX64.fullmatch(value) is None):
            return False
    if data.get("embedding_model") != EMBEDDING_MODEL or data.get("embedding_dim") != EMBEDDING_DIM:
        return False
    return True


def _replace_commit(root: Path, current: Mapping[str, Any], updated: Mapping[str, Any]) -> bool:
    path = _commit_path(root, str(current["batch_id"]))
    raw = _read_file(path, max_bytes=64 * 1024)
    if raw is None or hashlib.sha256(raw).hexdigest() != current.get("_sha256"):
        return False
    document = {key: value for key, value in updated.items() if key != "_sha256"}
    _atomic_write(path, _canonical(document))
    refreshed = _read_commit(root, str(current["batch_id"]))
    if not isinstance(refreshed, dict) or refreshed.get("ok") is False:
        return False
    return refreshed["generation"] == document["generation"] and refreshed["status"] == document["status"]


def _load_compare(root: Path, current: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = _compare_bytes(root, current)
    if raw is None:
        return None
    parsed = _parse_compare(raw)
    if parsed.get("ok") is not True:
        return None
    result = parsed["result"]
    if not isinstance(result, dict):
        return None
    return result


def _compare_bytes(root: Path, current: Mapping[str, Any]) -> bytes | None:
    digest = current.get("compare_sha256")
    if not isinstance(digest, str):
        return None
    directory = _existing_generation(root, current)
    if isinstance(directory, dict):
        return None
    raw = _read_file(directory / "compare_result.json", max_bytes=_COMPARE_FILE_MAX)
    if raw is None or hashlib.sha256(raw).hexdigest() != digest:
        return None
    return raw


def _files_match(root: Path, current: Mapping[str, Any]) -> bool:
    directory = _existing_generation(root, current)
    if isinstance(directory, dict):
        return False
    github = _read_file(directory / "github_decisions.json", max_bytes=_OBJECT_MAX_BYTES)
    earnings = _read_file(directory / "earnings_dispositions.json", max_bytes=_OBJECT_MAX_BYTES)
    signals = _read_file(directory / "signal_records.json", max_bytes=_OBJECT_MAX_BYTES)
    if github is None or earnings is None or signals is None:
        return False
    return _bundle_hash(github, earnings, signals) == current["bundle_hash"]


def _outcome_has_embedding_errors(outcome: Mapping[str, Any]) -> bool:
    """Whether this saved outcome still lists its own embedding failures.

    Live database coverage is not an input. An empty list is a finished ingest.
    """
    ingest = outcome.get("ingest")
    if not isinstance(ingest, dict):
        return False
    errors = ingest.get("embedding_errors")
    return isinstance(errors, list) and bool(errors)


def _adopt_written_outcome(current: Mapping[str, Any], outcome: Mapping[str, Any]) -> bool:
    """Whether an ingesting commit should keep the outcome file already on disk.

    A hash that still names this file finishes the first ingest, including a
    file that lists embedding errors. After a retry clears that hash, only a
    file with an empty error list is adopted. An error list continues the retry.
    """
    if current.get("outcome_sha256") == outcome.get("file_sha256"):
        return True
    return not _outcome_has_embedding_errors(outcome)


def _load_outcome(root: Path, current: Mapping[str, Any]) -> dict[str, Any] | None:
    digest = current.get("outcome_sha256")
    directory = _existing_generation(root, current)
    if isinstance(directory, dict):
        return None
    raw = _read_file(directory / "outcome.json", max_bytes=_OBJECT_MAX_BYTES)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != 1:
        return None
    if data.get("bundle_hash") != current["bundle_hash"]:
        return None
    if data.get("embedding_model") != EMBEDDING_MODEL or data.get("embedding_dim") != EMBEDDING_DIM:
        return None
    ingest = data.get("ingest")
    stats = data.get("stats")
    if not isinstance(ingest, dict) or not isinstance(stats, dict):
        return None
    if _public_ingest(ingest) is None or _public_stats(stats) is None:
        return None
    file_sha = hashlib.sha256(raw).hexdigest()
    if isinstance(digest, str) and file_sha != digest:
        return None
    data["ingest"] = _public_ingest(ingest)
    data["stats"] = _public_stats(stats)
    data["file_sha256"] = file_sha
    return data


def _run_child(
    command: Callable[..., ChildResult],
    argv: list[str],
    child_env: Mapping[str, str],
    timeout: float,
    on_spawn: Callable[[int], None] | None,
) -> dict[str, Any]:
    try:
        result = command(
            argv,
            env={str(key): str(value) for key, value in child_env.items()},
            timeout_seconds=timeout,
            stdout_limit_bytes=_STDOUT_LIMIT,
            stderr_limit_bytes=_STDERR_LIMIT,
            on_spawn=on_spawn,
            input_bytes=None,
        )
    except Exception:
        return _error("signal_failed")
    if not isinstance(result, ChildResult):
        return _error("signal_failed")
    if result.timed_out:
        return _error("signal_timeout")
    if result.returncode != 0 or result.still_running or result.stdout_truncated or result.stderr_truncated:
        return _error("signal_failed")
    return {"ok": True, "stdout": result.stdout}


def _argv(python_executable: Path | None, *parts: str) -> list[str] | dict[str, Any]:
    script = signal_script_path()
    if (
        not isinstance(python_executable, Path)
        or not python_executable.is_absolute()
        or not python_executable.is_file()
        or not script.is_file()
        or script.is_symlink()
    ):
        return _error("interpreter_not_configured")
    argv = [str(python_executable), str(script), *parts]
    if any(not isinstance(item, str) or not item or "\x00" in item for item in argv):
        return _error("signal_failed")
    return argv


def _take_output(path: Path) -> bytes | dict[str, Any]:
    raw = _read_file(path, max_bytes=_COMPARE_FILE_MAX)
    with contextlib.suppress(OSError):
        if path.exists() and not path.is_symlink():
            path.unlink()
    if raw is None:
        return _error("signal_failed")
    return raw


def _parse_compare(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return _error("signal_failed")
    if not isinstance(data, dict):
        return _error("signal_failed")
    modes = data.get("search_modes")
    comparisons = data.get("comparisons")
    if not isinstance(modes, list) or any(item not in _SEARCH_MODES for item in modes):
        return _error("signal_failed")
    if data.get("history_embedding_status_before_ingest") not in _HISTORY_STATUS:
        return _error("signal_failed")
    for key in ("history_signals_before_ingest", "history_embedded_signals_before_ingest"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return _error("signal_failed")
    if not isinstance(comparisons, list):
        return _error("signal_failed")
    return {"ok": True, "result": data}


def _parse_ingest(raw: object) -> dict[str, Any]:
    try:
        data = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError:
        return _error("signal_failed")
    public = _public_ingest(data)
    if public is None:
        return _error("signal_failed")
    return {"ok": True, "ingest": public}


def _parse_stats(raw: object) -> dict[str, Any]:
    try:
        data = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError:
        return _error("signal_failed")
    public = _public_stats(data)
    if public is None:
        return _error("signal_failed")
    return {"ok": True, "stats": public}


def _public_ingest(data: object) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    inserted = data.get("inserted")
    updated = data.get("updated")
    errors = data.get("embedding_errors")
    if isinstance(inserted, bool) or not isinstance(inserted, int) or inserted < 0:
        return None
    if isinstance(updated, bool) or not isinstance(updated, int) or updated < 0:
        return None
    if not isinstance(errors, list) or any(not _safe_embedding_error(item) for item in errors):
        return None
    return {"inserted": inserted, "updated": updated, "embedding_errors": list(errors)}


def _public_stats(data: object) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    if data.get("configured_embedding_model") != EMBEDDING_MODEL:
        return None
    if data.get("configured_embedding_dim") != EMBEDDING_DIM:
        return None
    if data.get("embedding_status") not in _HISTORY_STATUS:
        return None
    signals = data.get("signals")
    embedded = data.get("embedded_signals")
    any_embedded = data.get("embedded_signals_any_model")
    coverage = data.get("embedding_coverage_pct")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (signals, embedded, any_embedded)):
        return None
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)) or not math.isfinite(float(coverage)):
        return None
    by_source = _count_map(data.get("signals_by_source"))
    by_direction = _count_map(data.get("signals_by_direction"))
    if by_source is None or by_direction is None:
        return None
    return {
        "signals": signals,
        "embedded_signals": embedded,
        "embedded_signals_any_model": any_embedded,
        "embedding_status": data["embedding_status"],
        "embedding_coverage_pct": float(coverage),
        "configured_embedding_model": EMBEDDING_MODEL,
        "configured_embedding_dim": EMBEDDING_DIM,
        "signals_by_source": by_source,
        "signals_by_direction": by_direction,
    }


def _count_map(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict) or len(value) > 16:
        return None
    cleaned: dict[str, int] = {}
    for key, count in value.items():
        if not isinstance(key, str) or _TOKEN.fullmatch(key) is None:
            return None
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return None
        cleaned[key] = count
    return cleaned


def _embedding_record_key(record: str) -> bool:
    """Business id the signal CLI prefixes onto an exception class.

    GitHub keys are ``github:{owner}/{repo}:{YYYY-MM-DD}``. Earnings keys are
    ``earnings:{external_id}``. Repo names may contain token, key, or secret.
    """
    if len(record) > 4000 or "\x00" in record or "\\" in record or "\n" in record or "\r" in record:
        return False
    if record.startswith("github:"):
        owner_repo, separator, day = record[len("github:"):].rpartition(":")
        return (
            separator == ":"
            and _REPO.fullmatch(owner_repo) is not None
            and _iso_date(day) is not None
        )
    if record.startswith("earnings:"):
        external_id = record[len("earnings:"):]
        return (
            bool(external_id)
            and external_id == external_id.strip()
            and len(external_id) <= 128
            and ".." not in external_id
            and "/" not in external_id
            and "\\" not in external_id
        )
    return False


def _safe_embedding_error(value: object) -> bool:
    """Accept only ``{record_key}: {ExceptionClass}`` from the signal CLI."""
    if not isinstance(value, str) or "\n" in value or "\r" in value or "\\" in value or "\x00" in value:
        return False
    record, separator, kind = value.rpartition(": ")
    if separator != ": " or kind not in _EMBEDDING_EXCEPTIONS:
        return False
    return _embedding_record_key(record)


def _save_summary(
    parsed: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    reused: bool,
    counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    summary = {
        "ok": True,
        "action": "save",
        "code": "ok",
        "batch_id": parsed["batch_id"],
        "profile": PROFILE,
        "bundle_hash": current["bundle_hash"],
        "manifest_sha256": current["manifest_sha256"],
        "status": current["status"],
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dim": EMBEDDING_DIM,
        "reused": reused,
    }
    if counts is not None:
        summary["record_counts"] = dict(counts)
    return summary


def _compare_summary(
    parsed: Mapping[str, Any],
    current: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    reused: bool,
    raw: bytes,
) -> dict[str, Any]:
    encoded = raw
    return {
        "ok": True,
        "action": "compare",
        "code": "ok",
        "batch_id": parsed["batch_id"],
        "profile": PROFILE,
        "bundle_hash": current["bundle_hash"],
        "manifest_sha256": current["manifest_sha256"],
        "status": current["status"],
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dim": EMBEDDING_DIM,
        "reused": reused,
        "search_modes": list(result["search_modes"]),
        "history_signals_before_ingest": result["history_signals_before_ingest"],
        "history_embedded_signals_before_ingest": result["history_embedded_signals_before_ingest"],
        "history_embedding_status_before_ingest": result["history_embedding_status_before_ingest"],
        "comparison_count": len(result["comparisons"]),
        "content_sha256": hashlib.sha256(encoded).hexdigest(),
        "total_chars": len(encoded.decode("utf-8")),
    }


def _ingest_summary(
    parsed: Mapping[str, Any],
    current: Mapping[str, Any],
    outcome: Mapping[str, Any],
    *,
    reused: bool,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "ok": True,
        "action": "ingest",
        "code": "ok",
        "batch_id": parsed["batch_id"],
        "profile": PROFILE,
        "bundle_hash": current["bundle_hash"],
        "manifest_sha256": current["manifest_sha256"],
        "status": current["status"],
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dim": EMBEDDING_DIM,
        "reused": reused,
    }
    summary.update(outcome["ingest"])
    summary.update({
        "signals": outcome["stats"]["signals"],
        "embedded_signals": outcome["stats"]["embedded_signals"],
        "embedding_status": outcome["stats"]["embedding_status"],
        "embedding_coverage_pct": outcome["stats"]["embedding_coverage_pct"],
        "configured_embedding_model": EMBEDDING_MODEL,
        "configured_embedding_dim": EMBEDDING_DIM,
    })
    return summary


def _stats_summary(
    parsed: Mapping[str, Any],
    current: Mapping[str, Any],
    stats: Mapping[str, Any],
    *,
    reused: bool,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "ok": True,
        "action": "stats",
        "code": "ok",
        "batch_id": parsed["batch_id"],
        "profile": PROFILE,
        "bundle_hash": current["bundle_hash"],
        "status": current["status"],
        "reused": reused,
    }
    summary.update(dict(stats))
    return summary


def _page(
    text: str,
    digest: str,
    *,
    batch_id: str,
    bundle_hash: str,
    offset: int,
    max_chars: int,
) -> dict[str, Any]:
    total = len(text)
    shell: dict[str, Any] = {
        "ok": True,
        "action": "read",
        "code": "ok",
        "batch_id": batch_id,
        "profile": PROFILE,
        "bundle_hash": bundle_hash,
        "offset": offset,
        "max_chars": max_chars,
        "total_chars": total,
        "content_sha256": digest,
    }
    if offset > total:
        return _error(
            "offset_past_end",
            batch_id=batch_id,
            offset=offset,
            total_chars=total,
            content_sha256=digest,
        )
    window = min(max_chars, total - offset)
    lo = 0
    hi = window
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if len(_encode(_slice(shell, text, offset, mid, total))) <= MAX_RESPONSE_BYTES:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if window > 0 and best == 0:
        return _error("response_exceeds_bound", batch_id=batch_id)
    return _slice(shell, text, offset, best, total)


def _slice(shell: Mapping[str, Any], full: str, offset: int, count: int, total: int) -> dict[str, Any]:
    end = offset + count
    page = dict(shell)
    page["text"] = full[offset:end]
    page["next_offset"] = None if end >= total else end
    page["complete"] = end >= total
    return page


def _reason(violations: list[str]) -> str:
    text = "\n".join(violations)
    if "one-to-one" in text or "disposition count" in text or "ai_candidates" in text:
        return "disposition_mismatch"
    if "lacks an include" in text or "require decisions" in text:
        return "github_signal_not_included"
    if "watchlist or reject" in text:
        return "blocked_signal"
    if "duplicate" in text:
        return "duplicate_record"
    if "source_type" in text or "signal record" in text:
        return "signal_fields_invalid"
    return "bundle_invalid"


def _bundle_hash(github: bytes, earnings: bytes, signals: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(b"github_decisions\n")
    digest.update(github)
    digest.update(b"\nearnings_dispositions\n")
    digest.update(earnings)
    digest.update(b"\nsignal_records\n")
    digest.update(signals)
    return digest.hexdigest()


def _contains_forbidden_key(value: object) -> bool:
    """Reject a transcript or path hiding under any nested field name."""
    if isinstance(value, dict):
        if _FORBIDDEN_KEYS & set(value):
            return True
        return any(_contains_forbidden_key(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _eligible(candidate: Mapping[str, Any]) -> bool:
    return candidate.get("history_verified") is True and candidate.get("report_eligible") is True


def _https_url(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("https://") or len(value) > 500:
        return False
    if any(mark in value for mark in ("\n", "\r", "\x00", "@", "\\")):
        return False
    return True


def _json_safe(value: object, depth: int = 0) -> bool:
    if depth > _MAX_DEPTH:
        return False
    if value is None or type(value) is bool:
        return True
    if type(value) is int:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if isinstance(value, str):
        return len(value) <= _MAX_STRING and "\x00" not in value
    if isinstance(value, list):
        return len(value) <= _MAX_RECORDS and all(_json_safe(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return (
            len(value) <= 40
            and all(isinstance(key, str) and len(key) <= 80 and _json_safe(item, depth + 1) for key, item in value.items())
        )
    return False


def _generation_dir(root: Path, batch_id: str, generation: str) -> Path | dict[str, Any]:
    base = root / "signals" / batch_id
    if not _ensure_real_dir(base):
        return _error("path_escape", batch_id=batch_id)
    parent = base / "g"
    if not _ensure_real_dir(parent):
        return _error("path_escape", batch_id=batch_id)
    directory = parent / generation
    if directory.exists():
        return _error("signal_failed", batch_id=batch_id)
    directory.mkdir()
    return directory


def _existing_generation(root: Path, current: Mapping[str, Any]) -> Path | dict[str, Any]:
    directory = root / "signals" / str(current["batch_id"]) / "g" / str(current["generation"])
    if _real_dir(directory) is not True or directory.parent.parent.name != str(current["batch_id"]):
        return _error("commit_inconsistent", batch_id=current["batch_id"])
    return directory


def _commit_path(root: Path, batch_id: str) -> Path:
    return root / "signals" / batch_id / "commit.json"


def _db_path(root: Path) -> Path:
    return root / "ai_signal_memory.sqlite"


def _ensure_real_dir(path: Path) -> bool:
    if path.exists():
        return _real_dir(path) is True
    if path.parent.exists() and _real_dir(path.parent) is not True:
        return False
    path.mkdir(parents=True)
    return _real_dir(path) is True


def _real_dir(path: Path) -> bool | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    return True


def _read_named(directory: Path, name: str) -> bytes | None:
    if name != Path(name).name or name in {".", ".."}:
        return None
    return _read_file(directory / name, max_bytes=_SOURCE_MAX_BYTES)


def _read_json(path: Path) -> dict[str, Any] | None:
    raw = _read_file(path, max_bytes=_SOURCE_MAX_BYTES)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _read_file(path: Path, *, max_bytes: int) -> bytes | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                return None
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        temporary = ""
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            with contextlib.suppress(OSError):
                os.unlink(temporary)


def _canonical(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _encode(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _error(code: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "code": code,
        "error": _MESSAGES.get(code, "The signal operation did not finish."),
    }
    for key, value in extra.items():
        if key in {"text", "content", "body", "path", "state_root", "stdout", "stderr"}:
            continue
        payload[key] = value
    return payload


def _cancelled(cancelled: threading.Event | None) -> bool:
    return cancelled is not None and cancelled.is_set()


def _canonical_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) != 36:
        return None
    try:
        parsed = uuid.UUID(text)
    except (TypeError, ValueError, AttributeError):
        return None
    canonical = str(parsed)
    if canonical != text.lower():
        return None
    return canonical


def _iso_date(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    if parsed.strftime("%Y-%m-%d") != value:
        return None
    return value


def _parse_instant(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64 or "/" in value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed


def _require_root(state_root: Path) -> Path | dict[str, Any]:
    if not isinstance(state_root, Path) or not state_root.is_absolute():
        return _error("config_invalid")
    if any(part in {".openclaw", ".feishu-daily-ai-finance"} for part in state_root.parts):
        return _error("config_invalid")
    try:
        if state_root.is_symlink() or not state_root.is_dir():
            return _error("config_invalid")
    except OSError:
        return _error("config_invalid")
    return state_root


def _batch_thread_lock(batch_id: str) -> threading.Lock:
    with _BATCH_GUARD:
        lock = _BATCH_THREADS.get(batch_id)
        if lock is None:
            lock = threading.Lock()
            _BATCH_THREADS[batch_id] = lock
        return lock


@contextlib.contextmanager
def _flock(path: Path, thread_lock: threading.Lock) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with thread_lock:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _batch_lock(root: Path, batch_id: str) -> Iterator[None]:
    with _flock(root / "locks" / f"signals-{batch_id}.lock", _batch_thread_lock(batch_id)):
        yield


@contextlib.contextmanager
def _db_lock(root: Path) -> Iterator[None]:
    with _flock(root / "locks" / "ai-signal-memory.lock", _DB_THREAD):
        yield


