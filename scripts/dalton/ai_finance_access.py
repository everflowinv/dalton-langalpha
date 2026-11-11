"""Prepare and page one AI-finance batch from the operator state root.

The request cannot choose a path, an interpreter, or a shell. Candidate
reads use the manifest doc_id map. This module does not send, submit a
report, or write the signal database.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from scripts.dalton.ai_finance.ai_finance_github_momentum import resolve_explicit_path
    from scripts.dalton.ai_finance.ai_finance_github_trending import collect_trending
    from scripts.dalton.ai_finance.batch_retrieval import ConfirmedRetrievalFailure
    from scripts.dalton.ai_finance.prepare import PrepareError, prepare_batch
except ImportError:
    # The script directory is sys.path[0]. Collectors live in the ai_finance package.
    from ai_finance.ai_finance_github_momentum import resolve_explicit_path
    from ai_finance.ai_finance_github_trending import collect_trending
    from ai_finance.batch_retrieval import ConfirmedRetrievalFailure
    from ai_finance.prepare import PrepareError, prepare_batch

PROFILE = "ai-finance-daily"
HKT = ZoneInfo("Asia/Hong_Kong")
SOURCE_READ_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_CHARS = 16000
MAX_CHARS = 32000
MAX_RESPONSE_BYTES = 128 * 1024
_CONFIG_MAX_BYTES = 64 * 1024
_CLOCK_MAX_BYTES = 64 * 1024
_ALLOWED_OPERATIONS = frozenset({"auth_status", "search_library", "get_document"})
_RESOURCE_ORDER = ("manifest", "github", "earnings", "candidate", "news_seen")
_RESOURCES = frozenset(_RESOURCE_ORDER)
_NEWS_SEEN_FILE = "news_seen.json"
_NEWS_SEEN_RECORD = "news_seen_record.json"
_NEWS_RECORD_KEYS = frozenset({"schema", "batch_id", "owner_user_id", "started_at", "sha256"})
_NEWS_SEEN_WINDOW_SECONDS = 7 * 24 * 60 * 60
_STAGE_STATUS = frozenset({"pending", "failed", "complete"})
_PREPARE_KEYS = frozenset({
    "automation_execution_id",
    "scheduled_at",
    "started_at",
    "owner_user_id",
    "user_id",
    "workspace_id",
    "thread_id",
    "profile",
})
_READ_KEYS = frozenset({
    "automation_execution_id",
    "owner_user_id",
    "profile",
    "batch_id",
    "resource",
    "doc_id",
    "offset",
    "max_chars",
})
_ARTIFACTS = {
    "github": ("github_candidates.json", "github_candidates", "github"),
    "earnings": ("earnings_scan.json", "earnings_scan", "earnings"),
}
_DOC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CLOCK_THREAD = threading.Lock()
_OBSERVATION_THREAD = threading.Lock()
_MESSAGES = {
    "not_configured": "AI-finance prepare is not configured.",
    "config_invalid": "AI-finance config is invalid.",
    "invalid_request": "The request is not valid.",
    "unknown_field": "Request keys are not allowed.",
    "invalid_automation_execution": "automation_execution_id must be a canonical UUID.",
    "invalid_owner": "owner_user_id must be a canonical UUID.",
    "invalid_identity": "Caller identity must be canonical UUIDs.",
    "invalid_clock": "scheduled_at and started_at must be timezone-aware instants.",
    "clock_disagreement": "The execution clocks do not share one Hong Kong business date.",
    "clock_inconsistent": "The saved batch clock could not be read.",
    "owner_mismatch": "This batch belongs to another owner.",
    "profile_mismatch": "This batch profile is not ai-finance-daily.",
    "not_allowed": "This account cannot read the prepared batch.",
    "prepare_rejected": "The AI-finance batch could not be prepared.",
    "prepare_failed": "The AI-finance batch could not be prepared.",
    "read_failed": "The prepared batch could not be read.",
    "not_found": "The prepared batch is not available.",
    "hash_mismatch": "The prepared batch failed the hash check.",
    "offset_past_end": "offset is past the end of the prepared text.",
    "path_escape": "The candidate reference is not inside this batch.",
    "not_regular": "The prepared file is not a regular file.",
    "source_oversize": "The prepared batch exceeds the read bound.",
    "invalid_batch": "batch_id must be a real YYYY-MM-DD business date.",
    "invalid_resource": "resource must be manifest, github, earnings, candidate, or news_seen.",
    "news_seen_uncommitted": "This batch has no frozen news history.",
    "news_seen_invalid": "The news history is not a valid hash map.",
    "invalid_doc_id": "doc_id is only accepted for a manifest candidate.",
    "invalid_offset": "offset must be a nonnegative integer.",
    "invalid_max_chars": "max_chars must be an integer from 1 to 32000.",
    "response_exceeds_bound": "The page does not fit in the response bound.",
    "operation_rejected": "That AlphaEngine operation is not available.",
    "invalid_arguments": "The AlphaEngine arguments are not valid.",
    "research_failed": "AlphaEngine did not return a usable result.",
    "auth_not_ready": "AlphaEngine is not authenticated.",
    "alphaengine_timeout": "AlphaEngine did not answer before the timeout.",
}


class AccessError(Exception):
    """A batch read or config check failed with a public code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AeCallError(Exception):
    """One AlphaEngine call failed. The message is the public code only."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OperatorConfig:
    """Operator JSON. ``state_root`` is required.

    ``python_executable`` is optional and is kept as given, not resolved, so a
    virtualenv interpreter still sees its own prefix. ``embedding_model`` and
    ``embedding_dim`` are optional and may only be gemini-embedding-2 and 768.
    """

    state_root: Path
    python_executable: Path | None = None
    embedding_model: str = "gemini-embedding-2"
    embedding_dim: int = 768


def configured_owner_user_id() -> str | None:
    """Return ``DALTON_AI_FINANCE_OWNER_USER_ID`` when it is one hyphenated UUID."""
    return _canonical_uuid(os.environ.get("DALTON_AI_FINANCE_OWNER_USER_ID", ""))


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


def _message(code: str) -> str:
    return _MESSAGES.get(code, "The prepared batch could not be read.")


def _error(code: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "code": code, "error": _message(code)}
    for key, value in extra.items():
        if key in {"text", "content", "body", "path", "state_root"}:
            continue
        payload[key] = value
    payload.pop("text", None)
    return payload


def _aware(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    if value.tzinfo.utcoffset(value) is None:
        return None
    return value


def _parse_instant(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64 or "/" in value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _aware(parsed)


def _business_date(scheduled_at: datetime, started_at: datetime) -> str | None:
    scheduled_day = scheduled_at.astimezone(HKT).date()
    started_day = started_at.astimezone(HKT).date()
    if scheduled_day != started_day:
        return None
    return started_day.isoformat()


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


def _python_executable(value: object) -> Path:
    """Return the configured interpreter path without resolving it.

    A virtualenv ``python`` is often a symlink. Resolving it would drop the
    prefix Python uses to find ``pyvenv.cfg``. The path itself must still be
    absolute and name a file.
    """
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value:
        raise AccessError("config_invalid")
    path = Path(value)
    if not path.is_absolute():
        raise AccessError("config_invalid")
    if any(part in {".openclaw", ".feishu-daily-ai-finance"} for part in path.parts):
        raise AccessError("config_invalid")
    try:
        if not path.is_file():
            raise AccessError("config_invalid")
    except OSError:
        raise AccessError("config_invalid") from None
    return path


def _require_root(state_root: Path) -> Path:
    if not isinstance(state_root, Path) or not state_root.is_absolute():
        raise AccessError("config_invalid")
    try:
        if state_root.is_symlink():
            raise AccessError("config_invalid")
        resolved = resolve_explicit_path(state_root)
    except AccessError:
        raise
    except (OSError, ValueError):
        raise AccessError("config_invalid") from None
    try:
        info = resolved.lstat()
    except OSError:
        raise AccessError("config_invalid") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise AccessError("config_invalid")
    return resolved


def load_operator_config(config_path: Path) -> OperatorConfig:
    """Load the operator JSON. Unknown keys and symlink roots are refused."""
    if not isinstance(config_path, Path) or not config_path.is_absolute() or config_path.is_symlink():
        raise AccessError("config_invalid")
    try:
        info = config_path.lstat()
    except OSError:
        raise AccessError("config_invalid") from None
    if not stat.S_ISREG(info.st_mode) or info.st_size > _CONFIG_MAX_BYTES:
        raise AccessError("config_invalid")
    try:
        raw = _read_no_follow(config_path, max_bytes=_CONFIG_MAX_BYTES)
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, AccessError):
        raise AccessError("config_invalid") from None
    allowed = {"state_root", "python_executable", "embedding_model", "embedding_dim"}
    if not isinstance(data, dict) or "state_root" not in data or set(data) - allowed:
        raise AccessError("config_invalid")
    if not isinstance(data.get("state_root"), str):
        raise AccessError("config_invalid")
    python_executable = None
    if "python_executable" in data:
        python_executable = _python_executable(data.get("python_executable"))
    model = data.get("embedding_model", "gemini-embedding-2")
    dim = data.get("embedding_dim", 768)
    if model != "gemini-embedding-2" or type(dim) is not int or dim != 768:
        raise AccessError("config_invalid")
    return OperatorConfig(
        state_root=_require_root(Path(data["state_root"])),
        python_executable=python_executable,
        embedding_model=model,
        embedding_dim=dim,
    )


def research_params(tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Build one ``research`` call. The route and operation are not taken from arguments."""
    if tool not in _ALLOWED_OPERATIONS:
        raise AeCallError("operation_rejected")
    if not isinstance(arguments, dict):
        raise AeCallError("invalid_arguments")
    return {
        "name": "research",
        "arguments": {
            "route": "alphaengine",
            "operation": tool,
            "params": dict(arguments),
        },
    }


def unwrap_research_envelope(operation: str, envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``data.structuredContent`` after the business ``ok`` and ``isError`` checks.

    ``auth_status`` must carry ``authenticated`` true and ``status`` ``ready``.
    ``has_more`` true is a normal search page, not a failure.
    """
    if operation not in _ALLOWED_OPERATIONS or not isinstance(envelope, Mapping):
        raise AeCallError("research_failed")
    if envelope.get("error"):
        raise AeCallError("research_failed")
    result = envelope.get("result")
    if not isinstance(result, Mapping) or result.get("isError") is True:
        raise AeCallError("research_failed")
    business = _business_payload(result)
    if not isinstance(business, dict) or business.get("ok") is not True:
        raise AeCallError("research_failed")
    data = business.get("data")
    if not isinstance(data, dict) or data.get("isError", False) is not False:
        raise AeCallError("research_failed")
    structured = data.get("structuredContent")
    if not isinstance(structured, dict):
        raise AeCallError("research_failed")
    if operation == "auth_status":
        if structured.get("authenticated") is not True or structured.get("status") != "ready":
            raise AeCallError("auth_not_ready")
    return dict(structured)


def run_research_call(
    *,
    submit: Callable[[dict[str, Any]], Any],
    tool: str,
    arguments: Mapping[str, Any],
    timeout: float,
) -> dict[str, Any]:
    """Submit one call and wait once.

    A completed ``research_failed`` envelope becomes
    ``ConfirmedRetrievalFailure``. A timeout stays ``AeCallError`` and does
    not submit ``get_document`` again.
    """
    params = research_params(tool, arguments)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise AeCallError("invalid_arguments")
    future = submit(params)
    try:
        envelope = future.result(timeout=float(timeout))
    except TimeoutError:
        cancel = getattr(future, "cancel", None)
        if callable(cancel):
            cancel()
        raise AeCallError("alphaengine_timeout") from None
    if not isinstance(envelope, Mapping):
        raise ConfirmedRetrievalFailure("research_failed")
    try:
        return unwrap_research_envelope(tool, envelope)
    except AeCallError as exc:
        if exc.code == "research_failed":
            raise ConfirmedRetrievalFailure("research_failed") from None
        raise


def _business_payload(result: Mapping[str, Any]) -> dict[str, Any] | None:
    content = result.get("content")
    if isinstance(content, list) and content:
        block = content[0]
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            try:
                parsed = json.loads(block["text"])
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return parsed
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and "ok" in structured and "data" in structured:
        return dict(structured)
    return None


def parse_prepare_request(body: object) -> dict[str, Any]:
    """Validate the internal prepare body. Time values must already be canonical strings."""
    if not isinstance(body, Mapping):
        return _error("invalid_request")
    if set(body) != _PREPARE_KEYS:
        if set(body) - _PREPARE_KEYS:
            return _error("unknown_field")
        return _error("invalid_request")
    if body.get("profile") != PROFILE:
        return _error("profile_mismatch")
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
    scheduled_at = _parse_instant(body.get("scheduled_at"))
    started_at = _parse_instant(body.get("started_at"))
    if scheduled_at is None or started_at is None:
        return _error("invalid_clock")
    return {
        "ok": True,
        "execution_id": execution_id,
        "owner_user_id": owner,
        "user_id": user_id,
        "workspace_id": workspace_id,
        "thread_id": thread_id,
        "scheduled_at": scheduled_at,
        "started_at": started_at,
        "profile": PROFILE,
    }


def parse_read_request(body: object) -> dict[str, Any]:
    """Validate one read body. ``doc_id`` is required only for ``candidate``."""
    if not isinstance(body, Mapping):
        return _error("invalid_request")
    if set(body) - _READ_KEYS:
        return _error("unknown_field")
    required = {"automation_execution_id", "owner_user_id", "profile", "batch_id", "resource"}
    if not required <= set(body):
        return _error("invalid_request")
    if body.get("profile") != PROFILE:
        return _error("profile_mismatch")
    execution_id = _canonical_uuid(body.get("automation_execution_id"))
    owner = _canonical_uuid(body.get("owner_user_id"))
    if execution_id is None:
        return _error("invalid_automation_execution")
    if owner is None:
        return _error("invalid_owner")
    batch_id = _iso_date(body.get("batch_id"))
    if batch_id is None:
        return _error("invalid_batch")
    resource = body.get("resource")
    if resource not in _RESOURCES:
        return _error("invalid_resource")
    has_doc = "doc_id" in body
    if resource == "candidate":
        if not has_doc:
            return _error("invalid_doc_id")
        doc_id = body.get("doc_id")
        if not isinstance(doc_id, str) or _DOC_ID.fullmatch(doc_id) is None or ".." in doc_id:
            return _error("invalid_doc_id")
    elif has_doc:
        return _error("invalid_doc_id")
    else:
        doc_id = None
    offset = body["offset"] if "offset" in body else 0
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or offset > SOURCE_READ_MAX_BYTES:
        return _error("invalid_offset")
    max_chars = body["max_chars"] if "max_chars" in body else DEFAULT_MAX_CHARS
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= MAX_CHARS:
        return _error("invalid_max_chars")
    return {
        "ok": True,
        "execution_id": execution_id,
        "owner_user_id": owner,
        "batch_id": batch_id,
        "resource": resource,
        "doc_id": doc_id,
        "offset": offset,
        "max_chars": max_chars,
        "profile": PROFILE,
    }


def prepare_ai_finance_batch(
    *,
    state_root: Path,
    scheduled_at: datetime,
    started_at: datetime,
    execution_id: str,
    owner_user_id: str,
    transport: Any,
    github_collector: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    earnings_collector: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Freeze the first execution instant for the Hong Kong date, then prepare that batch.

    A later execution for the same date passes the saved instant into
    ``prepare_batch``. The GitHub observation pool write is serialized across
    batch ids. Completed stages stay on the core reuse path.
    """
    configured = configured_owner_user_id()
    owner = _canonical_uuid(owner_user_id)
    canonical_execution = _canonical_uuid(execution_id)
    if configured is None:
        return _error("not_configured")
    if owner is None or owner != configured:
        return _error("owner_mismatch")
    if canonical_execution is None:
        return _error("invalid_automation_execution")
    scheduled = _aware(scheduled_at)
    started = _aware(started_at)
    if scheduled is None or started is None:
        return _error("invalid_clock")
    batch_id = _business_date(scheduled, started)
    if batch_id is None:
        return _error("clock_disagreement")
    try:
        root = _require_root(state_root)
    except AccessError as exc:
        return _error(exc.code)
    frozen, recovered, problem = _freeze_clock(
        root,
        batch_id=batch_id,
        scheduled_at=scheduled,
        started_at=started,
        execution_id=canonical_execution,
        owner_user_id=owner,
    )
    if problem or frozen is None:
        return _error(problem or "clock_inconsistent")
    collector = github_collector or _locked_github(root)
    try:
        result = prepare_batch(
            state_root=root,
            batch_id=batch_id,
            started_at=frozen,
            transport=transport,
            github_collector=collector,
            earnings_collector=earnings_collector,
        )
    except PrepareError:
        return _error("prepare_rejected", batch_id=batch_id, recovered=recovered)
    except Exception:
        return _error("prepare_failed", batch_id=batch_id, recovered=recovered)
    return _public_prepare(root, result, recovered=recovered)


def read_ai_finance_page(state_root: Path, body: Mapping[str, Any]) -> dict[str, Any]:
    """Return one character page of a prepared batch, or an error without its text."""
    parsed = parse_read_request(body)
    if parsed.get("ok") is not True:
        return parsed
    configured = configured_owner_user_id()
    if configured is None:
        return _error("not_configured")
    if parsed["owner_user_id"] != configured:
        return _error("not_allowed")
    try:
        root = _require_root(state_root)
        clock = _load_clock_for_read(root, parsed["batch_id"], configured)
        raw, digest = _resource_bytes(root, clock, parsed["resource"], parsed.get("doc_id"))
    except AccessError as exc:
        return _error(exc.code, batch_id=parsed["batch_id"], resource=parsed["resource"])
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return _error("hash_mismatch", batch_id=parsed["batch_id"], resource=parsed["resource"])
    return _page(
        text,
        digest,
        batch_id=parsed["batch_id"],
        resource=parsed["resource"],
        doc_id=parsed.get("doc_id"),
        offset=int(parsed["offset"]),
        max_chars=int(parsed["max_chars"]),
    )


def _public_prepare(root: Path, result: object, *, recovered: bool) -> dict[str, Any]:
    if not isinstance(result, dict):
        return _error("prepare_failed", recovered=recovered)
    batch_id = result.get("batch_id") if _iso_date(result.get("batch_id")) else None
    payload: dict[str, Any] = {
        "ok": False,
        "profile": PROFILE,
        "recovered": recovered,
        "manifest_ready": False,
        "candidate_doc_ids": [],
        "resources": list(_RESOURCE_ORDER),
    }
    if batch_id is not None:
        payload["batch_id"] = batch_id
    started = _parse_instant(result.get("started_at"))
    if started is not None:
        payload["started_at"] = started.isoformat()
    window = _public_window(result.get("window"))
    if window is not None:
        payload["window"] = window
    stages = _public_stages(result.get("stages"))
    if stages is not None:
        payload["stages"] = stages
    reused = _public_reused(result.get("reused"))
    payload["reused"] = reused
    errors = _public_errors(result.get("errors"))
    payload["errors"] = errors
    payload["inconsistent"] = result.get("inconsistent") is True
    ready = (
        result.get("ok") is True
        and result.get("inconsistent") is not True
        and not errors
        and batch_id is not None
        and started is not None
        and window is not None
        and stages == {"github": "complete", "earnings": "complete"}
    )
    if ready:
        try:
            manifest = _verified_manifest(root, batch_id)
            payload["candidate_doc_ids"] = _doc_ids(manifest)
            payload["manifest_ready"] = True
            payload["ok"] = True
            payload["code"] = "ok"
        except AccessError:
            payload["ok"] = False
            payload["code"] = "hash_mismatch"
            payload["error"] = _message("hash_mismatch")
            payload["inconsistent"] = True
            payload["manifest_ready"] = False
            payload["candidate_doc_ids"] = []
    else:
        code = errors[0] if errors else ("inconsistent" if payload["inconsistent"] else "prepare_failed")
        payload["code"] = code
        payload["error"] = _message(code) if code in _MESSAGES else "The AI-finance batch was not prepared."
        if code not in _MESSAGES:
            payload["error"] = "The AI-finance batch was not prepared."
    return payload


def _public_window(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    timezone_name = value.get("timezone")
    start = _parse_instant(value.get("start"))
    end = _parse_instant(value.get("end"))
    hours = value.get("hours")
    if timezone_name != "Asia/Hong_Kong" or start is None or end is None:
        return None
    if type(hours) is not int or not 1 <= hours <= 168:
        return None
    return {
        "timezone": "Asia/Hong_Kong",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "hours": hours,
    }


def _public_stages(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict) or set(value) != {"github", "earnings"}:
        return None
    stages: dict[str, str] = {}
    for name in ("github", "earnings"):
        status = value.get(name)
        if not isinstance(status, str) or status not in _STAGE_STATUS:
            return None
        stages[name] = status
    return stages


def _public_reused(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    kept: list[str] = []
    for item in value:
        if item in {"github", "earnings"} and item not in kept:
            kept.append(item)
    return kept


def _public_errors(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    kept: list[str] = []
    for item in value:
        if isinstance(item, str) and _TOKEN.fullmatch(item):
            kept.append(item)
    return kept


def _freeze_clock(
    root: Path,
    *,
    batch_id: str,
    scheduled_at: datetime,
    started_at: datetime,
    execution_id: str,
    owner_user_id: str,
) -> tuple[datetime | None, bool, str | None]:
    path = root / "batch_clocks" / f"{batch_id}.json"
    with _flock(path.with_suffix(".lock"), _CLOCK_THREAD):
        existing, problem = _read_clock(path, batch_id)
        if problem:
            return None, False, problem
        if existing is not None:
            if existing["owner_user_id"] != owner_user_id:
                return None, True, "owner_mismatch"
            if existing["profile"] != PROFILE:
                return None, True, "profile_mismatch"
            try:
                _freeze_news_seen(
                    root,
                    batch_id=batch_id,
                    owner_user_id=owner_user_id,
                    started_at=existing["started_at"],
                    clock_exists=True,
                )
            except AccessError as exc:
                return None, True, exc.code
            return existing["started_at"], True, None
        try:
            _freeze_news_seen(
                root,
                batch_id=batch_id,
                owner_user_id=owner_user_id,
                started_at=started_at,
                clock_exists=False,
            )
        except AccessError as exc:
            return None, False, exc.code
        document = {
            "schema": 1,
            "batch_id": batch_id,
            "started_at": started_at.isoformat(),
            "scheduled_at": scheduled_at.isoformat(),
            "first_execution_id": execution_id,
            "owner_user_id": owner_user_id,
            "profile": PROFILE,
        }
        _atomic_write(path, _canonical_bytes(document))
        return started_at, False, None


def _freeze_news_seen(
    root: Path,
    *,
    batch_id: str,
    owner_user_id: str,
    started_at: datetime,
    clock_exists: bool,
) -> None:
    """Freeze ``seen.json`` once, before the clock file is published.

    The caller holds the clock lock. A snapshot that is already on disk is
    checked and kept. This does not take the report lock and does not import
    the report module. ``seen.json`` is read only while this batch has no
    snapshot file yet.
    """
    batch_dir = root / "batches" / batch_id
    snapshot = batch_dir / _NEWS_SEEN_FILE
    record = batch_dir / _NEWS_SEEN_RECORD
    if _path_exists(snapshot) or _path_exists(record):
        _require_real_dir(root / "batches")
        _require_real_dir(batch_dir)
        _durable_news_seen(batch_dir, batch_id, owner_user_id, started_at)
        return
    if clock_exists:
        return
    projected = _project_news_seen(root, started_at)
    raw = _canonical_bytes(projected)
    if len(raw) > SOURCE_READ_MAX_BYTES:
        raise AccessError("source_oversize")
    batch_dir = _ensure_batch_dir(root, batch_id)
    _atomic_write(batch_dir / _NEWS_SEEN_FILE, raw)
    digest = hashlib.sha256(raw).hexdigest()
    _atomic_write(batch_dir / _NEWS_SEEN_RECORD, _canonical_bytes({
        "schema": 1,
        "batch_id": batch_id,
        "owner_user_id": owner_user_id,
        "started_at": started_at.isoformat(),
        "sha256": digest,
    }))


def _project_news_seen(root: Path, started_at: datetime) -> dict[str, int | float]:
    path = root / "seen.json"
    try:
        path.lstat()
    except FileNotFoundError:
        return {}
    except OSError:
        raise AccessError("read_failed") from None
    raw = _read_no_follow(path, max_bytes=SOURCE_READ_MAX_BYTES)
    return _news_map(raw, started_at=started_at)


def _durable_news_seen(
    batch_dir: Path,
    batch_id: str,
    owner_user_id: str,
    started_at: datetime,
) -> bytes:
    """Return the frozen snapshot bytes when the completion record matches."""
    snapshot = batch_dir / _NEWS_SEEN_FILE
    record_path = batch_dir / _NEWS_SEEN_RECORD
    if not _path_exists(snapshot) or not _path_exists(record_path):
        raise AccessError("news_seen_uncommitted")
    record_raw = _read_no_follow(record_path, max_bytes=SOURCE_READ_MAX_BYTES)
    try:
        record = json.loads(record_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise AccessError("news_seen_invalid") from None
    if type(record) is not dict or set(record) != _NEWS_RECORD_KEYS:
        raise AccessError("news_seen_invalid")
    if type(record.get("schema")) is not int or record.get("schema") != 1:
        raise AccessError("news_seen_invalid")
    if record.get("batch_id") != batch_id or record.get("owner_user_id") != owner_user_id:
        raise AccessError("news_seen_invalid")
    frozen = _parse_instant(record.get("started_at"))
    if frozen is None or frozen.astimezone(timezone.utc) != started_at.astimezone(timezone.utc):
        raise AccessError("news_seen_invalid")
    digest = record.get("sha256")
    if not isinstance(digest, str) or _HEX64.fullmatch(digest) is None:
        raise AccessError("news_seen_invalid")
    raw = _read_no_follow(snapshot, max_bytes=SOURCE_READ_MAX_BYTES)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise AccessError("hash_mismatch")
    _news_map(raw)
    return raw


def _news_map(raw: bytes, *, started_at: datetime | None = None) -> dict[str, int | float]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise AccessError("news_seen_invalid") from None
    if type(parsed) is not dict:
        raise AccessError("news_seen_invalid")
    cutoff = None if started_at is None else started_at.timestamp() - _NEWS_SEEN_WINDOW_SECONDS
    kept: dict[str, int | float] = {}
    for key, value in parsed.items():
        if type(key) is not str or _HEX64.fullmatch(key) is None:
            raise AccessError("news_seen_invalid")
        if type(value) is bool or type(value) not in {int, float} or not math.isfinite(value):
            raise AccessError("news_seen_invalid")
        if cutoff is not None and value < cutoff:
            continue
        kept[key] = value
    return kept


def _ensure_batch_dir(root: Path, batch_id: str) -> Path:
    batches = root / "batches"
    if _path_exists(batches):
        _require_real_dir(batches)
    else:
        batches.mkdir()
    batch_dir = batches / batch_id
    if batch_dir.parent != batches or batch_dir.name != batch_id:
        raise AccessError("path_escape")
    if _path_exists(batch_dir):
        _require_real_dir(batch_dir)
    else:
        batch_dir.mkdir()
    return batch_dir


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise AccessError("read_failed") from None
    return True


def _load_clock_for_read(root: Path, batch_id: str, owner_user_id: str) -> dict[str, Any]:
    path = root / "batch_clocks" / f"{batch_id}.json"
    existing, problem = _read_clock(path, batch_id)
    if problem == "not_found" or existing is None and problem is None:
        raise AccessError("not_found")
    if problem or existing is None:
        raise AccessError(problem or "clock_inconsistent")
    if existing["owner_user_id"] != owner_user_id:
        raise AccessError("owner_mismatch")
    if existing["profile"] != PROFILE:
        raise AccessError("profile_mismatch")
    return existing


def _read_clock(path: Path, batch_id: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        path.lstat()
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, "clock_inconsistent"
    try:
        data = json.loads(_read_no_follow(path, max_bytes=_CLOCK_MAX_BYTES).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, AccessError):
        return None, "clock_inconsistent"
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("batch_id") != batch_id:
        return None, "clock_inconsistent"
    if data.get("profile") != PROFILE:
        return None, "clock_inconsistent"
    started = _parse_instant(data.get("started_at"))
    scheduled = _parse_instant(data.get("scheduled_at"))
    owner = _canonical_uuid(data.get("owner_user_id"))
    execution_id = _canonical_uuid(data.get("first_execution_id"))
    if started is None or scheduled is None or owner is None or execution_id is None:
        return None, "clock_inconsistent"
    if _business_date(scheduled, started) != batch_id:
        return None, "clock_inconsistent"
    return {
        "started_at": started,
        "scheduled_at": scheduled,
        "owner_user_id": owner,
        "first_execution_id": execution_id,
        "profile": PROFILE,
        "batch_id": batch_id,
    }, None


def _locked_github(state_root: Path) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def collect(request: dict[str, Any]) -> dict[str, Any]:
        with _flock(state_root / "locks" / "github-observation.lock", _OBSERVATION_THREAD):
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

    return collect


def _resource_bytes(
    root: Path,
    clock: Mapping[str, Any],
    resource: str,
    doc_id: str | None,
) -> tuple[bytes, str]:
    batch_id = str(clock["batch_id"])
    metadata, manifest, batch_dir = _load_verified_batch(root, batch_id, clock["started_at"])
    if resource == "news_seen":
        raw = _durable_news_seen(batch_dir, batch_id, str(clock["owner_user_id"]), clock["started_at"])
        return raw, hashlib.sha256(raw).hexdigest()
    if resource == "manifest":
        raw = _read_named(batch_dir, "manifest.json")
        digest = hashlib.sha256(raw).hexdigest()
        if digest != metadata.get("manifest_sha256"):
            raise AccessError("hash_mismatch")
        return raw, digest
    if resource == "candidate":
        return _candidate_bytes(batch_dir, metadata, manifest, doc_id)
    filename, artifact_key, stage_name = _ARTIFACTS[resource]
    raw = _read_named(batch_dir, filename)
    digest = hashlib.sha256(raw).hexdigest()
    stage = metadata["stages"][stage_name]
    artifact = manifest["artifacts"][artifact_key]
    if digest != stage.get("sha256") or digest != artifact.get("sha256"):
        raise AccessError("hash_mismatch")
    if artifact.get("path") != filename:
        raise AccessError("hash_mismatch")
    return raw, digest


def _candidate_bytes(
    batch_dir: Path,
    metadata: Mapping[str, Any],
    manifest: Mapping[str, Any],
    doc_id: str | None,
) -> tuple[bytes, str]:
    if not isinstance(doc_id, str):
        raise AccessError("invalid_doc_id")
    matches = [
        row for row in manifest.get("candidates") or []
        if isinstance(row, dict) and row.get("doc_id") == doc_id
    ]
    if len(matches) != 1:
        raise AccessError("not_found")
    row = matches[0]
    relative = row.get("path")
    name = _candidate_name(relative)
    if name is None:
        raise AccessError("path_escape")
    recorded = metadata["stages"]["earnings"].get("candidate_files")
    if not isinstance(recorded, dict) or recorded.get(relative) != row.get("sha256"):
        raise AccessError("hash_mismatch")
    candidates = batch_dir / "candidates"
    _require_real_dir(candidates)
    path = candidates / name
    if path.parent != candidates:
        raise AccessError("path_escape")
    raw = _read_no_follow(path, max_bytes=SOURCE_READ_MAX_BYTES)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != row.get("sha256"):
        raise AccessError("hash_mismatch")
    return raw, digest


def _candidate_name(value: object) -> str | None:
    if not isinstance(value, str) or "\\" in value or value.startswith("/") or ".." in value:
        return None
    parts = value.split("/")
    if len(parts) != 2 or parts[0] != "candidates" or parts[1] in {"", ".", ".."}:
        return None
    if "/" in parts[1] or parts[1] != Path(parts[1]).name:
        return None
    return parts[1]


def _load_verified_batch(
    root: Path,
    batch_id: str,
    started_at: datetime,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    batches = root / "batches"
    _require_real_dir(batches)
    batch_dir = batches / batch_id
    _require_real_dir(batch_dir)
    if batch_dir.parent != batches:
        raise AccessError("path_escape")
    metadata = _load_json(batch_dir / "metadata.json")
    if metadata.get("schema") != 1 or metadata.get("batch_id") != batch_id:
        raise AccessError("hash_mismatch")
    frozen = _parse_instant(metadata.get("started_at"))
    if frozen is None or frozen.astimezone(timezone.utc) != started_at.astimezone(timezone.utc):
        raise AccessError("clock_inconsistent")
    manifest = _verified_manifest(root, batch_id, metadata=metadata, batch_dir=batch_dir)
    return metadata, manifest, batch_dir


def _verified_manifest(
    root: Path,
    batch_id: str,
    *,
    metadata: dict[str, Any] | None = None,
    batch_dir: Path | None = None,
) -> dict[str, Any]:
    directory = batch_dir or (root / "batches" / batch_id)
    if metadata is None:
        metadata = _load_json(directory / "metadata.json")
    recorded = metadata.get("manifest_sha256")
    if not isinstance(recorded, str) or _HEX64.fullmatch(recorded) is None:
        raise AccessError("hash_mismatch")
    raw = _read_named(directory, "manifest.json")
    if hashlib.sha256(raw).hexdigest() != recorded:
        raise AccessError("hash_mismatch")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise AccessError("hash_mismatch") from None
    if not isinstance(manifest, dict) or manifest.get("batch_id") != batch_id:
        raise AccessError("hash_mismatch")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise AccessError("hash_mismatch")
    for filename, key, stage_name in _ARTIFACTS.values():
        artifact = artifacts.get(key)
        stage = (metadata.get("stages") or {}).get(stage_name) if isinstance(metadata.get("stages"), dict) else None
        if not isinstance(artifact, dict) or not isinstance(stage, dict):
            raise AccessError("hash_mismatch")
        if artifact.get("path") != filename or artifact.get("sha256") != stage.get("sha256"):
            raise AccessError("hash_mismatch")
        file_raw = _read_named(directory, filename)
        if hashlib.sha256(file_raw).hexdigest() != artifact.get("sha256"):
            raise AccessError("hash_mismatch")
    rows = manifest.get("candidates")
    if not isinstance(rows, list):
        raise AccessError("hash_mismatch")
    return manifest


def _doc_ids(manifest: Mapping[str, Any]) -> list[str]:
    rows = manifest.get("candidates")
    if not isinstance(rows, list):
        raise AccessError("hash_mismatch")
    found: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise AccessError("hash_mismatch")
        doc_id = row.get("doc_id")
        if not isinstance(doc_id, str) or _DOC_ID.fullmatch(doc_id) is None or ".." in doc_id or doc_id in seen:
            raise AccessError("hash_mismatch")
        if _candidate_name(row.get("path")) is None:
            raise AccessError("path_escape")
        seen.add(doc_id)
        found.append(doc_id)
    return found


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(_read_no_follow(path, max_bytes=SOURCE_READ_MAX_BYTES).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, AccessError):
        raise AccessError("read_failed") from None
    if not isinstance(data, dict):
        raise AccessError("hash_mismatch")
    return data


def _read_named(directory: Path, name: str) -> bytes:
    if name != Path(name).name or name in {".", ".."}:
        raise AccessError("path_escape")
    _require_real_dir(directory)
    path = directory / name
    if path.parent != directory:
        raise AccessError("path_escape")
    return _read_no_follow(path, max_bytes=SOURCE_READ_MAX_BYTES)


def _require_real_dir(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise AccessError("not_found") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise AccessError("path_escape")


def _read_no_follow(path: Path, *, max_bytes: int) -> bytes:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise AccessError("not_found") from None
    except OSError:
        raise AccessError("read_failed") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise AccessError("not_regular")
    if info.st_size > max_bytes:
        raise AccessError("source_oversize")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise AccessError("not_regular") from None
    try:
        stat_result = os.fstat(fd)
        if not stat.S_ISREG(stat_result.st_mode) or stat_result.st_size > max_bytes:
            raise AccessError("not_regular")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                raise AccessError("source_oversize")
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _page(
    text: str,
    digest: str,
    *,
    batch_id: str,
    resource: str,
    doc_id: str | None,
    offset: int,
    max_chars: int,
) -> dict[str, Any]:
    total = len(text)
    shell: dict[str, Any] = {
        "ok": True,
        "batch_id": batch_id,
        "resource": resource,
        "offset": offset,
        "max_chars": max_chars,
        "total_chars": total,
        "content_sha256": digest,
        "profile": PROFILE,
    }
    if doc_id is not None:
        shell["doc_id"] = doc_id
    if offset > total:
        return _error(
            "offset_past_end",
            batch_id=batch_id,
            resource=resource,
            offset=offset,
            total_chars=total,
            content_sha256=digest,
        )
    return _fit_page(shell, text, offset, max_chars, total)


def _fit_page(
    shell: dict[str, Any],
    full: str,
    offset: int,
    max_chars: int,
    total: int,
) -> dict[str, Any]:
    window = min(max_chars, total - offset)
    lo = 0
    hi = window
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if len(_encode(_slice_page(shell, full, offset, mid, total))) <= MAX_RESPONSE_BYTES:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if window > 0 and best == 0:
        return _error("response_exceeds_bound", batch_id=shell.get("batch_id"), resource=shell.get("resource"))
    page = _slice_page(shell, full, offset, best, total)
    if len(_encode(page)) > MAX_RESPONSE_BYTES:
        return _error("response_exceeds_bound", batch_id=shell.get("batch_id"), resource=shell.get("resource"))
    return page


def _slice_page(
    shell: Mapping[str, Any],
    full: str,
    offset: int,
    count: int,
    total: int,
) -> dict[str, Any]:
    end = offset + count
    page = dict(shell)
    page["text"] = full[offset:end]
    page["next_offset"] = None if end >= total else end
    page["complete"] = end >= total
    return page


def _encode(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


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


__all__ = [
    "DEFAULT_MAX_CHARS",
    "MAX_CHARS",
    "MAX_RESPONSE_BYTES",
    "PROFILE",
    "AccessError",
    "AeCallError",
    "OperatorConfig",
    "configured_owner_user_id",
    "load_operator_config",
    "parse_prepare_request",
    "parse_read_request",
    "prepare_ai_finance_batch",
    "read_ai_finance_page",
    "research_params",
    "run_research_call",
    "unwrap_research_envelope",
]
